import os
import re
import time
import requests
import pandas as pd
from dotenv import load_dotenv
from config import load_settings

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
    print("📦 Fetching all customers...")
    all_customers = []
    # base_url = f"https://{SHOP_NAME}.myshopify.com/admin/api/2024-10/customers.json"
    base_url = f"https://{SHOP_NAME}.myshopify.com/admin/api/2024-10/customers.json"
    params = {"limit": 250}
    next_page_info = None

    while True:
        if next_page_info:
            params = {"limit": 250, "page_info": next_page_info}

        response = requests.get(base_url, headers=HEADERS, params=params)
        if response.status_code != 200:
            print(f"❌ Error {response.status_code}: {response.text}")
            break

        data = response.json()
        customers = data.get("customers", [])
        all_customers.extend(customers)

        # Pagination
        link_header = response.headers.get("Link", "")
        if 'rel="next"' in link_header:
            match = re.search(r'page_info=([^&>]+)', link_header)
            next_page_info = match.group(1) if match else None
        else:
            break

        print(f"Fetched {len(all_customers)} customers so far...")

    print(f"✅ Total customers fetched: {len(all_customers)}")
    return all_customers



# =====================================================
# 3️⃣ Fetch ALL ORDERS (Payments Data)
# =====================================================
def get_all_orders():
    print("💰 Fetching all orders/payments...")
    all_orders = []
    base_url = f"https://{SHOP_NAME}.myshopify.com/admin/api/2024-10/orders.json"
    params = {"limit": 250, "status": "any"}  # include open/closed
    next_page_info = None

    while True:
        if next_page_info:
            params = {"limit": 250, "status": "any", "page_info": next_page_info}

        response = requests.get(base_url, headers=HEADERS, params=params)
        if response.status_code != 200:
            print(f"❌ Error {response.status_code}: {response.text}")
            break

        data = response.json()
        orders = data.get("orders", [])
        all_orders.extend(orders)

        # Pagination
        link_header = response.headers.get("Link", "")
        if 'rel="next"' in link_header:
            match = re.search(r'page_info=([^&>]+)', link_header)
            next_page_info = match.group(1) if match else None
        else:
            break

        print(f"Fetched {len(all_orders)} orders so far...")

    print(f"✅ Total orders fetched: {len(all_orders)}")
    return all_orders


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
# def add_subscription_status(customers):
#     print("🔍 Checking subscription status for each customer...")
#     enriched = []
#     for i, cust in enumerate(customers):
#         gid = cust.get("admin_graphql_api_id")
#         email = cust.get("email", "")
#         status = get_subscription_status(gid)
#         cust["subscription_status"] = status
#         enriched.append(cust)
#         print(f"[{i+1}/{len(customers)}] {email or gid} → {status}")
#         time.sleep(0.3)  # avoid Shopify API rate limits
#     return enriched

def add_subscription_status(customers, settings):
    for customer in customers:
        email = customer.get("email")
        cid = customer.get("id")

        if cid in settings.master_customer_ids or email in settings.master_customer_emails:
            customer["subscription_status"] = "Subscribed"
        elif cid in settings.trial_customer_ids or email in settings.trial_customer_emails:
            customer["subscription_status"] = "Trial"
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
    print("🚀 Starting Shopify Data Export (Customers + Subscriptions + Payments)...")

    customers = get_all_customers()
    if customers:
        customers = add_subscription_status(customers, settings=load_settings())
    else:
        print("⚠️ No customers found.")

    orders = get_all_orders()

    save_to_excel(customers, orders)