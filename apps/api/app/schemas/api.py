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
    observed_headers: list[str] | None = None


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
    as_of_date: date | None = None


class ReconcileGLRequest(StrictModel):
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


class UpsertGLBucketAccountsRequest(StrictModel):
    net_pay_control: list[str] = Field(default_factory=list)
    taxes: list[str] = Field(default_factory=list)
    pension: list[str] = Field(default_factory=list)
    other: list[str] = Field(default_factory=list)


class ResolveVarianceRequest(StrictModel):
    action: str
    note: str


class ReadyForReviewRequest(StrictModel):
    note: str | None = None


class ApproveRunRequest(StrictModel):
    note: str | None = None


class UnlockRunRequest(StrictModel):
    reason: str


class EnqueueExportPackRequest(StrictModel):
    idempotency_key: str | None = None


class CreateBatchRunsRequest(StrictModel):
    firm_id: UUID
    period_start: date
    period_end: date
    client_ids: list[UUID] = Field(min_length=1)
    as_of_date: date | None = None


class BatchRunItemResponse(StrictModel):
    id: UUID
    client_id: UUID
    run_id: UUID | None
    status: str
    bank_job_id: UUID | None
    gl_job_id: UUID | None
    error: dict[str, Any]
    updated_at: datetime


class BatchRunResponse(StrictModel):
    id: UUID
    firm_id: UUID
    period_start: date
    period_end: date
    status: str
    requested_clients: int
    created_runs: int
    queued_jobs: int
    succeeded_runs: int
    failed_runs: int
    options: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    items: list[BatchRunItemResponse]


class DashboardRunItem(StrictModel):
    run_id: UUID
    client_id: UUID
    client_name: str
    run_status: str
    overall_tie_status: str
    open_blockers: int
    open_review: int
    payday_date: date | None
    must_close_by_date: date | None
    import_health_band: str | None
    latest_job_status: str | None
    sla_reminder_state: str | None


class DashboardResponse(StrictModel):
    counts_by_status: dict[str, int]
    counts_by_due_bucket: dict[str, int]
    runs: list[DashboardRunItem]
    total: int


class UpdateUKTimingPolicyRequest(StrictModel):
    tax_due_day: int | None = Field(default=None, ge=1, le=31)
    pension_due_day: int | None = Field(default=None, ge=1, le=31)
    bacs_visibility_business_days: int | None = Field(default=None, ge=0, le=10)
    holiday_calendar: str | None = None
    enabled: bool | None = None


class UpsertMappingTemplateRequest(StrictModel):
    name: str
    file_kind: str
    mapping: dict[str, Any] = Field(default_factory=dict)
    expected_headers: list[str] | None = None


class RemapSourceFileRequest(StrictModel):
    mapping_template_id: UUID
    observed_headers: list[str] | None = None
