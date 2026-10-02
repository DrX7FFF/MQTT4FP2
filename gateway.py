"""Passerelle asynchrone entre un accessoire HomeKit et MQTT."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiomqtt
from aiohomekit import Controller
from aiohomekit.zeroconf import ZeroconfServiceListener
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf


LOGGER = logging.getLogger("fp2.gateway")
HAP_SERVICES = ["_hap._tcp.local.", "_hap._udp.local."]
CHARACTERISTIC_NAME = "00000023-0000-1000-8000-0026BB765291"
CHARACTERISTIC_CONFIGURED_NAME = "000000E3-0000-1000-8000-0026BB765291"
CHARACTERISTIC_OCCUPANCY = "00000071-0000-1000-8000-0026BB765291"
CHARACTERISTIC_LIGHT_LEVEL = "0000006B-0000-1000-8000-0026BB765291"
GLOBAL_NAMES = {
    "global occupancy",
    "global presence",
    "occupancy",
    "occupancy sensor",
    "presence",
    "presence globale",
    "presence global",
    "presence sensor",
}
BACKOFF_MAX_SECONDS = 60


@dataclass(frozen=True)
class GatewayConfig:
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str | None
    mqtt_password: str | None
    topic_prefix: str
    pairing_file: Path
    pairing_alias: str
    log_level: str
    diagnostic_interval: float
    fallback_interval: float

    @classmethod
    def from_env(cls) -> GatewayConfig:
        prefix = os.getenv("MQTT_PREFIX", "fp2").strip("/") or "fp2"
        if "+" in prefix or "#" in prefix:
            raise ValueError("MQTT_PREFIX ne peut pas contenir + ou #.")

        return cls(
            mqtt_host=os.getenv("MQTT_HOST", "127.0.0.1"),
            mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
            mqtt_username=os.getenv("MQTT_USERNAME") or None,
            mqtt_password=os.getenv("MQTT_PASSWORD") or None,
            topic_prefix=prefix,
            pairing_file=Path(os.getenv("PAIRING_FILE", "data/pairing.json")),
            pairing_alias=os.getenv("PAIRING_ALIAS", "fp2"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            diagnostic_interval=max(1.0, float(os.getenv("DIAGNOSTIC_INTERVAL", "30"))),
            fallback_interval=max(5.0, float(os.getenv("FALLBACK_INTERVAL", "60"))),
        )


@dataclass(frozen=True)
class Entity:
    kind: str
    topic: str
    aid: int
    iid: int
    name: str


@dataclass
class AccessoryMapping:
    entities: dict[tuple[int, int], Entity] = field(default_factory=dict)
    event_keys: set[tuple[int, int]] = field(default_factory=set)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _uuid(value: Any) -> str:
    text = str(value or "").upper()
    if re.fullmatch(r"[0-9A-F]{8}", text):
        return f"{text}-0000-1000-8000-0026BB765291"
    return text


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", ascii_text)).strip("-")


def _service_name(service: dict[str, Any]) -> str:
    for characteristic in service.get("characteristics", []):
        if _uuid(characteristic.get("type")) in {
            CHARACTERISTIC_CONFIGURED_NAME,
            CHARACTERISTIC_NAME,
        }:
            value = characteristic.get("value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    value = service.get("name")
    return value.strip() if isinstance(value, str) else ""


def _is_global_name(name: str) -> bool:
    return _slug(name).replace("-", " ") in GLOBAL_NAMES


def build_mapping(accessories: list[dict[str, Any]], prefix: str) -> AccessoryMapping:
    mapping = AccessoryMapping()
    occupancy: list[tuple[int, int, str, bool]] = []
    illuminance: list[tuple[int, int, str]] = []
    zone_slugs: set[str] = set()

    for accessory in accessories:
        aid = int(accessory.get("aid", 0))
        for service in accessory.get("services", []):
            name = _service_name(service)
            for characteristic in service.get("characteristics", []):
                characteristic_type = _uuid(characteristic.get("type"))
                key = (aid, int(characteristic.get("iid", 0)))
                permissions = characteristic.get("perms", [])
                if "ev" in permissions:
                    mapping.event_keys.add(key)

                if characteristic_type == CHARACTERISTIC_OCCUPANCY:
                    occupancy.append((key[0], key[1], name, "ev" in permissions))
                elif characteristic_type == CHARACTERISTIC_LIGHT_LEVEL:
                    illuminance.append((key[0], key[1], name))

    named_global = [candidate for candidate in occupancy if _is_global_name(candidate[2])]
    global_sensor: tuple[int, int, str, bool] | None = None
    if len(occupancy) == 1:
        global_sensor = occupancy[0]
    elif len(named_global) == 1:
        global_sensor = named_global[0]
    elif len(named_global) > 1:
        mapping.warnings.append("Plusieurs capteurs portent un nom de présence globale.")
    elif occupancy:
        mapping.warnings.append(
            "Capteur de présence global ambigu : aucun nom exposé ne permet de le distinguer."
        )

    if global_sensor:
        aid, iid, name, _ = global_sensor
        mapping.entities[(aid, iid)] = Entity("presence", f"{prefix}/presence", aid, iid, name)

    for aid, iid, name, _ in occupancy:
        if global_sensor and (aid, iid) == global_sensor[:2]:
            continue
        slug = _slug(name)
        if not slug or _is_global_name(name):
            mapping.unresolved.append(
                {"aid": aid, "iid": iid, "type": "presence", "name": name or None}
            )
            continue
        if slug in zone_slugs:
            slug = f"{slug}-{aid}-{iid}"
        zone_slugs.add(slug)
        mapping.entities[(aid, iid)] = Entity(
            "zone", f"{prefix}/zone/{slug}", aid, iid, name
        )

    if len(illuminance) == 1:
        aid, iid, name = illuminance[0]
        mapping.entities[(aid, iid)] = Entity(
            "illuminance", f"{prefix}/illuminance", aid, iid, name
        )
    elif len(illuminance) > 1:
        named_global_light = [candidate for candidate in illuminance if _is_global_name(candidate[2])]
        if len(named_global_light) == 1:
            aid, iid, name = named_global_light[0]
            mapping.entities[(aid, iid)] = Entity(
                "illuminance", f"{prefix}/illuminance", aid, iid, name
            )
        else:
            mapping.warnings.append(
                "Plusieurs capteurs de luminosité sont exposés; le topic principal reste ambigu."
            )
            mapping.unresolved.extend(
                {"aid": aid, "iid": iid, "type": "illuminance", "name": name or None}
                for aid, iid, name in illuminance
                if (aid, iid) not in mapping.entities
            )

    return mapping


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def publish_json(
    mqtt: Any,
    topic: str,
    payload: dict[str, Any],
    *,
    retain: bool = True,
) -> None:
    await mqtt.publish(
        topic,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        qos=1,
        retain=retain,
    )


async def publish_entity(mqtt: Any, entity: Entity, value: Any) -> None:
    await publish_json(
        mqtt,
        entity.topic,
        {"value": value, "timestamp": timestamp()},
    )


class Gateway:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self._mqtt: Any = None

    def _topic(self, suffix: str) -> str:
        return f"{self.config.topic_prefix}/{suffix}"

    async def _diagnostic(self, suffix: str, state: str, **details: Any) -> None:
        payload = {"state": state, "timestamp": timestamp(), **details}
        await publish_json(self._mqtt, self._topic(f"diagnostic/{suffix}"), payload)

    async def _publish_status(self, mqtt: Any, state: str) -> None:
        await mqtt.publish(self._topic("status"), state, qos=1, retain=True)

    async def _publish_updates(
        self,
        mapping: AccessoryMapping,
        updates: dict[tuple[int, int], dict[str, Any]],
    ) -> None:
        for key, result in updates.items():
            if "value" not in result:
                continue
            entity = mapping.entities.get(key)
            if entity:
                await publish_entity(self._mqtt, entity, result["value"])
            else:
                await publish_json(
                    self._mqtt,
                    self._topic(f"diagnostic/characteristic/{key[0]}/{key[1]}"),
                    {"value": result["value"], "timestamp": timestamp()},
                )

    async def _read_current_values(
        self,
        pairing: Any,
        mapping: AccessoryMapping,
    ) -> None:
        if not mapping.entities:
            return
        keys = list(mapping.entities)
        values = await pairing.get_characteristics(keys)
        await self._publish_updates(mapping, values)

    async def _subscribe(
        self,
        pairing: Any,
        old_keys: set[tuple[int, int]],
        new_keys: set[tuple[int, int]],
    ) -> None:
        removed = old_keys - new_keys
        added = new_keys - old_keys
        if removed:
            await pairing.unsubscribe(removed)
        if added:
            result = await pairing.subscribe(sorted(added))
            failures = {
                f"{aid}.{iid}": value
                for (aid, iid), value in (result or {}).items()
                if value.get("status", 0) != 0
            }
            if failures:
                LOGGER.warning("Certaines souscriptions HAP ont échoué : %s", failures)

    async def _serve_pairing(self, pairing: Any, mqtt: Any) -> None:
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=2048)
        loop = asyncio.get_running_loop()

        def enqueue(kind: str, value: Any) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, (kind, value))
            except RuntimeError:
                LOGGER.debug("Événement HomeKit ignoré après l'arrêt de la boucle.")

        disconnect_events = pairing.dispatcher_connect(lambda data: enqueue("event", data))
        disconnect_config = pairing.dispatcher_connect_config_changed(
            lambda config_num: enqueue("config", config_num)
        )

        try:
            accessories = await pairing.list_accessories_and_characteristics()
            mapping = build_mapping(accessories, self.config.topic_prefix)
            await self._subscribe(pairing, set(), mapping.event_keys)
            await self._read_current_values(pairing, mapping)

            await self._diagnostic(
                "homekit",
                "connected",
                accessory_count=len(accessories),
                config_num=pairing.config_num,
            )
            await self._publish_mapping(mapping, pairing.config_num)

            while True:
                try:
                    kind, value = await asyncio.wait_for(
                        queue.get(), timeout=self.config.fallback_interval
                    )
                except asyncio.TimeoutError:
                    if not pairing.is_available or not pairing.supports_subscribe:
                        await self._read_current_values(pairing, mapping)
                    continue

                if kind == "event":
                    await self._publish_updates(mapping, value)
                    await self._diagnostic("last_event", "received", count=len(value))
                    continue

                accessories = await pairing.list_accessories_and_characteristics()
                updated_mapping = build_mapping(accessories, self.config.topic_prefix)
                await self._subscribe(pairing, mapping.event_keys, updated_mapping.event_keys)
                mapping = updated_mapping
                await self._read_current_values(pairing, mapping)
                await self._publish_mapping(mapping, value)
        finally:
            disconnect_events()
            disconnect_config()

    async def _publish_mapping(self, mapping: AccessoryMapping, config_num: int) -> None:
        await self._diagnostic(
            "mapping",
            "updated",
            config_num=config_num,
            entities=[
                {"kind": entity.kind, "topic": entity.topic, "aid": entity.aid, "iid": entity.iid}
                for entity in mapping.entities.values()
            ],
            unresolved=mapping.unresolved,
            warnings=mapping.warnings,
        )
        for warning in mapping.warnings:
            LOGGER.warning("Mapping HAP incomplet : %s", warning)

    async def _homekit_reconnect_loop(self, mqtt: Any) -> None:
        delay = 1.0
        while True:
            try:
                await self._diagnostic("homekit", "connecting")
                async with AsyncZeroconf() as zeroconf:
                    browser = AsyncServiceBrowser(
                        zeroconf.zeroconf,
                        HAP_SERVICES,
                        listener=ZeroconfServiceListener(),
                    )
                    try:
                        async with Controller(async_zeroconf_instance=zeroconf) as controller:
                            controller.load_data(str(self.config.pairing_file))
                            pairing = controller.aliases.get(self.config.pairing_alias)
                            if pairing is None:
                                raise RuntimeError(
                                    f"Alias de pairing absent : {self.config.pairing_alias}"
                                )
                            delay = 1.0
                            await self._serve_pairing(pairing, mqtt)
                    finally:
                        await browser.async_cancel()
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError:
                raise
            except Exception as error:
                LOGGER.warning(
                    "Connexion HomeKit interrompue (%s); nouvel essai dans %.0f s.",
                    type(error).__name__,
                    delay,
                )
                await self._diagnostic("homekit", "disconnected", error=type(error).__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX_SECONDS)

    async def _diagnostic_heartbeat(self, mqtt: Any) -> None:
        sequence = 0
        while True:
            sequence += 1
            await self._diagnostic(
                "mqtt",
                "connected",
                broker=f"{self.config.mqtt_host}:{self.config.mqtt_port}",
                qos=1,
                sequence=sequence,
            )
            await self._diagnostic("heartbeat", "online", sequence=sequence)
            await asyncio.sleep(self.config.diagnostic_interval)

    async def _run_connected(self, mqtt: Any) -> None:
        self._mqtt = mqtt
        await self._publish_status(mqtt, "online")
        await self._diagnostic(
            "mqtt",
            "connected",
            broker=f"{self.config.mqtt_host}:{self.config.mqtt_port}",
            qos=1,
            sequence=0,
        )

        tasks = {
            asyncio.create_task(self._homekit_reconnect_loop(mqtt)),
            asyncio.create_task(self._diagnostic_heartbeat(mqtt)),
        }
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await self._publish_status(mqtt, "offline")
            except aiomqtt.MqttError:
                LOGGER.debug("Statut offline non publié avant la fermeture MQTT.")

    async def run(self) -> None:
        delay = 1.0
        while True:
            will = aiomqtt.Will(
                self._topic("status"),
                payload="offline",
                qos=1,
                retain=True,
            )
            try:
                async with aiomqtt.Client(
                    hostname=self.config.mqtt_host,
                    port=self.config.mqtt_port,
                    username=self.config.mqtt_username,
                    password=self.config.mqtt_password,
                    identifier="fp2-gateway",
                    will=will,
                ) as mqtt:
                    delay = 1.0
                    LOGGER.info(
                        "Connecté au broker MQTT %s:%s.",
                        self.config.mqtt_host,
                        self.config.mqtt_port,
                    )
                    await self._run_connected(mqtt)
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as error:
                LOGGER.warning(
                    "Connexion MQTT interrompue (%s); nouvel essai dans %.0f s.",
                    type(error).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX_SECONDS)


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), None)
    if not isinstance(level, int):
        raise ValueError(f"Niveau de log invalide : {level_name}")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s : %(message)s",
    )


async def async_main() -> None:
    config = GatewayConfig.from_env()
    configure_logging(config.log_level)
    await Gateway(config).run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Passerelle HomeKit vers MQTT pour Aqara FP2.")
    parser.parse_args()
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        LOGGER.info("Arrêt demandé.")


if __name__ == "__main__":
    main()