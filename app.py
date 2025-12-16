# app.py
import os
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# -----------------------------
# Load env + settings
# -----------------------------
load_dotenv()
from config import load_settings
settings = load_settings()

# -----------------------------
# Models
# -----------------------------
from models import Base, User

# -----------------------------
# Shopify helpers
# -----------------------------
from test_shopify_api import (
    get_all_customers,
    get_all_orders,
    get_customer_orders,
    get_customer_basic_info,   # ✅ NEW (email backfill)
)

# -----------------------------
# Subscription constants
# -----------------------------
TIER1_PRODUCT_ID = 8424668299439
TIER2_PRODUCT_ID = 8424683241647
PRO_PRODUCT_ID   = 8424226160815

SUBSCRIPTION_PRODUCT_IDS = {
    TIER1_PRODUCT_ID,
    TIER2_PRODUCT_ID,
    PRO_PRODUCT_ID,
}

TIER1_USES = 10
ACCESS_DAYS = 30

SUBSCRIPTION_PAGE = settings.plan_page_url
TOOL_URL = settings.tool_app_url

# -----------------------------
# Flask + DB setup
# -----------------------------
app = Flask(__name__)
CORS(app, origins=[
    "https://capsule-builder-qhzx.vercel.app",
    "https://formdepartment.com",
])

DB_PATH = os.getenv("SQLITE_PATH", "sqlite:///shopify_access.db")
engine = create_engine(DB_PATH, connect_args={"check_same_thread": False})

@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _):
    try:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA temp_store=MEMORY;")
        cur.close()
    except Exception:
        pass

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# -----------------------------
# Tiny in-memory TTL cache
# -----------------------------
class TTLCache:
    def __init__(self, ttl_seconds: int = 120):
        self.ttl = ttl_seconds
        self._store = {}

    def get(self, key):
        item = self._store.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at < datetime.utcnow().timestamp():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key, value, ttl_seconds: Optional[int] = None):
        ttl = self.ttl if ttl_seconds is None else int(ttl_seconds)
        self._store[key] = (datetime.utcnow().timestamp() + ttl, value)

    def delete(self, key):
        self._store.pop(key, None)

tool_cache = TTLCache(120)

# -----------------------------
# Helpers
# -----------------------------
def is_admin(user: User) -> bool:
    if not user.email:
        return False
    return user.email.lower() in {e.lower() for e in settings.master_customer_emails}

def _parse_shopify_dt(iso_str: str) -> datetime:
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))

# -----------------------------
# Per-customer subscription refresh (FAST)
# -----------------------------
def refresh_customer_subscription(sess, user: User, max_age_seconds: int = 300, force: bool = False):
    now = datetime.utcnow()

    # Skip refresh if not forced and recent check is valid
    if not force and user.last_shopify_check_at:
        if (now - user.last_shopify_check_at).total_seconds() <= max_age_seconds:
            return

    # Fetch customer orders from Shopify if forced or cache expired
    orders = get_customer_orders(int(user.customer_id), limit=50)
    latest = None

    for o in orders:
        created = o.get("created_at")
        if not created:
            continue
        created_dt = _parse_shopify_dt(created)
        for li in o.get("line_items", []):
            pid = li.get("product_id")
            if pid in SUBSCRIPTION_PRODUCT_IDS:
                if not latest or created_dt > latest[0]:
                    latest = (created_dt, o.get("id"), pid)

    user.last_shopify_check_at = now

    if not latest:
        user.plan = "none"
        user.expiry = None
        user.remaining_uses = None
        sess.commit()
        return

    created_dt, order_id, pid = latest

    if pid == TIER1_PRODUCT_ID:
        user.plan = "tier1"
        if user.last_subscription_order_id != order_id:
            user.remaining_uses = TIER1_USES
    elif pid == TIER2_PRODUCT_ID:
        user.plan = "tier2"
        user.remaining_uses = None
    elif pid == PRO_PRODUCT_ID:
        user.plan = "pro"
        user.remaining_uses = None

    user.plan_product_id = pid
    user.expiry = created_dt + timedelta(days=ACCESS_DAYS)
    user.last_subscription_order_id = order_id
    user.last_subscription_purchase_at = created_dt
    sess.commit()

