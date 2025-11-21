# app.py
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

from flask import Flask, request, jsonify, redirect
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import your models (models.py file shown below)
from models import Base, User

# Import your Shopify helper functions (your existing file test_shopify_api.py)
# It must expose get_all_customers() and get_all_orders()
from test_shopify_api import get_all_customers, get_all_orders

# Load .env
load_dotenv()
# Load settings from config.py
from config import load_settings
settings = load_settings()

# ---- Subscription product IDs (integers) ----
TIER1_PRODUCT_ID = 8424668299439
TIER2_PRODUCT_ID = 8424683241647
PRO_PRODUCT_ID   = 8424226160815

TIER1_USES = 10
ACCESS_DAYS = 30

# URLS from settings
SUBSCRIPTION_PAGE = settings.plan_page_url or "https://formdepartment.com/pages/about?view=subscription-plans"
TOOL_URL = settings.tool_app_url

# ---- Flask + DB setup ----
app = Flask(__name__)

DB_PATH = os.getenv("SQLITE_PATH", "sqlite:///shopify_access.db")
# create_engine accepts sqlite:///path; here we accept absolute or default relative file
engine = create_engine(DB_PATH, connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


# -----------------------
# Utility: upsert user
# -----------------------
def upsert_user_from_shopify(sess, customer):
    """
    customer: dict from get_all_customers() with keys id, email, first_name, last_name
    """
    cid = customer.get("id")
    if not cid:
        return None

    user = sess.query(User).filter_by(customer_id=cid).first()
    if not user:
        user = User(
            customer_id=cid,
            email=customer.get("email"),
            first_name=customer.get("first_name"),
            last_name=customer.get("last_name"),
            plan="none",
            plan_product_id=None,
            expiry=None,
            remaining_uses=None,
        )
        sess.add(user)
    else:
        user.email = customer.get("email") or user.email
        user.first_name = customer.get("first_name") or user.first_name
        user.last_name = customer.get("last_name") or user.last_name
        sess.add(user)
    return user


# -----------------------
# Sync logic (orders -> assign plans)
# -----------------------
def sync_from_shopify():
    """
    Fetch customers + orders from Shopify and update the local SQLite DB.
    - For each customer, create or update a User row.
    - For orders, find latest order per customer that contains one of the subscription product IDs.
    - Use that order's created_at to set expiry = created_at + ACCESS_DAYS.
    - For tier1 reset remaining_uses = TIER1_USES.
    - For tier2/tier3 remaining_uses = None (unlimited).
    """
    sess = Session()
    try:
        customers = get_all_customers()
    except Exception as e:
        sess.close()
        raise RuntimeError(f"Failed to fetch customers from Shopify: {e}")

    try:
        orders = get_all_orders()
    except Exception as e:
        sess.close()
        raise RuntimeError(f"Failed to fetch orders from Shopify: {e}")

    # 1) Ensure user rows exist / update basic profile
    for c in customers:
        upsert_user_from_shopify(sess, c)
    sess.commit()

    # 2) find latest subscription purchase per customer
    latest_purchase = {}  # cid -> (product_id, created_dt)
    for o in orders:
        cid = o.get("customer_id")
        pid = o.get("line_item_0_product_id")
        created = o.get("order_created_at") or o.get("created_at")
        if not cid or not pid:
            continue
        # product matching must use ints
        try:
            pid_int = int(pid)
        except Exception:
            continue
        if pid_int not in {TIER1_PRODUCT_ID, TIER2_PRODUCT_ID, PRO_PRODUCT_ID}:
            continue
        # parse created
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except Exception:
            # fallback
            created_dt = datetime.utcnow()

        prev = latest_purchase.get(cid)
        if not prev or created_dt > prev[1]:
            latest_purchase[cid] = (pid_int, created_dt)

    # 3) apply plan info to users
    for cid, (pid, created_dt) in latest_purchase.items():
        user = sess.query(User).filter_by(customer_id=cid).first()
        if not user:
            continue

        if pid == TIER1_PRODUCT_ID:
            user.plan = "tier1"
            if user.remaining_uses is None or user.plan != "tier1":
                user.remaining_uses = TIER1_USES
        elif pid == TIER2_PRODUCT_ID:
            user.plan = "tier2"
            user.remaining_uses = None
        elif pid == PRO_PRODUCT_ID:
            user.plan = "pro"
            user.remaining_uses = None

        user.plan_product_id = pid
        user.expiry = created_dt + timedelta(days=ACCESS_DAYS)
        sess.add(user)

    sess.commit()
    count = sess.query(User).count()
    sess.close()
    return {"synced_users": count, "updated_subscriptions": len(latest_purchase)}


# ---------------------------------------------------
# Admin endpoint to trigger sync manually (protected)
# ---------------------------------------------------
@app.route("/admin/sync_shopify", methods=["POST"])
def admin_sync_shopify():
    token = request.headers.get("X-SYNC-TOKEN")
    if token != os.getenv("SYNC_TOKEN", ""):
        return jsonify({"ok": False, "reason": "unauthorized"}), 401
    try:
        result = sync_from_shopify()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, **result})


