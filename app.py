# app.py
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask_cors import CORS 

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

# CORS setup
CORS(app, origins=[
    "https://capsule-builder-qhzx.vercel.app",
    "https://formdepartment.com"
])

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

    # NOTE: For this entrypoint we want the check to be **fast**.
    # Do NOT run a full Shopify sync here; just use the local DB to
    # determine if the user has already used their free trial.
    #
    # The heavier sync_from_shopify() call is still used on the
    # `/proxy/validate-submission` endpoint where we must enforce
    # up‑to‑date subscription status.

    # Validate this customer in local DB
    cid = int(customer_id)
    sess = Session()
    user = sess.query(User).filter_by(customer_id=cid).first()

    # If we don't yet have a record for this customer, create a minimal
    # one locally so they can immediately access their free trial.
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

    # Always allow access to tool
    # Show trial banner if trial hasn't been used yet (trial_used will be marked on first Schedule Call click)
    
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
    
    # We’re done mutating the user; safe to close the session now.
    sess.close()
    
    # Always return ok=True to allow tool access.
    # The frontend only needs to know whether the trial was already used
    # and where to send the user.
    return jsonify(
        {
            "ok": True,
            "trial_used": user.trial_used,
            "show_trial_banner": not user.trial_used,
            "has_subscription": has_active_subscription,
            "plan": plan_info.get("plan") if plan_info else None,
            "remaining_uses": plan_info.get("remaining_uses") if plan_info else None,
            "tool_url": TOOL_URL,
        }
    ), 200


@app.route("/proxy/validate-submission", methods=["GET"])
def validate_submission():
    """
    Validate if user can proceed to next step (called from Questionnaire Next button).
    - First time (trial not used): Allow and mark trial as used
    - After trial used: Check subscription status
    """
    customer_id = request.args.get("customer_id") or request.args.get("logged_in_customer_id")
    if not customer_id:
        return jsonify({"ok": False, "reason": "missing_customer_id"}), 400

    if not customer_id.isdigit() or len(customer_id) != 13:
        return jsonify({"ok": False, "reason": "invalid_customer_id"}), 400

    # Refresh DB
    try:
        sync_from_shopify()
    except Exception as exc:
        print("Sync error:", exc)
        return jsonify({"ok": False, "reason": "shopify_sync_failed", "message": str(exc)}), 500

    cid = int(customer_id)
    sess = Session()
    user = sess.query(User).filter_by(customer_id=cid).first()

    if not user:
        sess.close()
        return jsonify({"ok": False, "reason": "not_found"}), 401

    # If trial not used yet, allow submission and mark trial as used
    if not user.trial_used:
        user.trial_used = True
        sess.add(user)
        sess.commit()
        sess.close()
        return jsonify({
            "ok": True,
            "trial_just_used": True,
            "message": "Trial submission allowed"
        }), 200

    # Trial already used - check subscription
    now = datetime.utcnow()
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
        sess.add(user)
        sess.commit()
        remaining = user.remaining_uses
        sess.close()

        return jsonify({
            "ok": True,
            "plan": "tier1",
            "remaining_uses": remaining
        }), 200

    # tier2 or pro: unlimited access
    sess.close()
    return jsonify({
        "ok": True,
        "plan": user.plan
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