# -----------------------------
# Tool entry
# -----------------------------
@app.route("/proxy/tool", methods=["GET"])
def proxy_tool():
    customer_id = request.args.get("customer_id") or request.args.get("logged_in_customer_id")
    if not customer_id or not customer_id.isdigit():
        return jsonify({"ok": False}), 400

    cid = int(customer_id)
    cache_key = f"tool:{cid}"
    cached = tool_cache.get(cache_key)
    if cached:
        return jsonify(cached)

    sess = Session()
    user = sess.query(User).filter_by(customer_id=cid).first()

    if not user:
        user = User(customer_id=cid, trial_used=False)
        sess.add(user)
        sess.commit()

    # 🔐 EMAIL BACKFILL (only once, only if missing)
    # 🔐 BASIC INFO BACKFILL (email + first/last name, only once)
    if user.email is None or user.first_name is None or user.last_name is None:
        try:
            from test_shopify_api import get_customer_basic_info
            info = get_customer_basic_info(cid)
            if info:
                user.email = user.email or info.get("email")
                user.first_name = user.first_name or info.get("first_name")
                user.last_name = user.last_name or info.get("last_name")
                sess.commit()
        except Exception:
            pass  # never block user flow

    # 🔥 ADMIN SHORT-CIRCUIT
    if is_admin(user):
        resp = {
            "ok": True,
            "is_admin": True,
            "trial_used": True,
            "show_trial_banner": False,
            "has_subscription": True,
            "plan": "admin",
            "tool_url": TOOL_URL,
        }
        sess.close()
        tool_cache.set(cache_key, resp, 300)
        return jsonify(resp)

    now = datetime.utcnow()
    has_active = bool(user.expiry and now <= user.expiry)

    resp = {
        "ok": True,
        "trial_used": user.trial_used,
        "show_trial_banner": not user.trial_used,
        "has_subscription": has_active,
        "plan": user.plan if has_active else None,
        "remaining_uses": user.remaining_uses if has_active else None,
        "tool_url": TOOL_URL,
    }

    sess.close()
    tool_cache.set(cache_key, resp, 120)
    return jsonify(resp)

# -----------------------------
# Submission validation
# -----------------------------
@app.route("/proxy/validate-submission", methods=["GET"])
def validate_submission():
    customer_id = request.args.get("customer_id") or request.args.get("logged_in_customer_id")
    if not customer_id or not customer_id.isdigit():
        return jsonify({"ok": False}), 400

    cid = int(customer_id)
    sess = Session()
    user = sess.query(User).filter_by(customer_id=cid).first()

    if not user:
        user = User(customer_id=cid, trial_used=False)
        sess.add(user)
        sess.commit()

    if is_admin(user):
        sess.close()
        return jsonify({"ok": True, "plan": "admin"})

    # Force refresh when user is coming back from Shopify subscription page (important)
    force = False

    # Force refresh if user plan is 'none' (meaning user was previously unsubscribed)
    if user.plan == "none" or not user.expiry or (datetime.utcnow() > user.expiry):
        force = True

    # Trigger forced refresh if needed
    refresh_customer_subscription(sess, user, force=force)

    now = datetime.utcnow()
    if not user.expiry or now > user.expiry:
        sess.close()
        return jsonify({"ok": False, "redirect": SUBSCRIPTION_PAGE}), 403

    if user.plan == "tier1":
        if user.remaining_uses <= 0:
            sess.close()
            return jsonify({"ok": False, "redirect": SUBSCRIPTION_PAGE}), 403
        user.remaining_uses -= 1
        sess.commit()

    tool_cache.delete(f"tool:{cid}")
    sess.close()
    return jsonify({"ok": True, "plan": user.plan})

