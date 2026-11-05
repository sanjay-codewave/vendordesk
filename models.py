import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, LargeBinary, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # stored as naive UTC


def _db_url():
    url = os.environ.get("DATABASE_URL", "sqlite:///vendordesk.db")
    # Neon/Render give postgres:// or postgresql://; we ship psycopg 3, not psycopg2.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


engine = create_engine(_db_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    token: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    business_name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


FIELD_NAMES = ["legal_name", "trade_name", "gstin", "pan", "address", "bank_account_name",
               "bank_account_number", "ifsc", "bank_name", "msme_number"]
STATUSES = ["requested", "submitted", "needs_fixes", "verified", "approved"]
FILE_KINDS = ["gst_certificate", "pan_card", "cancelled_cheque", "msme", "other"]


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    vendor_name: Mapped[str] = mapped_column(String(200))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    contact_phone: Mapped[str | None] = mapped_column(String(40))
    contact_email: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str | None] = mapped_column(String(100))
    public_token: Mapped[str] = mapped_column(String(80), unique=True, index=True,
                                              default=lambda: secrets.token_urlsafe(24))
    status: Mapped[str] = mapped_column(String(20), default="requested")
    required_items: Mapped[list] = mapped_column(JSON, default=list)
    request_message: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    # Latest review saved by Claude via add_review_note (editable by the user).
    review_findings: Mapped[str | None] = mapped_column(Text)
    follow_up_message: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[str] = mapped_column(String(20), default="claude")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)

    fields: Mapped["VendorFields"] = relationship(cascade="all, delete-orphan")
    files: Mapped[list["VendorFile"]] = relationship(cascade="all, delete-orphan", order_by="VendorFile.id")
    events: Mapped[list["Event"]] = relationship(cascade="all, delete-orphan", order_by="Event.id.desc()")


class VendorFields(Base):
    __tablename__ = "vendor_fields"
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), primary_key=True)
    legal_name: Mapped[str | None] = mapped_column(String(300))
    trade_name: Mapped[str | None] = mapped_column(String(300))
    gstin: Mapped[str | None] = mapped_column(String(20))
    pan: Mapped[str | None] = mapped_column(String(20))
    address: Mapped[str | None] = mapped_column(Text)
    bank_account_name: Mapped[str | None] = mapped_column(String(300))
    bank_account_number: Mapped[str | None] = mapped_column(String(30))
    ifsc: Mapped[str | None] = mapped_column(String(20))
    bank_name: Mapped[str | None] = mapped_column(String(200))
    msme_number: Mapped[str | None] = mapped_column(String(40))

    def as_dict(self):
        return {f: getattr(self, f) for f in FIELD_NAMES}


class VendorFile(Base):
    __tablename__ = "vendor_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    # Render's free disk is ephemeral, so files live in the database.
    data: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    actor: Mapped[str] = mapped_column(String(20))  # vendor | you | claude
    action: Mapped[str] = mapped_column(String(100))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 260_000).hex()
    return f"{salt}${digest}"


def verify_password(password, stored):
    salt, _, _ = stored.partition("$")
    return hmac.compare_digest(hash_password(password, salt), stored)


def init_db():
    Base.metadata.create_all(engine)
    password = os.environ.get("DEMO_PASSWORD", "demo1234")
    token = os.environ.get("DEMO_TOKEN", "demo-local-token")
    with SessionLocal() as db:
        demo = db.scalar(select(User).where(User.username == "demo"))
        if not demo:
            demo = User(username="demo", business_name="Demo Traders")
            db.add(demo)
        # Keep the demo account in sync with the env so rotating the secrets takes effect on restart.
        demo.password_hash = hash_password(password)
        demo.token = token
        db.commit()
