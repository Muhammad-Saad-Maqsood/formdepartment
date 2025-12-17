import os
import re
import time
import requests
import pandas as pd
from dotenv import load_dotenv
from config import load_settings


SUBSCRIPTION_PRODUCT_IDS = {
    8424668299439,  # Tier 1
    8424683241647,  # Tier 2
    8424226160815   # Pro Tier
}


# =====================================================
# 1️⃣ Load Environment Variables
# =====================================================
load_dotenv()

SHOP_NAME = os.getenv("SHOP_NAME")
ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")

HEADERS = {
    "X-Shopify-Access-Token": ACCESS_TOKEN,
    "Content-Type": "application/json"
}

# =====================================================
# 2️⃣ Fetch ALL CUSTOMERS (Paginated)
# =====================================================

def get_all_customers():
    print("📦 Fetching customers (filtered fields)...")
    all_customers = []

    url = f"https://{SHOP_NAME}.myshopify.com/admin/api/2024-10/customers.json"
    params = {"limit": 250}
    next_page_info = None

    while True:
        if next_page_info:
            params["page_info"] = next_page_info

        response = requests.get(url, headers=HEADERS, params=params)
        if response.status_code != 200:
            print(f"❌ Error {response.status_code}: {response.text}")
            break

        data = response.json().get("customers", [])

        # Extract ONLY required fields
        for c in data:
            all_customers.append({
                "id": c.get("id"),
                "first_name": c.get("first_name"),
                "last_name": c.get("last_name"),
                "email": c.get("email"),
                "admin_graphql_api_id": c.get("admin_graphql_api_id")
            })

        # Pagination
        link_header = response.headers.get("Link", "")
        if 'rel="next"' in link_header:
            match = re.search(r'page_info=([^&>]+)', link_header)
            next_page_info = match.group(1)
        else:
            break

    print(f"✅ Total customers fetched: {len(all_customers)}")
    return all_customers




# =====================================================
# 3️⃣ Fetch ALL ORDERS (Payments Data)
# =====================================================

def get_all_orders():
    print("💰 Fetching orders (filtered fields)...")
    all_orders = []

    url = f"https://{SHOP_NAME}.myshopify.com/admin/api/2024-10/orders.json"
    params = {"limit": 250, "status": "any"}
    next_page_info = None

    while True:
        if next_page_info:
            params["page_info"] = next_page_info

        response = requests.get(url, headers=HEADERS, params=params)
        if response.status_code != 200:
            print(f"❌ Error {response.status_code}: {response.text}")
            break

        data = response.json().get("orders", [])

        for o in data:
            line_items = o.get("line_items", [])

            all_orders.append({
                "order_id": o.get("id"),
                "created_at": o.get("created_at"),
                "customer_id": o.get("customer", {}).get("id"),
                "customer_first_name": o.get("customer", {}).get("first_name"),
                "customer_last_name": o.get("customer", {}).get("last_name"),
                "contact_email": o.get("contact_email"),
                "line_item_0_product_id": line_items[0]["product_id"] if line_items else None,
                # Keep ALL product IDs so subscription detection is correct when the subscription
                # product is not the first line item.
                "line_item_product_ids": [li.get("product_id") for li in line_items if li.get("product_id")],
            })

        # Pagination
        link_header = response.headers.get("Link", "")
        if 'rel="next"' in link_header:
            match = re.search(r'page_info=([^&>]+)', link_header)
            next_page_info = match.group(1)
        else:
            break

    print(f"✅ Total orders fetched: {len(all_orders)}")
    return all_orders


# =====================================================
# 3b️⃣ Fetch orders for ONE customer (fast path)
# =====================================================

def get_customer_orders(customer_id: int, limit: int = 50):
    """Fetch a single customer's orders (status=any) with only the fields we need.

    This is the fast path to use during validation instead of fetching all store orders.
    Returns the raw order objects with line_items included.
    """
    url = f"https://{SHOP_NAME}.myshopify.com/admin/api/2024-10/orders.json"
    params = {
        "status": "any",
        "customer_id": int(customer_id),
        "limit": min(int(limit), 250),
    }

    response = requests.get(url, headers=HEADERS, params=params)
    if response.status_code != 200:
        raise RuntimeError(f"Shopify orders lookup failed ({response.status_code}): {response.text}")

    return response.json().get("orders", [])


# =====================================================
# 3c️⃣ Fetch basic info for ONE customer
# =====================================================

