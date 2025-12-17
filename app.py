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
# Database migration for new columns
# -----------------------------
def migrate_database():
    """Add new columns to existing database if they don't exist."""
    from sqlalchemy import inspect, text
    
    inspector = inspect(engine)
    existing_columns = [col['name'] for col in inspector.get_columns('users')]
    
    new_columns = {
        'last_shopify_check_at': 'DATETIME',
        'last_subscription_order_id': 'BIGINT',
        'last_subscription_purchase_at': 'DATETIME',
    }
    
    with engine.connect() as conn:
        for col_name, col_type in new_columns.items():
            if col_name not in existing_columns:
                try:
                    conn.execute(text(f'ALTER TABLE users ADD COLUMN {col_name} {col_type}'))
                    conn.commit()
                    print(f"Added column: {col_name}")
                except Exception as e:
                    print(f"Column {col_name} might already exist or error: {e}")

# Run migration on startup
try:
    migrate_database()
except Exception as e:
    print(f"Migration error (may be safe to ignore): {e}")

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
    """
    Refresh a single customer's subscription status from Shopify.
    This is much faster than syncing all customers/orders.
    """
    now = datetime.utcnow()

    # Skip refresh if not forced and recent check is valid
    try:
        last_check = getattr(user, 'last_shopify_check_at', None)
        if not force and last_check:
            if (now - last_check).total_seconds() <= max_age_seconds:
                return
    except Exception:
        pass  # Column might not exist yet

    # Fetch customer orders from Shopify
    try:
        orders = get_customer_orders(int(user.customer_id), limit=50)
    except Exception as e:
        print(f"Error fetching orders for customer {user.customer_id}: {e}")
        return

    latest = None

    for o in orders:
        created = o.get("created_at")
        if not created:
            continue
        try:
            created_dt = _parse_shopify_dt(created)
        except Exception:
            continue
        for li in o.get("line_items", []):
            pid = li.get("product_id")
            if pid in SUBSCRIPTION_PRODUCT_IDS:
                if not latest or created_dt > latest[0]:
                    latest = (created_dt, o.get("id"), pid)

    # Update last check time (safely)
    try:
        user.last_shopify_check_at = now
    except Exception:
        pass

    if not latest:
        user.plan = "none"
        user.expiry = None
        user.remaining_uses = None
        sess.commit()
        return

    created_dt, order_id, pid = latest

    # Check if this is a new order
    try:
        last_order = getattr(user, 'last_subscription_order_id', None)
        is_new_order = last_order != order_id
    except Exception:
        is_new_order = True

    if pid == TIER1_PRODUCT_ID:
        user.plan = "tier1"
        if is_new_order:
            user.remaining_uses = TIER1_USES
    elif pid == TIER2_PRODUCT_ID:
        user.plan = "tier2"
        user.remaining_uses = None
    elif pid == PRO_PRODUCT_ID:
        user.plan = "pro"
        user.remaining_uses = None

    user.plan_product_id = pid
    user.expiry = created_dt + timedelta(days=ACCESS_DAYS)
    
    # Update tracking fields (safely)
    try:
        user.last_subscription_order_id = order_id
        user.last_subscription_purchase_at = created_dt
    except Exception:
        pass
    
    sess.commit()

