from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Severity(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SEVERITY_UNSPECIFIED: _ClassVar[Severity]
    INFO: _ClassVar[Severity]
    WARNING: _ClassVar[Severity]
    CRITICAL: _ClassVar[Severity]

class RuleCode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RULE_CODE_UNSPECIFIED: _ClassVar[RuleCode]
    RULE_TOW: _ClassVar[RuleCode]
    RULE_CRASH: _ClassVar[RuleCode]
    RULE_GEOFENCE_BREACH: _ClassVar[RuleCode]
    RULE_CRITICAL_DTC: _ClassVar[RuleCode]
    RULE_LOW_SOC_STRANDING: _ClassVar[RuleCode]
    RULE_GOING_DARK: _ClassVar[RuleCode]
    RULE_EXCESSIVE_IDLING: _ClassVar[RuleCode]
    RULE_HARSH_DRIVING: _ClassVar[RuleCode]
    RULE_MAINTENANCE_RISK: _ClassVar[RuleCode]
    RULE_UNAPPROVED_DEPOT_CLUSTER: _ClassVar[RuleCode]
    RULE_ODOMETER_ROLLBACK: _ClassVar[RuleCode]
    RULE_SCHEMA_DRIFT: _ClassVar[RuleCode]
SEVERITY_UNSPECIFIED: Severity
INFO: Severity
WARNING: Severity
CRITICAL: Severity
RULE_CODE_UNSPECIFIED: RuleCode
RULE_TOW: RuleCode
RULE_CRASH: RuleCode
RULE_GEOFENCE_BREACH: RuleCode
RULE_CRITICAL_DTC: RuleCode
RULE_LOW_SOC_STRANDING: RuleCode
RULE_GOING_DARK: RuleCode
RULE_EXCESSIVE_IDLING: RuleCode
RULE_HARSH_DRIVING: RuleCode
RULE_MAINTENANCE_RISK: RuleCode
RULE_UNAPPROVED_DEPOT_CLUSTER: RuleCode
RULE_ODOMETER_ROLLBACK: RuleCode
RULE_SCHEMA_DRIFT: RuleCode

class Alert(_message.Message):
    __slots__ = ("alert_id", "tenant_id", "vin", "rule", "severity", "window_start_ms", "event_ts_ms", "detected_ts_ms", "lat", "lon", "summary", "details", "impact_usd", "ingest_ts_ms", "trace_id")
    class DetailsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ALERT_ID_FIELD_NUMBER: _ClassVar[int]
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    VIN_FIELD_NUMBER: _ClassVar[int]
    RULE_FIELD_NUMBER: _ClassVar[int]
    SEVERITY_FIELD_NUMBER: _ClassVar[int]
    WINDOW_START_MS_FIELD_NUMBER: _ClassVar[int]
    EVENT_TS_MS_FIELD_NUMBER: _ClassVar[int]
    DETECTED_TS_MS_FIELD_NUMBER: _ClassVar[int]
    LAT_FIELD_NUMBER: _ClassVar[int]
    LON_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    DETAILS_FIELD_NUMBER: _ClassVar[int]
    IMPACT_USD_FIELD_NUMBER: _ClassVar[int]
    INGEST_TS_MS_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    alert_id: str
    tenant_id: str
    vin: str
    rule: RuleCode
    severity: Severity
    window_start_ms: int
    event_ts_ms: int
    detected_ts_ms: int
    lat: float
    lon: float
    summary: str
    details: _containers.ScalarMap[str, str]
    impact_usd: float
    ingest_ts_ms: int
    trace_id: str
    def __init__(self, alert_id: _Optional[str] = ..., tenant_id: _Optional[str] = ..., vin: _Optional[str] = ..., rule: _Optional[_Union[RuleCode, str]] = ..., severity: _Optional[_Union[Severity, str]] = ..., window_start_ms: _Optional[int] = ..., event_ts_ms: _Optional[int] = ..., detected_ts_ms: _Optional[int] = ..., lat: _Optional[float] = ..., lon: _Optional[float] = ..., summary: _Optional[str] = ..., details: _Optional[_Mapping[str, str]] = ..., impact_usd: _Optional[float] = ..., ingest_ts_ms: _Optional[int] = ..., trace_id: _Optional[str] = ...) -> None: ...
