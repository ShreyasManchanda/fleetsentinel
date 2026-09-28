from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class EventType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EVENT_TYPE_UNSPECIFIED: _ClassVar[EventType]
    POSITION: _ClassVar[EventType]
    HARSH_BRAKE: _ClassVar[EventType]
    HARSH_ACCEL: _ClassVar[EventType]
    IGNITION_ON: _ClassVar[EventType]
    IGNITION_OFF: _ClassVar[EventType]
    TOW: _ClassVar[EventType]
    CRASH: _ClassVar[EventType]
    CHARGING_START: _ClassVar[EventType]
    CHARGING_END: _ClassVar[EventType]
    DTC_SET: _ClassVar[EventType]
    OVERSPEED: _ClassVar[EventType]
EVENT_TYPE_UNSPECIFIED: EventType
POSITION: EventType
HARSH_BRAKE: EventType
HARSH_ACCEL: EventType
IGNITION_ON: EventType
IGNITION_OFF: EventType
TOW: EventType
CRASH: EventType
CHARGING_START: EventType
CHARGING_END: EventType
DTC_SET: EventType
OVERSPEED: EventType

class TelemetryEvent(_message.Message):
    __slots__ = ("vin", "ts_ms", "seq", "lat", "lon", "speed_kmh", "soc_pct", "fuel_pct", "odo_km", "dtc", "evt", "ignition", "sw_version", "oem", "ingest_ts_ms", "driver_ref_enc", "soh_pct", "engine_temp_c")
    VIN_FIELD_NUMBER: _ClassVar[int]
    TS_MS_FIELD_NUMBER: _ClassVar[int]
    SEQ_FIELD_NUMBER: _ClassVar[int]
    LAT_FIELD_NUMBER: _ClassVar[int]
    LON_FIELD_NUMBER: _ClassVar[int]
    SPEED_KMH_FIELD_NUMBER: _ClassVar[int]
    SOC_PCT_FIELD_NUMBER: _ClassVar[int]
    FUEL_PCT_FIELD_NUMBER: _ClassVar[int]
    ODO_KM_FIELD_NUMBER: _ClassVar[int]
    DTC_FIELD_NUMBER: _ClassVar[int]
    EVT_FIELD_NUMBER: _ClassVar[int]
    IGNITION_FIELD_NUMBER: _ClassVar[int]
    SW_VERSION_FIELD_NUMBER: _ClassVar[int]
    OEM_FIELD_NUMBER: _ClassVar[int]
    INGEST_TS_MS_FIELD_NUMBER: _ClassVar[int]
    DRIVER_REF_ENC_FIELD_NUMBER: _ClassVar[int]
    SOH_PCT_FIELD_NUMBER: _ClassVar[int]
    ENGINE_TEMP_C_FIELD_NUMBER: _ClassVar[int]
    vin: str
    ts_ms: int
    seq: int
    lat: float
    lon: float
    speed_kmh: float
    soc_pct: float
    fuel_pct: float
    odo_km: float
    dtc: _containers.RepeatedScalarFieldContainer[str]
    evt: EventType
    ignition: bool
    sw_version: str
    oem: str
    ingest_ts_ms: int
    driver_ref_enc: bytes
    soh_pct: float
    engine_temp_c: float
    def __init__(self, vin: _Optional[str] = ..., ts_ms: _Optional[int] = ..., seq: _Optional[int] = ..., lat: _Optional[float] = ..., lon: _Optional[float] = ..., speed_kmh: _Optional[float] = ..., soc_pct: _Optional[float] = ..., fuel_pct: _Optional[float] = ..., odo_km: _Optional[float] = ..., dtc: _Optional[_Iterable[str]] = ..., evt: _Optional[_Union[EventType, str]] = ..., ignition: _Optional[bool] = ..., sw_version: _Optional[str] = ..., oem: _Optional[str] = ..., ingest_ts_ms: _Optional[int] = ..., driver_ref_enc: _Optional[bytes] = ..., soh_pct: _Optional[float] = ..., engine_temp_c: _Optional[float] = ...) -> None: ...
