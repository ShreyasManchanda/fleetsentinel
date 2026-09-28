"""aiomqtt transport: one mTLS client per OEM connector (client cert CN = MQTT username; ACL limits each to its own prefix)."""
from __future__ import annotations

import contextlib
from pathlib import Path

import aiomqtt
import structlog

from ..constants import OEM_CODES, certs_dir

log = structlog.get_logger("fs_simulator.mqtt")


class MqttTransport:
    """Async context manager. publish() routes by the OEM segment of the topic (telemetry/{oem}/{shard})."""

    def __init__(self, host: str = "localhost", port: int = 8883, certs: Path | None = None, oems=OEM_CODES,
                 max_inflight: int = 2000):
        self.host, self.port, self.certs, self.oems, self.max_inflight = host, port, Path(certs or certs_dir()), oems, max_inflight
        self.clients: dict[str, aiomqtt.Client] = {}
        self._stack = contextlib.AsyncExitStack()

    async def __aenter__(self) -> MqttTransport:
        await self._stack.__aenter__()
        for oem in self.oems:
            tls = aiomqtt.TLSParameters(ca_certs=str(self.certs / "ca.crt"), certfile=str(self.certs / f"{oem}.crt"),
                                        keyfile=str(self.certs / f"{oem}.key"))
            client = aiomqtt.Client(hostname=self.host, port=self.port, identifier=f"fs-sim-{oem}", tls_params=tls,
                                    max_inflight_messages=self.max_inflight)
            self.clients[oem] = await self._stack.enter_async_context(client)
            log.info("mqtt_connected", oem=oem, host=self.host, port=self.port)
        return self

    async def __aexit__(self, *exc) -> bool | None:
        return await self._stack.__aexit__(*exc)

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        await self.clients[topic.split("/")[1]].publish(topic, payload, qos=qos)
