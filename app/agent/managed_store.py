"""Durable receipts and action journals for managed design handoffs.

This module owns storage only. The route transaction must authenticate actors,
validate source proofs, compare original request bytes, and freeze audit/outbox
content before writing. JSON helpers must never normalize an incoming wire body.
References to legacy handoffs remain logical because they use another metadata.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    """Encode a locally authored object; never use this to replace wire bytes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def action_key(scope_id: str, username: str, action_id: str) -> str:
    """Keep action identities isolated without ambiguous delimiter joins."""
    return sha256(canonical_bytes([scope_id, username, action_id]))


def _new_id() -> str:
    return uuid.uuid4().hex


class ManagedBase(DeclarativeBase):
    pass


class ManagedReceipt(ManagedBase):
    __tablename__ = "design_managed_receipt"
    __table_args__ = (
        UniqueConstraint(
            "source_instance_id",
            "target_instance_id",
            "scope_id",
            "package_id",
            "package_version",
            name="uq_design_managed_receipt_package",
        ),
        UniqueConstraint(
            "source_instance_id",
            "target_instance_id",
            "scope_id",
            "request_id",
            name="uq_design_managed_receipt_request",
        ),
        CheckConstraint("revision >= 1", name="ck_design_managed_receipt_revision"),
        CheckConstraint("priority IN ('P0', 'P1', 'P2', 'P3')", name="ck_design_managed_receipt_priority"),
    )

    handoff_id: Mapped[str] = mapped_column(String(220), primary_key=True)
    source_instance_id: Mapped[str] = mapped_column(String(160))
    target_instance_id: Mapped[str] = mapped_column(String(160))
    scope_id: Mapped[str] = mapped_column(String(160), index=True)
    request_id: Mapped[str] = mapped_column(String(160))
    package_id: Mapped[str] = mapped_column(String(160))
    package_version: Mapped[str] = mapped_column(String(80))
    envelope_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    envelope_sha256: Mapped[str] = mapped_column(String(64))
    proof: Mapped[dict] = mapped_column(JSON)
    receipt: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    priority: Mapped[str] = mapped_column(String(2), default="P2", server_default="P2")
    due_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class ManagedAction(ManagedBase):
    __tablename__ = "design_managed_action"

    action_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(String(100), index=True)
    username: Mapped[str] = mapped_column(String(80), index=True)
    auth_context_id: Mapped[str] = mapped_column(String(160))
    method: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(1000))
    raw_body: Mapped[bytes] = mapped_column(LargeBinary)
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict] = mapped_column(JSON)
    handoff_id: Mapped[str | None] = mapped_column(String(220), nullable=True, index=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class ManagedAudit(ManagedBase):
    __tablename__ = "design_managed_audit"
    __table_args__ = (CheckConstraint("revision >= 1", name="ck_design_managed_audit_revision"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_new_id)
    handoff_id: Mapped[str] = mapped_column(String(220), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(80))
    actor: Mapped[str] = mapped_column(String(160))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class ManagedOutbox(ManagedBase):
    __tablename__ = "design_managed_outbox"

    event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    handoff_id: Mapped[str] = mapped_column(String(220), index=True)
    source_instance_id: Mapped[str] = mapped_column(String(160))
    target_instance_id: Mapped[str] = mapped_column(String(160))
    scope_id: Mapped[str] = mapped_column(String(160), index=True)
    raw_body: Mapped[bytes] = mapped_column(LargeBinary)
    body_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), default="prepared", server_default="prepared")
    receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class ManagedAttempt(ManagedBase):
    __tablename__ = "design_managed_attempt"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_new_id)
    event_id: Mapped[str] = mapped_column(String(160), index=True)
    action_id: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(40))
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    started_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    finished_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class IndependentProject(ManagedBase):
    """Local owner binding. Never an external handoff, receipt or approval."""

    __tablename__ = "design_independent_project"
    project_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    id: Mapped[str] = mapped_column(String(80), unique=True)
    owner_subject: Mapped[str] = mapped_column(String(80), index=True)
    scope_id: Mapped[str] = mapped_column(String(160), index=True)
    target_instance_id: Mapped[str] = mapped_column(String(160))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class ImageReservation(ManagedBase):
    """A private authorization can reserve exactly one task, including unknown outcomes."""

    __tablename__ = "design_image_reservation"
    grant_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(40), unique=True)
    project_id: Mapped[int] = mapped_column(Integer)
    subject: Mapped[str] = mapped_column(String(80))
    authorization: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class ExecutionReservation(ManagedBase):
    """One immutable authorization domain; pending/unknown never refunds quota."""

    __tablename__ = "design_execution_reservation"
    __table_args__ = (UniqueConstraint("run_id", "stage", name="uq_design_execution_stage"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(100), index=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    stage: Mapped[str] = mapped_column(String(20))
    project_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class ProviderSlot(ManagedBase):
    """Shared dispatch slot. Unknown ownership cannot expire into another call."""

    __tablename__ = "design_provider_slot"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32))


class CreationGrantBinding(ManagedBase):
    """The next-creation grant is claimed once in the original creation transaction."""

    __tablename__ = "design_creation_grant_binding"
    grant_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer)
    creation_action_id: Mapped[str] = mapped_column(String(100))
    intent_id: Mapped[str] = mapped_column(String(40))
    run_id: Mapped[str] = mapped_column(String(40))
    grant_sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    bound_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class ExecutionContextPolicy(ManagedBase):
    __tablename__ = "design_execution_context_policy"
    auth_context_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(20))
    subject: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class InteractiveGrant(ManagedBase):
    __tablename__ = "design_interactive_grant"
    grant_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(100), index=True)
    run_id: Mapped[str] = mapped_column(String(40), unique=True)
    project_id: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class PublicExecutionBudget(ManagedBase):
    __tablename__ = "design_public_execution_budget"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(160))
    instance_id: Mapped[str] = mapped_column(String(160))
    text_limit: Mapped[int] = mapped_column(Integer)
    image_limit: Mapped[int] = mapped_column(Integer)


class PublicProviderCall(ManagedBase):
    __tablename__ = "design_public_provider_call"
    attempt_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    stage: Mapped[str] = mapped_column(String(20))
    run_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
