import base64
import hashlib
import hmac
import unittest

try:
    from backend.shopify_client import verify_app_proxy_signature, verify_webhook_signature
except ImportError:  # pragma: no cover - allow running tests from package root
    from shopify_client import verify_app_proxy_signature, verify_webhook_signature  # type: ignore


class SecurityTests(unittest.TestCase):
    def test_verify_app_proxy_signature_accepts_valid_signature(self):
        secret = "shhh"
        params = {"logged_in_customer_id": "12345", "timestamp": "1700000000"}
        message = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

        params_with_signature = {**params, "signature": signature}

        self.assertTrue(verify_app_proxy_signature(params_with_signature, secret))

    def test_verify_app_proxy_signature_rejects_invalid_signature(self):
        params = {"logged_in_customer_id": "12345", "timestamp": "1700000000", "signature": "invalid"}
        self.assertFalse(verify_app_proxy_signature(params, "secret"))

    def test_verify_webhook_signature_accepts_valid_signature(self):
        secret = "webhook-secret"
        body = b'{"id": "gid://shopify/SubscriptionContract/1"}'
        digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
        signature = base64.b64encode(digest).decode()

        self.assertTrue(verify_webhook_signature(body, signature, secret))

    def test_verify_webhook_signature_rejects_invalid_signature(self):
        body = b'{"id": "gid://shopify/SubscriptionContract/1"}'
        self.assertFalse(verify_webhook_signature(body, "not-valid", "secret"))


if __name__ == "__main__":
    unittest.main()

