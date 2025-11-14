import base64
import hashlib
import hmac
from dataclasses import dataclass
from typing import Dict, List, Optional
import json

import requests

try:
    from .config import Settings
except ImportError:  # pragma: no cover - allow direct module execution
    from config import Settings  # type: ignore


def _canonicalize_params(params: Dict[str, str]) -> str:
    return "&".join(f"{key}={value}" for key, value in sorted(params.items()))


def verify_app_proxy_signature(params: Dict[str, str], shared_secret: str) -> bool:
    """Validate the signature attached to Shopify App Proxy requests."""
    signature = params.get("signature")
    if not signature:
        return False

    filtered = {key: value for key, value in params.items() if key != "signature"}
    message = _canonicalize_params(filtered)
    digest = hmac.new(shared_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


def verify_webhook_signature(body: bytes, header_hmac: str, shared_secret: str) -> bool:
    """Validate the HMAC signature included with Shopify webhook payloads."""
    if not header_hmac or not shared_secret:
        return False

    digest = hmac.new(shared_secret.encode("utf-8"), body, hashlib.sha256).digest()
    calculated = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(calculated, header_hmac)


@dataclass
class CustomerAccess:
    allowed: bool
    status: str
    reason: str
    subscription_count: int
    trial: bool


class ShopifyAdminClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _rest_url(self, path: str) -> str:
        return f"https://{self.settings.shop_domain}/admin/api/{self.settings.api_version}/{path}"

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Shopify-Access-Token": self.settings.access_token,
            "Content-Type": "application/json",
        }

    def _is_master(self, customer_id: Optional[str], email: Optional[str]) -> bool:
        return bool(
            (customer_id and customer_id in self.settings.master_customer_ids)
            or (email and email.lower() in {item.lower() for item in self.settings.master_customer_emails})
        )

    def _is_trial(self, customer_id: Optional[str], email: Optional[str]) -> bool:
        return bool(
            (customer_id and customer_id in self.settings.trial_customer_ids)
            or (email and email.lower() in {item.lower() for item in self.settings.trial_customer_emails})
        )

    def check_customer_access(self, customer_id: str) -> CustomerAccess:
        customer_id = _normalize_customer_id(customer_id)
        """
        Determine whether the customer should be allowed to access the tool.

        Priority:
          1. Master list (always allowed)
          2. Trial list (allowed, flagged as trial)
          3. Active subscription contracts
        """

        customer = self._fetch_customer(customer_id)
        if customer is None:
            return CustomerAccess(
                allowed=False,
                status="not_found",
                reason="Customer not found in Shopify",
                subscription_count=0,
                trial=False,
            )
        email = customer.get("email")

        # Check email marketing consent
        is_subscribed = _is_email_marketing_subscribed(customer)
        print(f"🔍 Email marketing subscribed: {is_subscribed}")

        if self._is_master(customer_id, email):
            return CustomerAccess(
                allowed=True,
                status="master",
                reason="Customer is in master access list",
                subscription_count=0,
                trial=False,
            )

        if _is_email_marketing_subscribed(customer):
            return CustomerAccess(
                allowed=True,
                status="email_marketing_subscribed",
                reason="Customer opted in to marketing emails",
                subscription_count=0,
                trial=False,
            )

        active_subscriptions = self._fetch_active_subscription_contracts(customer_id)

        if active_subscriptions:
            return CustomerAccess(
                allowed=True,
                status="active_subscription",
                reason="Customer has an active subscription contract",
                subscription_count=len(active_subscriptions),
                trial=False,
            )

        if self._is_trial(customer_id, email):
            return CustomerAccess(
                allowed=True,
                status="trial",
                reason="Customer is in trial access list",
                subscription_count=0,
                trial=True,
            )

        return CustomerAccess(
            allowed=False,
            status="inactive",
            reason="Customer does not have an active subscription",
            subscription_count=0,
            trial=False,
        )

    def _fetch_customer(self, customer_id: str) -> Optional[Dict]:
        url = self._rest_url(f"customers/{customer_id}.json")
        params = {
            "fields": "id,email,first_name,last_name,email_marketing_consent,email_marketing_consent_state"
        }
        try:
            response = requests.get(url, headers=self._headers(), params=params, timeout=10)
        except requests.RequestException as exc:
            raise RuntimeError(f"Failed to communicate with Shopify: {exc}") from exc

        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeError(f"Shopify REST error {response.status_code}: {response.text}")

        data = response.json()
        print("👤 Customer data:", json.dumps(data.get("customer"), indent=2))
        return data.get("customer")

    def _fetch_active_subscription_contracts(self, customer_id: str) -> List[Dict]:
        url = self._rest_url("subscription_contracts.json")
        params = {"customer_id": customer_id, "status": "ACTIVE"}
        
        try:
            response = requests.get(url, headers=self._headers(), params=params, timeout=10)
            
            # Handle 404 as no subscriptions (not an error)
            if response.status_code == 404:
                print(f"🔍 No subscription contracts found for customer {customer_id}")
                return []
                
            if response.status_code != 200:
                print(f"❌ Subscription check error {response.status_code}: {response.text}")
                return []  # Return empty instead of raising error

            payload = response.json()
            contracts = payload.get("subscription_contracts", [])
            print(f"🔍 Found {len(contracts)} subscription contracts")
            return contracts
            
        except requests.RequestException as exc:
            print(f"❌ Network error checking subscriptions: {exc}")
            return []  # Return empty instead of raising error
        
        
def _is_email_marketing_subscribed(customer: Dict) -> bool:
    # Check both possible locations for email marketing consent
    state = customer.get("email_marketing_consent_state")
    if isinstance(state, str) and state.lower() == "subscribed":
        return True

    consent = customer.get("email_marketing_consent")
    if isinstance(consent, dict):
        status = consent.get("state")
        return isinstance(status, str) and status.lower() == "subscribed"

    # Also check the direct field
    if customer.get("accepts_marketing"):
        return True

    return False


    consent = customer.get("email_marketing_consent")
    if isinstance(consent, dict):
        status = consent.get("state")
        return isinstance(status, str) and status.lower() == "subscribed"

    return False


def _normalize_customer_id(customer_id: str) -> str:
    if isinstance(customer_id, str) and customer_id.startswith("gid://shopify/Customer/"):
        return customer_id.rsplit("/", 1)[-1]
    return customer_id