# -----------------------------
# Tool entry - FAST endpoint for initial tool access
# -----------------------------
@app.route("/proxy/tool", methods=["GET"])
def proxy_tool():
    """
    Main endpoint called by the Vercel tool.
    This is a FAST endpoint - uses cache and local DB only.
    
    IMPORTANT: This endpoint does NOT mark trial_used.
    trial_used is only marked in /proxy/validate-submission
    """
    customer_id = request.args.get("customer_id") or request.args.get("logged_in_customer_id")
    if not customer_id:
        return jsonify({"ok": False, "reason": "missing_customer_id", "redirect": "/account/login"}), 400

    # Validate format (13-digit numeric)
    if not customer_id.isdigit() or len(customer_id) != 13:
        return jsonify({"ok": False, "reason": "invalid_customer_id", "message": "Customer id must be 13 digits."}), 400

    cid = int(customer_id)
    cache_key = f"tool:{cid}"
    
    # Check TTL cache first for fast response
    cached = tool_cache.get(cache_key)
    if cached:
        return jsonify(cached), 200

    sess = Session()
    user = sess.query(User).filter_by(customer_id=cid).first()

    # If we don't have a record for this customer, create one for free trial access
    if not user:
        user = User(
            customer_id=cid,
            plan="none",
            plan_product_id=None,
            expiry=None,
            remaining_uses=None,
            trial_used=False,
        )
        sess.add(user)
        sess.commit()

    # Backfill user profile info from Shopify (only if missing, non-blocking)
    if user.email is None or user.first_name is None or user.last_name is None:
        try:
            info = get_customer_basic_info(cid)
            if info:
                user.email = user.email or info.get("email")
                user.first_name = user.first_name or info.get("first_name")
                user.last_name = user.last_name or info.get("last_name")
                sess.commit()
        except Exception:
            pass  # Non-critical, don't block user flow

    # Admin bypass - unlimited access
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
        return jsonify(resp), 200

    # Check subscription status for response info (but don't block access)
    now = datetime.utcnow()
    has_active_subscription = (
        user.plan and 
        user.plan != "none" and 
        user.expiry and 
        now <= user.expiry
    )

    # Get plan info if subscribed
    plan_info = None
    if has_active_subscription:
        if user.plan == "tier1":
            plan_info = {"plan": "tier1", "remaining_uses": user.remaining_uses}
        else:
            plan_info = {"plan": user.plan}

    # Build response - always allow tool access
    # NOTE: trial_used is NOT modified here - only in validate-submission
    resp = {
        "ok": True,
        "trial_used": user.trial_used,
        "show_trial_banner": not user.trial_used,
        "has_subscription": has_active_subscription,
        "plan": plan_info.get("plan") if plan_info else None,
        "remaining_uses": plan_info.get("remaining_uses") if plan_info else None,
        "tool_url": TOOL_URL,
    }

    sess.close()
    
    # Cache response for 2 minutes
    tool_cache.set(cache_key, resp, 120)
    
    return jsonify(resp), 200

# -----------------------------
# Submission validation
# -----------------------------
@app.route("/proxy/validate-submission", methods=["GET"])
def validate_submission():
    """
    Validate if user can proceed to next step (called from Questionnaire Next button).
    FLOW:
    1. Admin users: Always allowed (unlimited access)
    2. First time (trial not used): Allow and mark trial as used - FREE TRIAL
    3. After trial used: Check subscription status from Shopify
    """
    try:
        customer_id = request.args.get("customer_id") or request.args.get("logged_in_customer_id")
        if not customer_id:
            return jsonify({"ok": False, "reason": "missing_customer_id"}), 400

        if not customer_id.isdigit() or len(customer_id) != 13:
            return jsonify({"ok": False, "reason": "invalid_customer_id"}), 400

        cid = int(customer_id)
        sess = Session()
        
        try:
            user = sess.query(User).filter_by(customer_id=cid).first()

            if not user:
                user = User(customer_id=cid, trial_used=False, plan="none")
                sess.add(user)
                sess.commit()

            # ============================================================
            # ADMIN CHECK - Fetch email first if missing, then check admin
            # ============================================================
            if not user.email:
                try:
                    info = get_customer_basic_info(cid)
                    if info and info.get("email"):
                        user.email = info.get("email")
                        user.first_name = info.get("first_name")
                        user.last_name = info.get("last_name")
                        sess.commit()
                except Exception as e:
                    print(f"Error fetching customer info: {e}")

            # Admin bypass - unlimited access, no expiry
            if is_admin(user):
                tool_cache.delete(f"tool:{cid}")
                sess.close()
                return jsonify({
                    "ok": True, 
                    "plan": "admin",
                    "is_admin": True,
                    "message": "Admin access granted"
                }), 200

            # ============================================================
            # CRITICAL: FREE TRIAL FLOW
            # If trial not used yet, allow submission and mark trial as used
            # This is the FREE first use - no subscription required
            # ============================================================
            if not user.trial_used:
                user.trial_used = True
                sess.commit()
                
                # Invalidate cache so next /proxy/tool call shows updated trial_used status
                tool_cache.delete(f"tool:{cid}")
                
                sess.close()
                return jsonify({
                    "ok": True,
                    "trial_just_used": True,
                    "message": "Trial submission allowed"
                }), 200

            # ============================================================
            # TRIAL ALREADY USED - Check subscription from Shopify
            # ============================================================
            
            # Force refresh if user has no active subscription in DB
            # (they might have just purchased one)
            now = datetime.utcnow()
            force_refresh = (
                user.plan == "none" or 
                user.plan is None or 
                not user.expiry or 
                now > user.expiry
            )
            
            try:
                refresh_customer_subscription(sess, user, max_age_seconds=300, force=force_refresh)
            except Exception as exc:
                print(f"Subscription refresh error for {cid}: {exc}")
                # Continue with existing DB data if refresh fails

            # Re-check subscription status after refresh
            has_active_subscription = (
                user.plan and 
                user.plan != "none" and 
                user.expiry and 
                now <= user.expiry
            )

            if not has_active_subscription:
                sess.close()
                return jsonify({
                    "ok": False,
                    "reason": "no_active_subscription",
                    "redirect": SUBSCRIPTION_PAGE,
                    "message": "Please subscribe to continue"
                }), 403

            # User has active subscription - check tier1 usage limits
            if user.plan == "tier1":
                if user.remaining_uses is None:
                    sess.close()
                    return jsonify({
                        "ok": False,
                        "reason": "subscription_data_error",
                        "message": "Tier1 user missing remaining_uses in DB"
                    }), 500

                if user.remaining_uses <= 0:
                    sess.close()
                    return jsonify({
                        "ok": False,
                        "reason": "tier1_exhausted",
                        "redirect": SUBSCRIPTION_PAGE,
                        "message": "You have exhausted your usage limit. Please upgrade."
                    }), 403

                # Decrement usage for tier1
                user.remaining_uses -= 1
                sess.commit()
                remaining = user.remaining_uses
                
                # Invalidate cache
                tool_cache.delete(f"tool:{cid}")
                
                sess.close()
                return jsonify({
                    "ok": True,
                    "plan": "tier1",
                    "remaining_uses": remaining
                }), 200

            # tier2 or pro: unlimited access
            tool_cache.delete(f"tool:{cid}")
            sess.close()
            return jsonify({
                "ok": True,
                "plan": user.plan
            }), 200
            
        except Exception as e:
            sess.close()
            raise e
            
    except Exception as e:
        print(f"Validation error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "ok": False,
            "reason": "server_error",
            "message": str(e)
        }), 500

