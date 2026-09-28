from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ActorType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ACTOR_TYPE_UNSPECIFIED: _ClassVar[ActorType]
    USER: _ClassVar[ActorType]
    SERVICE: _ClassVar[ActorType]
    AGENT: _ClassVar[ActorType]
    SYSTEM: _ClassVar[ActorType]

class Decision(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DECISION_UNSPECIFIED: _ClassVar[Decision]
    ALLOW: _ClassVar[Decision]
    DENY: _ClassVar[Decision]
ACTOR_TYPE_UNSPECIFIED: ActorType
USER: ActorType
SERVICE: ActorType
AGENT: ActorType
SYSTEM: ActorType
DECISION_UNSPECIFIED: Decision
ALLOW: Decision
DENY: Decision

class AuditEvent(_message.Message):
    __slots__ = ("event_id", "ts_ms", "tenant_id", "actor_id", "actor_type", "action", "resource_type", "resource_ids", "purpose", "decision", "reason", "latency_ms", "attributes")
    class AttributesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    TS_MS_FIELD_NUMBER: _ClassVar[int]
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_TYPE_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_TYPE_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_IDS_FIELD_NUMBER: _ClassVar[int]
    PURPOSE_FIELD_NUMBER: _ClassVar[int]
    DECISION_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    LATENCY_MS_FIELD_NUMBER: _ClassVar[int]
    ATTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    ts_ms: int
    tenant_id: str
    actor_id: str
    actor_type: ActorType
    action: str
    resource_type: str
    resource_ids: _containers.RepeatedScalarFieldContainer[str]
    purpose: str
    decision: Decision
    reason: str
    latency_ms: int
    attributes: _containers.ScalarMap[str, str]
    def __init__(self, event_id: _Optional[str] = ..., ts_ms: _Optional[int] = ..., tenant_id: _Optional[str] = ..., actor_id: _Optional[str] = ..., actor_type: _Optional[_Union[ActorType, str]] = ..., action: _Optional[str] = ..., resource_type: _Optional[str] = ..., resource_ids: _Optional[_Iterable[str]] = ..., purpose: _Optional[str] = ..., decision: _Optional[_Union[Decision, str]] = ..., reason: _Optional[str] = ..., latency_ms: _Optional[int] = ..., attributes: _Optional[_Mapping[str, str]] = ...) -> None: ...