def get_customer_basic_info(customer_id: int):
    """
    Fetch basic customer info from Shopify (email, first_name, last_name).
    Used for lazy backfill of user profile data.
    """
    url = f"https://{SHOP_NAME}.myshopify.com/admin/api/2024-10/customers/{customer_id}.json"
    response = requests.get(url, headers=HEADERS)

    if response.status_code != 200:
        return None

    customer = response.json().get("customer", {})
    return {
        "email": customer.get("email"),
        "first_name": customer.get("first_name"),
        "last_name": customer.get("last_name"),
    }


# =====================================================
# 4️⃣ GraphQL Query — Subscription Status per Customer
# =====================================================

def get_subscription_status(customer_gid):
    """
    Check if a customer has an active or trial subscription.
    """
    url = f"https://{SHOP_NAME}.myshopify.com/admin/api/2024-10/graphql.json"

    query = """
    query getCustomerSubscription($id: ID!) {
      customer(id: $id) {
        subscriptionContracts(first: 5) {
          edges {
            node {
              id
              status
              trialDays
              nextBillingDate
              createdAt
              lineItems(first: 2) {
                edges {
                  node {
                    title
                  }
                }
              }
            }
          }
        }
      }
    }
    """

    variables = {"id": customer_gid}

    try:
        response = requests.post(url, headers=HEADERS, json={"query": query, "variables": variables})
        if response.status_code != 200:
            print(f"GraphQL Error {response.status_code}: {response.text}")
            return "Error"

        data = response.json()
        subs = data["data"]["customer"]["subscriptionContracts"]["edges"]

        if not subs:
            return "Inactive"

        for s in subs:
            node = s["node"]
            if node["status"] == "ACTIVE":
                trial_days = node.get("trialDays", 0)
                if trial_days and trial_days > 0:
                    return "Free Trial"
                return "Subscribed"
        return "Inactive"

    except Exception as e:
        print(f"❌ Error checking subscription for {customer_gid}: {e}")
        return "Error"


# =====================================================
# 5️⃣ Enrich Customers with Subscription Status
# =====================================================
def map_customer_subscriptions(orders):
    subscribed_customers = set()

    for order in orders:
        pid = order.get("line_item_0_product_id")
        cid = order.get("customer_id")

        if pid in SUBSCRIPTION_PRODUCT_IDS and cid:
            subscribed_customers.add(cid)

    return subscribed_customers


def add_subscription_status(customers, subscribed_customers):
    for customer in customers:
        cid = customer.get("id")

        if cid in subscribed_customers:
            customer["subscription_status"] = "Subscribed"
        else:
            customer["subscription_status"] = "Inactive"

    return customers


# =====================================================
# 6️⃣ Utility — Flatten JSON (for Excel)
# =====================================================
def flatten_json(y, prefix=''):
    out = {}
    def flatten(x, name=''):
        if isinstance(x, dict):
            for a in x:
                flatten(x[a], f"{name}{a}_")
        elif isinstance(x, list):
            i = 0
            for a in x:
                flatten(a, f"{name}{i}_")
                i += 1
        else:
            out[name[:-1]] = x
    flatten(y, prefix)
    return out


# =====================================================
# 7️⃣ Save to Excel — Two Sheets
# =====================================================
def save_to_excel(customers, orders, file_name="shopify_data_full.xlsx"):
    print("🧾 Saving data to Excel... please wait")
    customers_flat = [flatten_json(c) for c in customers]
    orders_flat = [flatten_json(o) for o in orders]

    customers_df = pd.DataFrame(customers_flat)
    orders_df = pd.DataFrame(orders_flat)

    with pd.ExcelWriter(file_name, engine='openpyxl') as writer:
        customers_df.to_excel(writer, sheet_name="Customers_Subscriptions", index=False)
        orders_df.to_excel(writer, sheet_name="Orders_Payments", index=False)

    print(f"✅ Export complete → {file_name}")
    print(f"   • Customers: {len(customers_df)} records")
    print(f"   • Orders: {len(orders_df)} records")


# =====================================================
# 8️⃣ Main Execution
# =====================================================
if __name__ == "__main__":
    print("🚀 Starting Shopify Data Export (Customers + Subscription Flags + Payments)...")

    customers = get_all_customers()
    orders = get_all_orders()

    # Build subscription map from orders
    subscribed_customers = map_customer_subscriptions(orders)

    # Add subscription status
    if customers:
        customers = add_subscription_status(customers, subscribed_customers)

    save_to_excel(customers, orders)