# -----------------------------
# Admin dashboard endpoints
# -----------------------------
@app.route("/admin/dashboard", methods=["GET"])
def admin_dashboard():
    """
    Admin dashboard - returns all users with subscription and trial status.
    Protected by ADMIN_DASHBOARD_TOKEN.
    """
    token = request.headers.get("X-ADMIN-TOKEN") or request.args.get("token")
    expected_token = os.getenv("ADMIN_DASHBOARD_TOKEN")
    
    if not expected_token or token != expected_token:
        return jsonify({"ok": False, "reason": "unauthorized"}), 401

    sess = Session()
    users = sess.query(User).order_by(User.created_at.desc()).all()
    now = datetime.utcnow()

    data = []
    for u in users:
        subscription_active = bool(
            u.plan and 
            u.plan != "none" and 
            u.expiry and 
            now <= u.expiry
        )
        
        # Safely get new columns that might not exist
        try:
            last_check = u.last_shopify_check_at.isoformat() if u.last_shopify_check_at else None
        except Exception:
            last_check = None
        
        data.append({
            "customer_id": u.customer_id,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "email": u.email,
            "trial_used": u.trial_used,
            "plan": u.plan,
            "subscription_active": subscription_active,
            "expiry": u.expiry.isoformat() if u.expiry else None,
            "remaining_uses": u.remaining_uses,
            "last_shopify_check_at": last_check,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "updated_at": u.updated_at.isoformat() if u.updated_at else None,
        })

    sess.close()
    
    # Summary stats
    total_users = len(data)
    trial_not_used = sum(1 for u in data if not u["trial_used"])
    trial_used_count = sum(1 for u in data if u["trial_used"])
    active_subscribers = sum(1 for u in data if u["subscription_active"])
    tier1_users = sum(1 for u in data if u["plan"] == "tier1" and u["subscription_active"])
    tier2_users = sum(1 for u in data if u["plan"] == "tier2" and u["subscription_active"])
    pro_users = sum(1 for u in data if u["plan"] == "pro" and u["subscription_active"])
    
    return jsonify({
        "ok": True,
        "summary": {
            "total_users": total_users,
            "trial_not_used": trial_not_used,
            "trial_used_count": trial_used_count,
            "active_subscribers": active_subscribers,
            "tier1_users": tier1_users,
            "tier2_users": tier2_users,
            "pro_users": pro_users,
        },
        "users": data
    })


