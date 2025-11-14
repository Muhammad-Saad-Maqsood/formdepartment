import argparse
import hmac
import hashlib
import time
from urllib.parse import urlencode

from config import load_settings
from shopify_client import verify_app_proxy_signature


def build_signature(shared_secret: str, customer_id: str, timestamp: str) -> str:
    message = f"logged_in_customer_id={customer_id}&timestamp={timestamp}"
    digest = hmac.new(shared_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a valid Shopify App Proxy query string for local testing."
    )
    parser.add_argument(
        "--customer-id",
        required=True,
        help="Shopify numeric customer ID (e.g. 8843450000000)",
    )
    parser.add_argument(
        "--timestamp",
        help="Unix timestamp in seconds (defaults to current time).",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:5000/proxy/tool",
        help="Base URL for the proxy endpoint.",
    )
    args = parser.parse_args()

    settings = load_settings()
    timestamp = args.timestamp or str(int(time.time()))

    signature = build_signature(settings.shared_secret, args.customer_id, timestamp)

    params = {
        "logged_in_customer_id": args.customer_id,
        "timestamp": timestamp,
        "signature": signature,
    }

    is_valid = verify_app_proxy_signature(params, settings.shared_secret)

    query_string = urlencode(params)
    full_url = f"{args.base_url}?{query_string}"

    print("Generated parameters:")
    for key, value in params.items():
        print(f"  {key}={value}")
    print()
    print(f"Signature valid according to backend: {is_valid}")
    print(f"Full URL: {full_url}")


if __name__ == "__main__":
    main()

