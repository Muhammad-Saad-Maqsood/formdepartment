# models.py
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, BigInteger
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    customer_id = Column(BigInteger, unique=True, index=True, nullable=False)  # Shopify id
    email = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)

    plan = Column(String, default="none")   # none | tier1 | tier2
    plan_product_id = Column(BigInteger, nullable=True)
    expiry = Column(DateTime, nullable=True)       # purchase_created_at + 30 days
    remaining_uses = Column(Integer, nullable=True) # Deprecated - kept for legacy data. Both tiers now have unlimited access.
    trial_used = Column(Boolean, default=False)

    # Subscription bookkeeping for optimized per-customer validation
    # These let us refresh Shopify only when needed and correctly reset Tier1
    # usage when a new subscription purchase happens.
    last_shopify_check_at = Column(DateTime, nullable=True)
    last_subscription_order_id = Column(BigInteger, nullable=True)
    last_subscription_purchase_at = Column(DateTime, nullable=True)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
