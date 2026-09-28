"""Pydantic models mirroring contracts/openapi.yaml components (names match so conformance tests can compare them).
Responses drop None values except for fields the contract declares nullable; requests reject unknown fields
(mass-assignment protection)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_serializer

Severity = Literal["INFO", "WARNING", "CRITICAL"]
AlertStatus = Literal["open", "ack", "resolved"]
RuleCode = Literal["TOW", "CRASH", "GEOFENCE_BREACH", "CRITICAL_DTC", "LOW_SOC_STRANDING", "GOING_DARK", "EXCESSIVE_IDLING",
                   "HARSH_DRIVING", "MAINTENANCE_RISK", "UNAPPROVED_DEPOT_CLUSTER", "ODOMETER_ROLLBACK", "SCHEMA_DRIFT"]
VehicleStatus = Literal["PARKED", "DRIVING", "IDLING", "CHARGING", "OFFLINE", "TOWED"]
Powertrain = Literal["ICE", "EV", "HYBRID"]


class ApiModel(BaseModel):
    NULLABLE: ClassVar[frozenset[str]] = frozenset()
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    @model_serializer(mode="wrap")
    def _drop_none(self, handler):
        d = handler(self)
        return {k: v for k, v in d.items() if v is not None or k in self.NULLABLE}


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    trace_id: str | None = None


class Me(ApiModel):
    sub: str
    tenant_id: UUID
    tenant_name: str | None = None
    permissions: list[str] | None = None
    roles: list[str]
    email: str | None = None


class FleetSummary(ApiModel):
    vehicles_total: int
    vehicles_by_status: dict[str, int]
    leaking_usd_today: float
    savings_opportunity_usd_week: float
    at_risk_vehicles: int
    open_critical_alerts: int
    freshness_p95_ms: int
    model_fallback_active: bool | None = None
    as_of: datetime


class Vehicle(ApiModel):
    vin: str
    fleet_id: UUID
    oem: str
    model: str
    powertrain: Powertrain
    status: VehicleStatus | None = None
    last_risk_score: float | None = Field(None, ge=0, le=1)
    open_alerts: int | None = None
    lat: float | None = None
    lon: float | None = None
    last_seen: datetime | None = None


class Page(ApiModel):
    NULLABLE = frozenset({"next_cursor"})
    next_cursor: str | None = None


class VehiclePage(Page):
    items: list[Vehicle]


class Trip(ApiModel):
    id: UUID
    start_ts: datetime
    end_ts: datetime
    distance_km: float
    idle_s: int | None = None
    energy_used: float | None = None
    energy_cost: float | None = None
    harsh_brake_count: int | None = None
    harsh_accel_count: int | None = None
    driver_ref: str | None = None


class TripPage(Page):
    items: list[Trip]


class ReasonCode(ApiModel):
    feature: str
    contribution: float
    human: str | None = None


class RiskDetail(ApiModel):
    p_failure_7d: float = Field(ge=0, le=1)
    model_version: str
    fallback: bool | None = None
    reason_codes: list[ReasonCode] | None = None
    expected_value_usd: float | None = None
    recommendation: Literal["INSPECT", "MONITOR", "NONE"]
    predicted_at: datetime | None = None


class Lookalike(ApiModel):
    NULLABLE = frozenset({"failed_within_7d"})
    vin: str
    similarity: float
    failed_within_7d: bool | None = None
    note: str | None = None


class Alert(ApiModel):
    id: UUID
    vin: str
    rule: RuleCode
    severity: Severity
    status: AlertStatus
    occurrences: int | None = None
    summary: str | None = None
    details: dict[str, Any] | None = None
    impact_usd: float | None = None
    opened_at: datetime
    event_ts: datetime | None = None
    detected_ts: datetime | None = None
    latency_ms: int | None = None
    lat: float | None = None
    lon: float | None = None
    trace_id: str | None = None


class AlertPage(Page):
    items: list[Alert]


class Vehicle360(ApiModel):
    vehicle: Vehicle
    twin: dict[str, Any] | None = None
    risk: RiskDetail | None = None
    recent_alerts: list[Alert] | None = None
    recent_trips: list[Trip] | None = None
    location_precision: Literal["exact", "geohash7", "geohash6", "geohash5"] | None = None


class MapPoint(ApiModel):
    vin: str
    lat: float
    lon: float
    status: VehicleStatus | None = None


class MapCell(ApiModel):
    geohash: str
    count: int
    idle: int


class MapViewport(ApiModel):
    mode: Literal["points", "cells"] | None = None
    points: list[MapPoint] | None = None
    cells: list[MapCell] | None = None
    truncated: bool | None = None


class Driver(ApiModel):
    NULLABLE = frozenset({"display_name"})
    id: UUID
    display_name: str | None = None
    erased: bool | None = None
    safety_score: float = Field(ge=0, le=100)
    confidence: Literal["low", "ok"]
    km_30d: float | None = None


class DriverPage(Page):
    items: list[Driver]


class Per100km(ApiModel):
    harsh_brake: float | None = None
    harsh_accel: float | None = None
    overspeed_min: float | None = None
    night_share: float | None = None
    idle_share: float | None = None


class CoachingLever(ApiModel):
    lever: str | None = None
    value: float | None = None
    fleet_p50: float | None = None
    message: str | None = None


class DriverDetail(Driver):
    per_100km: Per100km | None = None
    coaching: list[CoachingLever] | None = None


class CostSeries(ApiModel):
    key: str | None = None
    energy_cost: float | None = None
    idle_cost: float | None = None
    distance_km: float | None = None


class CostReport(ApiModel):
    currency: str | None = "USD"
    total_cost: float | None = None
    idle_cost: float | None = None
    savings_opportunity: float | None = None
    series: list[CostSeries] | None = None


class BatchReportRequest(RequestModel):
    report_id: Literal["fuel_idle_by_depot", "risk_by_sw_version", "utilization_by_hour", "carbon_by_fleet"]
    params: dict[str, Any] | None = None


class BatchReportResult(ApiModel):
    report_id: str | None = None
    rows_scanned: int | None = None
    elapsed_ms: int | None = None
    columns: list[str] | None = None
    rows: list[list[Any]] | None = None


class InsightCell(ApiModel):
    geohash: str | None = None
    vehicles_approx: int | None = None
    idle_hours: float | None = None
    harsh_events: int | None = None


class InsightExport(ApiModel):
    k_anonymity: int | None = None
    location_precision: str | None = None
    cells: list[InsightCell] | None = None
    suppressed_cells: int | None = None
    totals: dict[str, float] | None = None


class AgentChatRequest(RequestModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: UUID | None = None


class AgentAction(ApiModel):
    NULLABLE = frozenset({"decided_at", "work_order_id"})
    id: UUID
    status: Literal["pending", "approved", "rejected"]
    proposal: dict[str, Any]
    rationale: str | None = None
    expected_impact_usd: float | None = None
    created_at: datetime | None = None
    decided_at: datetime | None = None
    work_order_id: UUID | None = None


class AgentAnswer(ApiModel):
    conversation_id: UUID | None = None
    answer: str
    grounded: bool
    tool_calls: list[dict[str, Any]] | None = None
    proposed_actions: list[AgentAction] | None = None
    degraded: bool | None = None


class BriefingItem(ApiModel):
    rank: int | None = None
    kind: Literal["cost", "maintenance", "safety", "ops"] | None = None
    title: str | None = None
    detail: str | None = None
    impact_usd: float | None = None
    action: AgentAction | None = None


class Briefing(ApiModel):
    generated_at: datetime | None = None
    source: Literal["llm", "rules"] | None = None
    headline: str | None = None
    total_impact_usd: float | None = None
    items: list[BriefingItem] | None = None


class ActionProposalCreate(RequestModel):
    alert_id: UUID | None = None
    proposal: dict[str, Any]
    rationale: str = Field(max_length=1000)
    expected_impact_usd: float | None = Field(None, ge=0)


class ActionPage(Page):
    items: list[AgentAction]


class RejectBody(RequestModel):
    reason: str | None = Field(None, max_length=500)


class MappingSummary(ApiModel):
    NULLABLE = frozenset({"activated_at"})
    oem: str | None = None
    version: int | None = None
    status: Literal["draft", "approved", "active", "retired"] | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    activated_at: datetime | None = None


class MappingDraft(RequestModel):
    oem: str
    document: dict[str, Any]
    source: Literal["human", "copilot"] | None = None


class MappingPreviewRequest(RequestModel):
    document: dict[str, Any]
    sample: dict[str, Any] | list[Any] | str


class PreviewFailure(ApiModel):
    reason: str | None = None
    detail: str | None = None
    raw: Any | None = None


class MappingPreviewResult(ApiModel):
    ok: list[dict[str, Any]] | None = None
    failed: list[PreviewFailure] | None = None


class FaultRate(ApiModel):
    sw_version: str | None = None
    vehicles: int | None = None
    dtc_per_1000km: float | None = None


class OemQuality(ApiModel):
    NULLABLE = frozenset({"active_mapping_version"})
    oem: str | None = None
    active_mapping_version: int | None = None
    records_1h: int | None = None
    failure_rate_1h: float | None = None
    drift_suspected: bool | None = None
    drift_detail: str | None = None
    quality_score: float | None = Field(None, ge=0, le=100)
    fault_rate_by_sw_version: list[FaultRate] | None = None


class DlqItem(ApiModel):
    oem: str | None = None
    error_reason: str | None = None
    ts: datetime | None = None
    payload: Any | None = None


class DlqReplayRequest(RequestModel):
    oem: str
    reason: str | None = None
    limit: int | None = Field(None, le=1_000_000)


class DlqReplayResult(ApiModel):
    job_id: str | None = None
    records: int | None = None


class AuditEntry(ApiModel):
    seq: int | None = None
    ts: datetime | None = None
    actor_id: str | None = None
    actor_type: str | None = None
    action: str | None = None
    resource_type: str | None = None
    resource_ids: list[str] | None = None
    purpose: str | None = None
    decision: Literal["ALLOW", "DENY"] | None = None
    reason: str | None = None
    hash: str | None = None


class AuditPage(Page):
    items: list[AuditEntry]


class AuditVerify(ApiModel):
    NULLABLE = frozenset({"first_bad_seq", "anchored_day"})
    ok: bool
    checked: int
    first_bad_seq: int | None = None
    head_hash: str | None = None
    anchored_day: date | None = None


class DsrCreate(RequestModel):
    driver_id: UUID
    kind: Literal["ERASE", "ACCESS"]


class Dsr(ApiModel):
    NULLABLE = frozenset({"completed_at"})
    id: UUID | None = None
    kind: Literal["ERASE", "ACCESS"] | None = None
    status: Literal["received", "verified", "completed", "rejected"] | None = None
    requested_at: datetime | None = None
    completed_at: datetime | None = None
    evidence: dict[str, Any] | None = None
