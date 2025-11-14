import argparse
import json
from typing import Any

import requests

from config import load_settings


def pretty_print(label: str, value: Any) -> None:
    print(f"{label}:")
    print(json.dumps(value, indent=2, ensure_ascii=False))
    print()


def fetch_customer(settings, customer_id: str):
    url = f"https://{settings.shop_domain}/admin/api/{settings.api_version}/customers/{customer_id}.json"
    # Explicitly request marketing consent fields so we can inspect them.
    params = {
        "fields": "id,email,first_name,last_name,email_marketing_consent,email_marketing_consent_state,admin_graphql_api_id"
    }
    response = requests.get(url, headers=_headers(settings), params=params, timeout=10)
    return response.status_code, response.json() if response.content else {}


def fetch_subscription_contracts(settings, customer_id: str):
    url = f"https://{settings.shop_domain}/admin/api/{settings.api_version}/subscription_contracts.json"
    params = {"customer_id": customer_id, "status": "active"}
    response = requests.get(url, headers=_headers(settings), params=params, timeout=10)
    return response.status_code, response.json() if response.content else {}


def _headers(settings):
    return {
        "X-Shopify-Access-Token": settings.access_token,
        "Content-Type": "application/json",
    }


def main():
    parser = argparse.ArgumentParser(
        description="Inspect a Shopify customer and their active subscription contracts using current .env values."
    )
    parser.add_argument("customer_id", help="Shopify customer ID (numeric).")
    args = parser.parse_args()

    settings = load_settings()

    status, customer_payload = fetch_customer(settings, args.customer_id)
    print(f"Customer lookup status: {status}")
    if status == 200:
        pretty_print("Customer payload", customer_payload)
    else:
        print(customer_payload)

    status, contracts_payload = fetch_subscription_contracts(settings, args.customer_id)
    print(f"Subscription contracts status: {status}")
    if status == 200:
        pretty_print("Contracts payload", contracts_payload)
    else:
        print(contracts_payload)


if __name__ == "__main__":
    main()

