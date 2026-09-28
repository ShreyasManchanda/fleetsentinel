from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class VehicleStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    VEHICLE_STATUS_UNSPECIFIED: _ClassVar[VehicleStatus]
    PARKED: _ClassVar[VehicleStatus]
    DRIVING: _ClassVar[VehicleStatus]
    IDLING: _ClassVar[VehicleStatus]
    CHARGING: _ClassVar[VehicleStatus]
    OFFLINE: _ClassVar[VehicleStatus]
    TOWED: _ClassVar[VehicleStatus]
VEHICLE_STATUS_UNSPECIFIED: VehicleStatus
PARKED: VehicleStatus
DRIVING: VehicleStatus
IDLING: VehicleStatus
CHARGING: VehicleStatus
OFFLINE: VehicleStatus
TOWED: VehicleStatus

class VehicleState(_message.Message):
    __slots__ = ("vin", "tenant_id", "ts_ms", "lat", "lon", "speed_kmh", "status", "soc_pct", "fuel_pct", "ignition", "risk_score", "open_alerts", "ingest_ts_ms", "oem")
    VIN_FIELD_NUMBER: _ClassVar[int]
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    TS_MS_FIELD_NUMBER: _ClassVar[int]
    LAT_FIELD_NUMBER: _ClassVar[int]
    LON_FIELD_NUMBER: _ClassVar[int]
    SPEED_KMH_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SOC_PCT_FIELD_NUMBER: _ClassVar[int]
    FUEL_PCT_FIELD_NUMBER: _ClassVar[int]
    IGNITION_FIELD_NUMBER: _ClassVar[int]
    RISK_SCORE_FIELD_NUMBER: _ClassVar[int]
    OPEN_ALERTS_FIELD_NUMBER: _ClassVar[int]
    INGEST_TS_MS_FIELD_NUMBER: _ClassVar[int]
    OEM_FIELD_NUMBER: _ClassVar[int]
    vin: str
    tenant_id: str
    ts_ms: int
    lat: float
    lon: float
    speed_kmh: float
    status: VehicleStatus
    soc_pct: float
    fuel_pct: float
    ignition: bool
    risk_score: float
    open_alerts: int
    ingest_ts_ms: int
    oem: str
    def __init__(self, vin: _Optional[str] = ..., tenant_id: _Optional[str] = ..., ts_ms: _Optional[int] = ..., lat: _Optional[float] = ..., lon: _Optional[float] = ..., speed_kmh: _Optional[float] = ..., status: _Optional[_Union[VehicleStatus, str]] = ..., soc_pct: _Optional[float] = ..., fuel_pct: _Optional[float] = ..., ignition: _Optional[bool] = ..., risk_score: _Optional[float] = ..., open_alerts: _Optional[int] = ..., ingest_ts_ms: _Optional[int] = ..., oem: _Optional[str] = ...) -> None: ...
