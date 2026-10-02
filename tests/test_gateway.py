from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohomekit import Controller
from aiohomekit.zeroconf import ZeroconfServiceListener
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

from gateway import Gateway, GatewayConfig, build_mapping, publish_entity


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
HAP_SERVICES = ["_hap._tcp.local.", "_hap._udp.local."]


class FakeMqtt:
    def __init__(self) -> None:
        self.publications: list[tuple[str, str, int, bool]] = []

    async def publish(
        self,
        topic: str,
        payload: str,
        *,
        qos: int,
        retain: bool,
    ) -> None:
        self.publications.append((topic, payload, qos, retain))


class GatewayTests(unittest.TestCase):
    def test_builds_mapping_from_exposed_homekit_services(self) -> None:
        accessories = json.loads((FIXTURES / "accessories.json").read_text(encoding="utf-8"))

        mapping = build_mapping(accessories, "fp2")

        self.assertEqual(mapping.entities[(1, 12)].topic, "fp2/presence")
        self.assertEqual(mapping.entities[(1, 22)].topic, "fp2/zone/bureau")
        self.assertEqual(mapping.entities[(1, 32)].topic, "fp2/illuminance")
        self.assertEqual(mapping.event_keys, {(1, 12), (1, 22), (1, 32)})
        self.assertFalse(mapping.unresolved)

    def test_does_not_guess_global_sensor_when_multiple_are_ambiguous(self) -> None:
        accessories = [
            {
                "aid": 2,
                "services": [
                    {
                        "iid": 10,
                        "characteristics": [
                            {"iid": 11, "type": "00000023-0000-1000-8000-0026BB765291", "value": "Cellier"},
                            {"iid": 12, "type": "00000071-0000-1000-8000-0026BB765291", "perms": ["ev"]},
                        ],
                    },
                    {
                        "iid": 20,
                        "characteristics": [
                            {"iid": 21, "type": "00000023-0000-1000-8000-0026BB765291", "value": "Couloir"},
                            {"iid": 22, "type": "00000071-0000-1000-8000-0026BB765291", "perms": ["ev"]},
                        ],
                    },
                ],
            }
        ]

        mapping = build_mapping(accessories, "fp2")

        self.assertEqual(mapping.entities[(2, 12)].topic, "fp2/zone/cellier")
        self.assertEqual(mapping.entities[(2, 22)].topic, "fp2/zone/couloir")
        self.assertNotIn("fp2/presence", {entity.topic for entity in mapping.entities.values()})
        self.assertEqual(len(mapping.warnings), 1)
        self.assertIn("ambigu", mapping.warnings[0])

    def test_entity_publish_is_json_qos_one_and_retained(self) -> None:
        mapping = build_mapping(
            json.loads((FIXTURES / "accessories.json").read_text(encoding="utf-8")),
            "fp2",
        )
        mqtt = FakeMqtt()

        asyncio.run(publish_entity(mqtt, mapping.entities[(1, 22)], True))

        topic, payload, qos, retain = mqtt.publications[0]
        decoded = json.loads(payload)
        self.assertEqual(topic, "fp2/zone/bureau")
        self.assertIs(decoded["value"], True)
        self.assertTrue(decoded["timestamp"].endswith("Z"))
        self.assertEqual(qos, 1)
        self.assertTrue(retain)

    def test_diagnostic_mqtt_publish_is_qos_one_and_retained(self) -> None:
        config = GatewayConfig(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_username=None,
            mqtt_password=None,
            topic_prefix="fp2",
            pairing_file=ROOT / "pairing.json",
            pairing_alias="fp2-test",
            log_level="INFO",
            diagnostic_interval=30,
            fallback_interval=60,
        )
        gateway = Gateway(config)
        mqtt = FakeMqtt()
        gateway._mqtt = mqtt

        asyncio.run(gateway._diagnostic("mqtt", "connected", qos=1, sequence=1))

        topic, payload, qos, retain = mqtt.publications[0]
        self.assertEqual(topic, "fp2/diagnostic/mqtt")
        self.assertEqual(json.loads(payload)["state"], "connected")
        self.assertEqual(qos, 1)
        self.assertTrue(retain)

    def test_availability_status_is_plain_online_or_offline(self) -> None:
        config = GatewayConfig(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_username=None,
            mqtt_password=None,
            topic_prefix="fp2",
            pairing_file=ROOT / "pairing.json",
            pairing_alias="fp2-test",
            log_level="INFO",
            diagnostic_interval=30,
            fallback_interval=60,
        )
        gateway = Gateway(config)
        mqtt = FakeMqtt()

        asyncio.run(gateway._publish_status(mqtt, "online"))
        asyncio.run(gateway._publish_status(mqtt, "offline"))

        self.assertEqual(
            [(topic, payload, qos, retain) for topic, payload, qos, retain in mqtt.publications],
            [
                ("fp2/status", "online", 1, True),
                ("fp2/status", "offline", 1, True),
            ],
        )

    def test_fake_pairing_file_loads_without_connecting_to_accessory(self) -> None:
        async def load_fixture() -> tuple[str, str]:
            async with AsyncZeroconf() as zeroconf:
                browser = AsyncServiceBrowser(
                    zeroconf.zeroconf,
                    HAP_SERVICES,
                    listener=ZeroconfServiceListener(),
                )
                try:
                    async with Controller(async_zeroconf_instance=zeroconf) as controller:
                        controller.load_data(str(FIXTURES / "pairing.json"))
                        pairing = controller.aliases["fp2-test"]
                        return pairing.id, pairing.pairing_data["AccessoryIP"]
                finally:
                    await browser.async_cancel()

        pairing_id, host = asyncio.run(load_fixture())

        self.assertEqual(pairing_id, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(host, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()