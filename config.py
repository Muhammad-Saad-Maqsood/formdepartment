import os
from dataclasses import dataclass
from typing import List, Optional

from dotenv import load_dotenv

# Ensure environment variables from .env are available when running locally.
load_dotenv()


def _as_list(raw_value: Optional[str]) -> List[str]:
    if not raw_value:
        return []
    return [item.strip() for item in raw_value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the Capsule Builder Shopify integration."""

    shop_name: str
    access_token: str
    shared_secret: str
    api_version: str
    tool_app_url: str
    plan_page_url: Optional[str]
    upgrade_path: str

    master_customer_ids: List[str]
    trial_customer_ids: List[str]
    master_customer_emails: List[str]
    trial_customer_emails: List[str]

    webhook_shared_secret: Optional[str]

    @property
    def shop_domain(self) -> str:
        return f"{self.shop_name}.myshopify.com" if "." not in self.shop_name else self.shop_name

    @property
    def upgrade_url(self) -> str:
        base = self.shop_domain
        path = self.upgrade_path.lstrip("/")
        return f"https://{base}/{path}"


def load_settings() -> Settings:
    """Load settings from environment variables with sensible fallbacks."""

    shop_name = os.getenv("SHOP_NAME")
    access_token = os.getenv("SHOPIFY_ACCESS_TOKEN")
    shared_secret = os.getenv("SHOPIFY_SHARED_SECRET") or os.getenv("SHOPIFY_SECRET")

    if not shop_name or not access_token or not shared_secret:
        missing = [
            name
            for name, value in [
                ("SHOP_NAME", shop_name),
                ("SHOPIFY_ACCESS_TOKEN", access_token),
                ("SHOPIFY_SHARED_SECRET", shared_secret),
            ]
            if not value
        ]
        raise RuntimeError(f"Missing required Shopify configuration values: {', '.join(missing)}")

    return Settings(
        shop_name=shop_name.strip(),
        access_token=access_token.strip(),
        shared_secret=shared_secret.strip(),
        api_version=os.getenv("SHOPIFY_API_VERSION", "2024-10"),
        tool_app_url=os.getenv("TOOL_APP_URL", "https://capsule-builder-qhzx.vercel.app/"),
        plan_page_url=os.getenv("PLAN_PAGE_PATH", "https://formdepartment.com/pages/about?view=subscription-plans"),
        upgrade_path=os.getenv("UPGRADE_PATH", "pages/upgrade"),
        master_customer_ids=_as_list(os.getenv("MASTER_CUSTOMER_IDS")),
        trial_customer_ids=_as_list(os.getenv("TRIAL_CUSTOMER_IDS")),
        master_customer_emails=_as_list(os.getenv("MASTER_CUSTOMER_EMAILS")),
        trial_customer_emails=_as_list(os.getenv("TRIAL_CUSTOMER_EMAILS")),
        webhook_shared_secret=os.getenv("SHOPIFY_WEBHOOK_SECRET"),
    )


