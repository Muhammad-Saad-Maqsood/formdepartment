
import hmac, hashlib
from shopify_client import verify_app_proxy_signature

shared_secret = "PASTE_THE_SECRET_YOU_JUST_PRINTED"
params = {
    "logged_in_customer_id": "YOUR_CUSTOMER_ID",
    "timestamp": "1710100000"
}

message = "&".join(f"{k}={params[k]}" for k in sorted(params))
signature = hmac.new(shared_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
params["signature"] = signature

print("Use these query params:", params)
print("Would Flask accept it? ->", verify_app_proxy_signature(params, shared_secret))