@app.route("/proxy/validate-submission", methods=["GET"])
def validate_submission():
    customer_id = request.args.get("customer_id") or request.args.get("logged_in_customer_id")
    if not customer_id or not customer_id.isdigit():
        return jsonify({"ok": False}), 400

    cid = int(customer_id)
    sess = Session()
    user = sess.query(User).filter_by(customer_id=cid).first()

    if not user:
        user = User(customer_id=cid, trial_used=False)
        sess.add(user)
        sess.commit()

    if is_admin(user):
        sess.close()
        return jsonify({"ok": True, "plan": "admin"})

    # Force refresh when user is coming back from Shopify subscription page (important)
    force = False

    # Force refresh if user plan is 'none' (meaning user was previously unsubscribed)
    if user.plan == "none" or not user.expiry or (datetime.utcnow() > user.expiry):
        force = True

    # Trigger forced refresh if needed
    refresh_customer_subscription(sess, user, force=force)

    now = datetime.utcnow()
    if not user.expiry or now > user.expiry:
        sess.close()
        return jsonify({"ok": False, "redirect": SUBSCRIPTION_PAGE}), 403

    if user.plan == "tier1":
        if user.remaining_uses <= 0:
            sess.close()
            return jsonify({"ok": False, "redirect": SUBSCRIPTION_PAGE}), 403
        user.remaining_uses -= 1
        sess.commit()

    tool_cache.delete(f"tool:{cid}")
    sess.close()
    return jsonify({"ok": True, "plan": user.plan})
@app.route("/proxy/validate-submission", methods=["GET"])
def validate_submission():
    customer_id = request.args.get("customer_id") or request.args.get("logged_in_customer_id")
    if not customer_id or not customer_id.isdigit():
        return jsonify({"ok": False}), 400

    cid = int(customer_id)
    sess = Session()
    user = sess.query(User).filter_by(customer_id=cid).first()

    if not user:
        user = User(customer_id=cid, trial_used=False)
        sess.add(user)
        sess.commit()

    if is_admin(user):
        sess.close()
        return jsonify({"ok": True, "plan": "admin"})

    # Force refresh when user is coming back from Shopify subscription page (important)
    force = False

    # Force refresh if user plan is 'none' (meaning user was previously unsubscribed)
    if user.plan == "none" or not user.expiry or (datetime.utcnow() > user.expiry):
        force = True

    # Trigger forced refresh if needed
    refresh_customer_subscription(sess, user, force=force)

    now = datetime.utcnow()
    if not user.expiry or now > user.expiry:
        sess.close()
        return jsonify({"ok": False, "redirect": SUBSCRIPTION_PAGE}), 403

    if user.plan == "tier1":
        if user.remaining_uses <= 0:
            sess.close()
            return jsonify({"ok": False, "redirect": SUBSCRIPTION_PAGE}), 403
        user.remaining_uses -= 1
        sess.commit()

    tool_cache.delete(f"tool:{cid}")
    sess.close()
    return jsonify({"ok": True, "plan": user.plan})

# -----------------------------
# Admin dashboard (DB-only)
# -----------------------------
@app.route("/admin/dashboard", methods=["GET"])
def admin_dashboard():
    token = request.headers.get("X-ADMIN-TOKEN") or request.args.get("token")
    if token != os.getenv("ADMIN_DASHBOARD_TOKEN"):
        return jsonify({"ok": False}), 401

    sess = Session()
    users = sess.query(User).order_by(User.created_at.desc()).all()
    now = datetime.utcnow()

    data = []
    for u in users:
        data.append({
            "customer_id": u.customer_id,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "email": u.email,
            "trial_used": u.trial_used,
            "plan": u.plan,
            "subscription_active": bool(u.expiry and now <= u.expiry),
            "expiry": u.expiry.isoformat() if u.expiry else None,
            "remaining_uses": u.remaining_uses,
            "created_at": u.created_at.isoformat(),
        })

    sess.close()
    return jsonify({"ok": True, "users": data})


@app.route("/admin/sync-customers", methods=["POST"])
def sync_customers():
    if request.headers.get("X-ADMIN-TOKEN") != os.getenv("ADMIN_DASHBOARD_TOKEN"):
        return jsonify({"ok": False, "reason": "Unauthorized"}), 401

    # Fetch all Shopify customers and insert them into the DB
    customers = get_all_customers()  # Your existing method
    sess = Session()
    for customer in customers:
        user = sess.query(User).filter_by(customer_id=customer['id']).first()
        if not user:
            user = User(
                customer_id=customer['id'],
                email=customer['email'],
                first_name=customer['first_name'],
                last_name=customer['last_name'],
            )
            sess.add(user)
    sess.commit()
    return jsonify({"ok": True, "message": "Customers synced"})

# -----------------------------
# Health
# -----------------------------
@app.route("/")
def health():
    return jsonify({"status": "ok", "shop": settings.shop_domain})

# -----------------------------
# Run
# -----------------------------
if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", 5000)))