# ---------------------------------------------------
# Main endpoint called by Vercel tool
# This endpoint automatically refreshes the db then validates user
# ---------------------------------------------------
@app.route("/proxy/tool", methods=["GET"])
def proxy_tool():
    # Accept either param name used in earlier routes
    customer_id = request.args.get("customer_id") or request.args.get("logged_in_customer_id")
    if not customer_id:
        # No id provided — instruct frontend to redirect to login
        return jsonify({"ok": False, "reason": "missing_customer_id", "redirect": "/account/login"}), 400

    # Validate format (13-digit numeric)
    if not customer_id.isdigit() or len(customer_id) != 13:
        return jsonify({"ok": False, "reason": "invalid_customer_id", "message": "Customer id must be 13 digits."}), 400

    # 1) Refresh DB using Shopify data (synchronous)
    try:
        sync_from_shopify()
    except Exception as exc:
        # If Shopify fetch fails, be conservative and deny access (or choose to allow fallback)
        print("Sync error:", exc)
        return jsonify({"ok": False, "reason": "shopify_sync_failed", "message": str(exc)}), 500

    # 2) Validate this customer in local DB
    cid = int(customer_id)
    sess = Session()
    user = sess.query(User).filter_by(customer_id=cid).first()

    if not user:
        sess.close()
        return jsonify({"ok": False, "reason": "not_found", "redirect": "/account/login"}), 401

    now = datetime.utcnow()

    # expired or no plan
    if not user.plan or user.plan == "none" or not user.expiry or now > user.expiry:
        sess.close()
        return jsonify({"ok": False, "reason": "no_active_subscription", "redirect": SUBSCRIPTION_PAGE}), 403

    # tier1: enforce usage count
    if user.plan == "tier1":

        # remaining_uses MUST already exist in DB
        if user.remaining_uses is None:
            sess.close()
            return jsonify({
                "ok": False,
                "reason": "subscription_data_error",
                "message": "Tier1 user missing remaining_uses in DB"
            }), 500

        # if exhausted
        if user.remaining_uses <= 0:
            sess.close()
            return jsonify({
                "ok": False,
                "reason": "tier1_exhausted",
                "redirect": SUBSCRIPTION_PAGE
            }), 403

        # decrement usage
        user.remaining_uses -= 1
        sess.add(user)
        sess.commit()
        remaining = user.remaining_uses
        sess.close()

        return jsonify({
            "ok": True,
            "plan": "tier1",
            "remaining_uses": remaining,
            "tool_url": TOOL_URL
        }), 200


    # tier2 or tier3: unlimited access
    sess.close()
    return jsonify({
        "ok": True,
        "plan": user.plan,
        "tool_url": TOOL_URL
    }), 200


# ----------------------------------------------------------------
# Simple status endpoint (health)
# ----------------------------------------------------------------
@app.route("/")
def health():
    return jsonify({"status": "ok", "shop": settings.shop_domain})


# Run app
if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", 5000)))
