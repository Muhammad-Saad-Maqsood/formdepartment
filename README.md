# CapsuleBuilder

## Shopify App Proxy Integration

The Flask backend in `backend/app.py` exposes a secure Shopify App Proxy bridge that gates access to the Capsule Builder tool. Requests arrive via `/proxy/tool`, and the backend performs the following checks:

- Validates the App Proxy signature using your Shopify app shared secret.
- Confirms the logged-in customer has an active subscription, trial access, or master override by calling Shopify's Admin GraphQL API.
- Returns either the embedded Capsule Builder interface (authorized) or a subscription upgrade prompt (unauthorized).

### Environment Variables

Create a `.env` file inside `backend/` (or configure the variables in your hosting platform) with the following keys:

```
SHOP_NAME=your-store-subdomain
SHOPIFY_SHARED_SECRET=app-proxy-shared-secret
SHOPIFY_ACCESS_TOKEN=admin-api-access-token
SHOPIFY_WEBHOOK_SECRET=webhook-signing-secret
SHOPIFY_API_VERSION=2024-10
TOOL_APP_URL=https://capsule-builder-qhzx.vercel.app/
UPGRADE_PATH=pages/upgrade
MASTER_CUSTOMER_IDS=12345,67890
TRIAL_CUSTOMER_IDS=54321
MASTER_CUSTOMER_EMAILS=founder@example.com
TRIAL_CUSTOMER_EMAILS=sample@example.com
```

Only the first three variables are required; the rest provide optional overrides, trial access, and URLs.

### Running Locally

```
cd backend
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -r requirements.txt
python -m flask --app app run --debug
```

### Webhooks

Shopify webhooks should target `https://<your-backend>/webhooks/<topic>` (for example `/webhooks/subscription_contracts/update`). Incoming payloads are validated via `X-Shopify-Hmac-Sha256`. Each event is appended to JSONL files under `backend/data/webhooks/` for auditing.

### Tests

Run the unit tests with:

```
python -m unittest discover backend/tests
```