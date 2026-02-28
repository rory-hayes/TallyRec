from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateFirmRequest(StrictModel):
    name: str
    slug: str


class CreateFirmResponse(StrictModel):
    id: UUID
    name: str
    slug: str
    created_at: datetime


class CreateClientRequest(StrictModel):
    firm_id: UUID
    name: str
    external_ref: str | None = None


class CreateClientResponse(StrictModel):
    id: UUID
    firm_id: UUID
    name: str
    external_ref: str | None
    created_at: datetime


class CreateRunRequest(StrictModel):
    firm_id: UUID
    client_id: UUID
    period_start: date
    period_end: date


class CreateRunResponse(StrictModel):
    id: UUID
    firm_id: UUID
    client_id: UUID
    status: str
    period_start: date
    period_end: date
    created_at: datetime


class RegisterSourceFileRequest(StrictModel):
    file_kind: str
    filename: str
    uri: str
    checksum_sha256: str = Field(min_length=64, max_length=64)
    byte_size: int = Field(ge=0)
    mapping_template_id: UUID | None = None


class RegisterSourceFileResponse(StrictModel):
    id: UUID
    run_id: UUID
    file_kind: str
    uri: str
    checksum_sha256: str
    created_at: datetime


class EnqueueJobRequest(StrictModel):
    job_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class EnqueueJobResponse(StrictModel):
    id: UUID
    run_id: UUID
    status: str
    job_type: str
    queued_at: datetime


class ReconcileBankRequest(StrictModel):
    idempotency_key: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class UpdateReconPolicyRequest(StrictModel):
    amount_tolerance: float | None = None
    date_window_days: int | None = None
    max_group_size: int | None = None
    enable_one_to_many: bool | None = None
    enable_many_to_one: bool | None = None
    require_allowed_account: bool | None = None


class BankAccountItem(StrictModel):
    account_ref: str
    label: str | None = None


class PutBankAccountsRequest(StrictModel):
    accounts: list[BankAccountItem]
