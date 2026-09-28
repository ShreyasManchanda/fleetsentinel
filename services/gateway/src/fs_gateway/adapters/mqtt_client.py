"""aiomqtt subscriber (mTLS, shared subscriptions, manual QoS1 acks). Integration-tested; excluded from the unit gate."""
from __future__ import annotations

import asyncio
import ssl
import time

import aiomqtt
import structlog

from ..application.pipeline import GatewayPipeline, Item
from ..settings import Settings

log = structlog.get_logger("fs_gateway.mqtt")


def tls_context(s: Settings) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(s.certs_dir / s.ca_file))
    ctx.load_cert_chain(str(s.certs_dir / s.cert_file), str(s.certs_dir / s.key_file))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    return ctx


def _props(message: aiomqtt.Message) -> dict[str, str]:
    up = getattr(getattr(message, "properties", None), "UserProperty", None) or []
    return {k: v for k, v in up}


async def run_subscriber(s: Settings, pipeline: GatewayPipeline, stop: asyncio.Event) -> None:
    backoff = 1.0
    while not stop.is_set():
        try:
            async with aiomqtt.Client(
                s.mqtt_host, s.mqtt_port, tls_context=tls_context(s), identifier=f"gw-{time.time_ns() % 10**9}",
                protocol=aiomqtt.ProtocolVersion.V5, keepalive=30, max_queued_incoming_messages=1000,
                clean_start=True,
            ) as client:
                client._client.manual_ack_set(True)          # ack only after Kafka/spill (paho private API)
                await client.subscribe(f"$share/{s.shared_group}/telemetry/#", qos=1)
                await client.subscribe(f"$share/{s.shared_group}/event/#", qos=1)
                log.info("mqtt_subscribed", host=s.mqtt_host, group=s.shared_group)
                backoff = 1.0
                async for message in client.messages:
                    ack = (lambda m=message: client._client.ack(m.mid, m.qos)) if message.qos > 0 else None
                    await pipeline.submit(Item(str(message.topic), bytes(message.payload), int(time.time() * 1000), ack, _props(message)))
                    if stop.is_set():
                        break
        except aiomqtt.MqttError as e:
            log.warning("mqtt_disconnected", error=str(e), retry_in_s=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)