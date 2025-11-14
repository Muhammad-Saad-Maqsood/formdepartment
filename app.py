import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from flask import Flask, abort, jsonify, make_response, render_template, request, redirect

try:
    from .config import load_settings
    from .shopify_client import ShopifyAdminClient, verify_app_proxy_signature, verify_webhook_signature
except ImportError:  # pragma: no cover - fallback for direct execution
    from config import load_settings  # type: ignore
    from shopify_client import ShopifyAdminClient, verify_app_proxy_signature, verify_webhook_signature  # type: ignore

app = Flask(__name__, template_folder="templates")

settings = load_settings()
shopify = ShopifyAdminClient(settings)

WEBHOOK_STORAGE_DIR = Path(__file__).resolve().parent / "data" / "webhooks"
WEBHOOK_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_WEBHOOK_TOPICS = {
    "subscription_contracts/create",
    "subscription_contracts/update",
    "subscription_contracts/activate",
    "subscription_contracts/cancel",
    "app/uninstalled",
}


@app.route("/")
def healthcheck():
    return jsonify({"status": "ok", "shop": settings.shop_domain})


@app.route("/proxy/tool", methods=["GET"])
def proxy_tool():
    params = request.args.to_dict(flat=True)

    # DEBUG: Print what we're receiving
    print(f"🔍 Received parameters: {list(params.keys())}")
    print(f"🔍 Customer ID: {params.get('logged_in_customer_id')}")
    print(f"🔍 Signature present: {'signature' in params}")


    # if not verify_app_proxy_signature(params, settings.shared_secret):
    #     abort(403)

    customer_id = params.get("logged_in_customer_id")
    print("🧭 Logged in customer ID:", customer_id)

    if not customer_id:
        return _build_response(
            status_code=401,
            json_body={
                "authorized": False,
                "reason": "login_required",
                "message": "Please log in to your Shopify account to continue.",
            },
            html_template="login_required.html",
        )

    try:
        access = shopify.check_customer_access(customer_id)
        print(f"🔍 Access check: allowed={access.allowed}, reason={access.reason}")
    except RuntimeError as exc:
        print(f"❌ Error checking access: {exc}")
        return _build_response(
            status_code=502,
            json_body={
                "authorized": False,
                "reason": "shopify_error",
                "message": str(exc),
            },
            html_template="error.html",
            template_context={"error": str(exc)},
        )

    if not access.allowed:
        print(f"🔍 Redirecting non-subscribed user to subscription page")
        subscription_plan_url = "https://formdepartment.com/pages/about?view=subscription-plans"
        return redirect(subscription_plan_url)
    
    return _build_response(
        status_code=200,
        json_body={
            "authorized": True,
            "status": access.status,
            "reason": access.reason,
            "customer_id": customer_id,
            "trial": access.trial,
            "subscription_count": access.subscription_count,
            "tool_url": settings.tool_app_url,
        },
        html_template="authorized.html",
        template_context={
            "tool_url": settings.tool_app_url,
            "customer_id": customer_id,
            "status": access.status,
            "trial": access.trial,
        },
    )


@app.route("/webhooks/<path:topic>", methods=["POST"])
def webhook_handler(topic: str):
    canonical_topic = topic.replace("\\", "/").lower()
    header_topic = (request.headers.get("X-Shopify-Topic") or "").lower()
    if header_topic and header_topic != canonical_topic:
        # Protect against spoofed URLs: topic must match header.
        abort(400)

    if header_topic and header_topic not in ALLOWED_WEBHOOK_TOPICS:
        abort(400, description=f"Unsupported webhook topic: {header_topic}")

    secret = settings.webhook_shared_secret or settings.shared_secret
    body = request.get_data()
    header_hmac = request.headers.get("X-Shopify-Hmac-Sha256")

    if not verify_webhook_signature(body, header_hmac, secret):
        abort(401)

    payload = request.get_json(silent=True)
    if payload is None:
        payload = json.loads(body.decode("utf-8")) if body else {}

    _persist_webhook_event(header_topic or canonical_topic, payload, request.headers)

    return ("", 200)


def _persist_webhook_event(topic: str, payload: Dict, headers: Dict) -> None:
    entry = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "topic": topic,
        "payload": payload,
        "shop_domain": headers.get("X-Shopify-Shop-Domain"),
    }

    target_file = WEBHOOK_STORAGE_DIR / f"{topic.replace('/', '_')}.jsonl"
    with target_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry))
        handle.write("\n")


def _build_response(
    status_code: int,
    json_body: Dict,
    html_template: str,
    template_context: Optional[Dict[str, object]] = None,
):
    template_context = template_context or {}

    best = request.accept_mimetypes.best_match(
        ["application/json", "text/html"], default="application/json"
    )

    if best == "application/json":
        response = jsonify(json_body)
    else:
        response = make_response(render_template(html_template, **template_context))

    response.status_code = status_code
    return response


@app.route("/test-simple-redirect")
def test_simple_redirect():
    """Simple test to verify redirection works"""
    customer_id = "8873545400495"  # Non-subscribed customer
    subscription_plan_url = "https://formdepartment.com/pages/about?view=subscription-plans"
    return redirect(subscription_plan_url)


if __name__ == "__main__":
    app.run(port=5000, debug=True)