"""
SQLAlchemy Models for Hybrid Multi-Tenancy Architecture.
Follows Hexagonal Architecture: These are pure persistence models (Adapters).
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr


# ---------------------------------------------------------------------------
# Global Control Plane Models
# ---------------------------------------------------------------------------
class GlobalBase(DeclarativeBase):
    __allow_unmapped__ = True


class DatabaseShard(GlobalBase):
    """
    Represents a physical database instance (Shard or Dedicated Enterprise DB).
    """

    __tablename__ = "database_shards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    dsn = Column(String(1024), nullable=False)  # SQLAlchemy connection string
    created_at = Column(DateTime, default=datetime.utcnow)


class Tenant(GlobalBase):
    """
    Represents a tenant (Organization) in the Control Plane.
    """

    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idp_tenant_id = Column(String(255), nullable=True, unique=True)  # Links to generic external IdP
    name = Column(String(255), nullable=False, unique=True)

    # Routing info
    shard_id = Column(Integer, ForeignKey("database_shards.id"), nullable=False)

    # Tier: 'standard' (pooled in a shard), 'enterprise' (dedicated shard)
    tier = Column(String(50), nullable=False, default="standard")

    created_at = Column(DateTime, default=datetime.utcnow)


class User(GlobalBase):
    """
    Represents a user profile in the application.
    Authentication is handled by Zitadel, this maps the IDP user to our app.
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idp_user_id = Column(String(255), nullable=True, unique=True)  # Links to Zitadel user
    email = Column(String(255), nullable=False, unique=True)
    name = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TenantUser(GlobalBase):
    """
    Maps users to tenants with specific roles (RBAC).
    """

    __tablename__ = "tenant_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(50), nullable=False, default="member")

    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_tenant_user"),)


# ---------------------------------------------------------------------------
# Tenant Data Models (Reside in Shards/Enterprise DBs)
# ---------------------------------------------------------------------------
class TenantBase(DeclarativeBase):
    __allow_unmapped__ = True


class TenantAwareMixin:
    """
    Mixin that adds a `tenant_id` to all tenant-scoped tables.
    Essential for Row-Level Security (RLS) enforcement.
    """

    @declared_attr
    def tenant_id(cls) -> Mapped[int]:
        from sqlalchemy.orm import mapped_column

        # We don't enforce a ForeignKey here because the tenants table
        # is mastered in the Global DB. While logical replication might sync it down,
        # relying purely on the application routing and RLS is safer and more decoupled.
        return mapped_column(Integer, nullable=False, index=True)


class TradingPartner(TenantBase, TenantAwareMixin):
    """
    Represents an AS2 Trading Partner profile.
    """

    __tablename__ = "trading_partners"
    id = Column(Integer, primary_key=True, autoincrement=True)

    as2_id = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    url = Column(String(1024), nullable=True)  # Destination URL for outgoing messages

    public_cert_pem = Column(Text, nullable=True)
    is_host_identity = Column(Boolean, default=False, nullable=False)

    # KMS / Envelope Encryption Strategy
    private_key_ciphertext = Column(Text, nullable=True)
    kms_key_id = Column(String(255), nullable=True)

    # Vault / External Secret Strategy (alternative)
    private_key_secret_id = Column(String(255), nullable=True)

    # Key rotation metadata
    key_version = Column(Integer, default=1, nullable=False)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "as2_id", name="uq_tenant_as2_id"),
        Index(
            "ix_tenant_active_host_identity",
            "tenant_id",
            unique=True,
            postgresql_where=(is_host_identity.is_(True) & is_active.is_(True)),
        ),
    )


class AS2Payload(TenantBase, TenantAwareMixin):
    """
    Storage for incoming and outgoing AS2 Messages and their payloads.
    """

    __tablename__ = "as2_payloads"
    id = Column(Integer, primary_key=True, autoincrement=True)

    message_id = Column(String(255), nullable=False, index=True)
    direction = Column(String(10), nullable=False)  # 'INBOUND' or 'OUTBOUND'

    as2_from = Column(String(255), nullable=False)
    as2_to = Column(String(255), nullable=False)

    raw_headers = Column(Text, nullable=True)
    payload_storage_uri = Column(String(2048), nullable=True)

    mic = Column(String(255), nullable=True)
    status = Column(String(50), nullable=False)  # 'RECEIVED', 'DECRYPTED', 'MDN_SENT', 'ERROR'

    created_at = Column(DateTime, default=datetime.utcnow)