@app.route("/admin/sync-customers", methods=["POST"])
def admin_sync_customers():
    """
    Sync all customers from Shopify to local DB (profile info only).
    Protected by ADMIN_DASHBOARD_TOKEN.
    """
    token = request.headers.get("X-ADMIN-TOKEN") or request.args.get("token")
    expected_token = os.getenv("ADMIN_DASHBOARD_TOKEN")
    
    if not expected_token or token != expected_token:
        return jsonify({"ok": False, "reason": "unauthorized"}), 401

    try:
        customers = get_all_customers()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    
    sess = Session()
    created_count = 0
    updated_count = 0
    
    for customer in customers:
        cid = customer.get("id")
        if not cid:
            continue
            
        user = sess.query(User).filter_by(customer_id=cid).first()
        if not user:
            user = User(
                customer_id=cid,
                email=customer.get("email"),
                first_name=customer.get("first_name"),
                last_name=customer.get("last_name"),
                plan="none",
                trial_used=False,
            )
            sess.add(user)
            created_count += 1
        else:
            # Update profile info if available
            if customer.get("email"):
                user.email = customer.get("email")
            if customer.get("first_name"):
                user.first_name = customer.get("first_name")
            if customer.get("last_name"):
                user.last_name = customer.get("last_name")
            updated_count += 1
    
    sess.commit()
    sess.close()
    
    return jsonify({
        "ok": True,
        "message": "Customers synced",
        "created": created_count,
        "updated": updated_count,
    })


@app.route("/admin/sync-subscriptions", methods=["POST"])
def admin_sync_subscriptions():
    """
    Full sync of subscription status from Shopify orders.
    This is a heavy operation - fetches ALL orders from Shopify.
    Protected by ADMIN_DASHBOARD_TOKEN.
    """
    token = request.headers.get("X-ADMIN-TOKEN") or request.args.get("token")
    expected_token = os.getenv("ADMIN_DASHBOARD_TOKEN")
    
    if not expected_token or token != expected_token:
        return jsonify({"ok": False, "reason": "unauthorized"}), 401

    try:
        # Fetch all orders from Shopify
        orders = get_all_orders()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    
    sess = Session()
    now = datetime.utcnow()
    
    # Find latest subscription purchase per customer
    # Check ALL line items in each order, not just the first one
    latest_purchase = {}  # cid -> (product_id, created_dt, order_id)
    
    for o in orders:
        cid = o.get("customer_id")
        created = o.get("created_at")
        order_id = o.get("order_id")
        if not cid or not created:
            continue
        
        # Check all product IDs in the order
        all_product_ids = o.get("line_item_product_ids", [])
        # Fallback to first item if line_item_product_ids not available
        if not all_product_ids:
            first_pid = o.get("line_item_0_product_id")
            if first_pid:
                all_product_ids = [first_pid]
        
        # Find subscription product in this order
        subscription_pid = None
        for pid in all_product_ids:
            try:
                pid_int = int(pid)
                if pid_int in SUBSCRIPTION_PRODUCT_IDS:
                    subscription_pid = pid_int
                    break
            except (TypeError, ValueError):
                continue
        
        if not subscription_pid:
            continue
            
        # Parse created datetime
        try:
            created_dt = _parse_shopify_dt(created)
        except Exception:
            created_dt = datetime.utcnow()

        prev = latest_purchase.get(cid)
        if not prev or created_dt > prev[1]:
            latest_purchase[cid] = (subscription_pid, created_dt, order_id)

    # Apply plan info to users
    updated_count = 0
    for cid, (pid, created_dt, order_id) in latest_purchase.items():
        user = sess.query(User).filter_by(customer_id=cid).first()
        if not user:
            # Create user if not exists
            user = User(customer_id=cid, plan="none", trial_used=False)
            sess.add(user)

        # Check if this is a new order (for tier1 usage reset)
        is_new_order = user.last_subscription_order_id != order_id

        if pid == TIER1_PRODUCT_ID:
            user.plan = "tier1"
            if is_new_order or user.remaining_uses is None:
                user.remaining_uses = TIER1_USES
        elif pid == TIER2_PRODUCT_ID:
            user.plan = "tier2"
            user.remaining_uses = None
        elif pid == PRO_PRODUCT_ID:
            user.plan = "pro"
            user.remaining_uses = None

        user.plan_product_id = pid
        user.expiry = created_dt + timedelta(days=ACCESS_DAYS)
        
        # Safely set new columns
        try:
            user.last_subscription_order_id = order_id
            user.last_subscription_purchase_at = created_dt
            user.last_shopify_check_at = now
        except Exception:
            pass
            
        updated_count += 1

    sess.commit()
    total_users = sess.query(User).count()
    sess.close()
    
    return jsonify({
        "ok": True,
        "message": "Subscriptions synced from Shopify orders",
        "total_users": total_users,
        "updated_subscriptions": updated_count,
    })

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
