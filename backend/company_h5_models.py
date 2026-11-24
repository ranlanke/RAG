"""Additive integration records; no changes to the research or vector schema."""
from datetime import datetime
from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from database import Base


class H5SourceVersion(Base):
    __tablename__ = 'company_h5_source_versions'
    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True)


class CompanyH5Job(Base):
    __tablename__ = 'company_h5_jobs'
    __table_args__ = (UniqueConstraint('owner_id', 'request_key', name='uq_company_h5_request'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, index=True)
    owner: Mapped[str] = mapped_column(String(100))
    request_key: Mapped[str] = mapped_column(String(80))
    active_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    source_kind: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[str] = mapped_column(String(64), default='')
    source_family: Mapped[str] = mapped_column(String(64), default='')
    source_name: Mapped[str] = mapped_column(String(255))
    source_sha256: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[int] = mapped_column(Integer, default=0)
    source_modified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_acl: Mapped[dict] = mapped_column(JSON, default=dict)
    source_key: Mapped[str] = mapped_column(String(500))
    source_size: Mapped[int] = mapped_column(Integer)
    theme: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(24), default='queued', index=True)
    stage: Mapped[str] = mapped_column(String(160), default='等待制作')
    error: Mapped[str] = mapped_column(Text, default='')
    remote_id: Mapped[str] = mapped_column(String(64), default='')
    remote_started: Mapped[int] = mapped_column(Integer, default=0)
    resume_requested: Mapped[int] = mapped_column(Integer, default=0)
    material_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    html_key: Mapped[str] = mapped_column(String(500), default='')
    preview_key: Mapped[str] = mapped_column(String(500), default='')
    zip_key: Mapped[str] = mapped_column(String(500), default='')
    saved_document_id: Mapped[str] = mapped_column(String(64), default='')
    lease_token: Mapped[str] = mapped_column(String(64), default='')
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transport_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


def observe_version(db, doc, previous):
    """Keep stable identity across subsequent rename, move, and file versions."""
    mappings = [db.get(H5SourceVersion, item.id) for item in previous]
    family = next((item.family_id for item in mappings if item), doc.id)
    for item in [*previous, doc]:
        if not db.get(H5SourceVersion, item.id):
            db.add(H5SourceVersion(document_id=item.id, family_id=family))
    return family
