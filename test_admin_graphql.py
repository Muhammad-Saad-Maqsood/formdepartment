import argparse
import json

import requests

from config import load_settings


def run_query(query: str) -> None:
    settings = load_settings()

    url = f"https://{settings.shop_domain}/admin/api/{settings.api_version}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": settings.access_token,
        "Content-Type": "application/json",
    }

    response = requests.post(url, headers=headers, json={"query": query}, timeout=10)

    print(f"Status: {response.status_code}")
    try:
        data = response.json()
    except json.JSONDecodeError:
        print("Response could not be parsed as JSON:")
        print(response.text)
        return

    print(json.dumps(data, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send a test GraphQL query to the Shopify Admin API using the current .env values."
    )
    parser.add_argument(
        "--query",
        default="query { shop { name myshopifyDomain } }",
        help="GraphQL query string to execute. Defaults to a simple shop info query.",
    )
    args = parser.parse_args()

    run_query(args.query)


if __name__ == "__main__":
    main()

