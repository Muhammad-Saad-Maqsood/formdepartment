import json
import sys

import requests

from config import load_settings


def main() -> int:
    try:
        settings = load_settings()
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    url = f"https://{settings.shop_domain}/admin/api/{settings.api_version}/graphql.json"

    headers = {
        "X-Shopify-Access-Token": settings.access_token,
        "Content-Type": "application/json",
    }
    payload = {"query": "{ shop { id name myshopifyDomain } }"}

    try:
        response = requests.post(url, headers=headers, json={**payload}, timeout=10)
    except requests.RequestException as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 1

    print(f"HTTP {response.status_code}")

    text = response.text.strip()
    if not text:
        print("No response body.")
        return 1

    try:
        data = response.json()
    except json.JSONDecodeError:
        print("Response not JSON:")
        print(text)
        return 1

    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

