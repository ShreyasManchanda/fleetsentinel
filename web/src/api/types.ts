// Hand-written types mirroring contracts/openapi.yaml schemas (source of truth).
// Field names/types kept identical so switching mock -> real is a base-URL change.

export type Severity = 'INFO' | 'WARNING' | 'CRITICAL';
export type AlertStatus = 'open' | 'ack' | 'resolved';
export type RuleCode =
  | 'TOW' | 'CRASH' | 'GEOFENCE_BREACH' | 'CRITICAL_DTC' | 'LOW_SOC_STRANDING'
  | 'GOING_DARK' | 'EXCESSIVE_IDLING' | 'HARSH_DRIVING' | 'MAINTENANCE_RISK'
  | 'UNAPPROVED_DEPOT_CLUSTER' | 'ODOMETER_ROLLBACK' | 'SCHEMA_DRIFT';
export type VehicleStatus = 'PARKED' | 'DRIVING' | 'IDLING' | 'CHARGING' | 'OFFLINE' | 'TOWED';
export type Powertrain = 'ICE' | 'EV' | 'HYBRID';
export type Purpose = 'OPERATIONS' | 'RECOVERY' | 'SAFETY_REVIEW' | 'COMPLIANCE';

export interface Problem {
  type: string;
  title: string;
  status: number;
  detail?: string;
  instance?: string;
  trace_id?: string;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface Me {
  sub: string;
  tenant_id: string;
  tenant_name?: string;
  roles: string[];
  email?: string;
}

export interface FleetSummary {
  vehicles_total: number;
  vehicles_by_status: Record<string, number>;
  leaking_usd_today: number;
  savings_opportunity_usd_week: number;
  at_risk_vehicles: number;
  open_critical_alerts: number;
  freshness_p95_ms: number;
  model_fallback_active?: boolean;
  as_of: string;
}

export interface Vehicle {
  vin: string;
  fleet_id: string;
  oem: string;
  model: string;
  powertrain: Powertrain;
  status?: VehicleStatus;
  last_risk_score?: number;
  open_alerts?: number;
  lat?: number;
  lon?: number;
  last_seen?: string;
}

export interface RiskReasonCode {
  feature: string;
  contribution: number;
  human?: string;
}

export interface RiskDetail {
  p_failure_7d: number;
  model_version: string;
  fallback?: boolean;
  reason_codes?: RiskReasonCode[];
  expected_value_usd?: number;
  recommendation: 'INSPECT' | 'MONITOR' | 'NONE';
  predicted_at?: string;
}

export interface Alert {
  id: string;
  vin: string;
  rule: RuleCode;
  severity: Severity;
  status: AlertStatus;
  occurrences?: number;
  summary?: string;
  details?: Record<string, unknown>;
  impact_usd?: number;
  opened_at: string;
  event_ts?: string;
  detected_ts?: string;
  latency_ms?: number;
  lat?: number;
  lon?: number;
  trace_id?: string;
}

export interface Trip {
  id: string;
  start_ts: string;
  end_ts: string;
  distance_km: number;
  idle_s?: number;
  energy_used?: number;
  energy_cost?: number;
  harsh_brake_count?: number;
  harsh_accel_count?: number;
  driver_ref?: string;
}

export interface Vehicle360 {
  vehicle: Vehicle;
  twin?: Record<string, unknown>;
  risk?: RiskDetail;
  recent_alerts?: Alert[];
  recent_trips?: Trip[];
  location_precision?: 'exact' | 'geohash7' | 'geohash6' | 'geohash5';
}

export interface Lookalike {
  vin: string;
  similarity: number;
  failed_within_7d?: boolean | null;
  note?: string;
}

export interface MapPoint {
  vin: string;
  lat: number;
  lon: number;
  status: VehicleStatus;
}

export interface MapCell {
  geohash: string;
  count: number;
  idle: number;
}

export interface MapViewport {
  mode?: 'points' | 'cells';
  points?: MapPoint[];
  cells?: MapCell[];
  truncated?: boolean;
}

export interface Driver {
  id: string;
  display_name?: string | null;
  erased?: boolean;
  safety_score: number;
  confidence: 'low' | 'ok';
  km_30d?: number;
}

export interface CoachingLever {
  lever: string;
  value: number;
  fleet_p50: number;
  message?: string;
}

export interface DriverDetail extends Driver {
  per_100km?: {
    harsh_brake: number;
    harsh_accel: number;
    overspeed_min: number;
    night_share: number;
    idle_share: number;
  };
  coaching?: CoachingLever[];
}

export interface CostReport {
  currency?: string;
  total_cost?: number;
  idle_cost?: number;
  savings_opportunity?: number;
  series?: { key: string; energy_cost: number; idle_cost: number; distance_km: number }[];
}

export interface AgentAction {
  id: string;
  status: 'pending' | 'approved' | 'rejected';
  proposal: Record<string, unknown>;
  rationale?: string;
  expected_impact_usd?: number;
  created_at?: string;
  decided_at?: string | null;
  work_order_id?: string | null;
}

export interface AgentAnswer {
  conversation_id?: string;
  answer: string;
  grounded: boolean;
  tool_calls?: { tool: string; args: Record<string, unknown>; result_hash: string }[];
  proposed_actions?: AgentAction[];
  degraded?: boolean;
}

export interface BriefingItem {
  rank: number;
  kind: 'cost' | 'maintenance' | 'safety' | 'ops';
  title: string;
  detail?: string;
  impact_usd?: number;
  action?: AgentAction;
}

export interface Briefing {
  generated_at?: string;
  source?: 'llm' | 'rules';
  headline?: string;
  total_impact_usd?: number;
  items?: BriefingItem[];
}

export interface MappingSummary {
  oem?: string;
  version?: number;
  status?: 'draft' | 'approved' | 'active' | 'retired';
  created_by?: string;
  created_at?: string;
  activated_at?: string | null;
}

export interface MappingPreviewResult {
  ok?: Record<string, unknown>[];
  failed?: { reason: string; detail?: string; raw?: unknown }[];
}

export interface OemQuality {
  oem?: string;
  active_mapping_version?: number | null;
  records_1h?: number;
  failure_rate_1h?: number;
  drift_suspected?: boolean;
  drift_detail?: string;
  quality_score?: number;
  fault_rate_by_sw_version?: { sw_version: string; vehicles: number; dtc_per_1000km: number }[];
}

export interface DlqItem {
  oem?: string;
  error_reason?: string;
  ts?: string;
  payload?: unknown;
}

export interface AuditEntry {
  seq: number;
  ts: string;
  actor_id: string;
  actor_type?: string;
  action: string;
  resource_type?: string;
  resource_ids?: string[];
  purpose?: string;
  decision: 'ALLOW' | 'DENY';
  reason?: string;
  hash: string;
}

export interface AuditVerify {
  ok: boolean;
  checked: number;
  first_bad_seq?: number | null;
  head_hash: string;
  anchored_day?: string | null;
}

export interface Dsr {
  id: string;
  kind: 'ERASE' | 'ACCESS';
  status: 'received' | 'verified' | 'completed' | 'rejected';
  requested_at?: string;
  completed_at?: string | null;
  evidence?: Record<string, unknown>;
}

export interface InsightExport {
  k_anonymity: number;
  location_precision: string;
  cells?: { geohash: string; vehicles_approx: number; idle_hours: number; harsh_events: number }[];
  suppressed_cells?: number;
  totals?: Record<string, number>;
}
