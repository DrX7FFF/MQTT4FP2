"""Affiche les accessoires, services et caractéristiques HomeKit appairés."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any

from aiohomekit import Controller
from aiohomekit.zeroconf import ZeroconfServiceListener
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf


LOGGER = logging.getLogger("fp2.dump")
HAP_SERVICES = ["_hap._tcp.local.", "_hap._udp.local."]
NAME_CHARACTERISTIC = "00000023-0000-1000-8000-0026BB765291"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Afficher les données exposées par un accessoire HomeKit."
    )
    parser.add_argument(
        "--pairing-file",
        default=os.getenv("PAIRING_FILE", "data/pairing.json"),
        help="Chemin du fichier de pairing (défaut : %(default)s)",
    )
    parser.add_argument(
        "--alias",
        default=os.getenv("PAIRING_ALIAS", "fp2"),
        help="Alias du pairing (défaut : %(default)s)",
    )
    return parser.parse_args()


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        raise ValueError(f"Niveau de log invalide : {level_name}")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s : %(message)s",
    )


def service_name(service: dict[str, Any]) -> str:
    for characteristic in service.get("characteristics", []):
        if str(characteristic.get("type", "")).upper() == NAME_CHARACTERISTIC:
            value = characteristic.get("value")
            if value is not None:
                return str(value)
    return str(service.get("name", ""))


def display_accessories(accessories: list[dict[str, Any]]) -> None:
    for accessory in accessories:
        aid = accessory.get("aid", "?")
        services = accessory.get("services", [])
        noms_services = [name for service in services if (name := service_name(service))]
        nom_accessoire = ", ".join(dict.fromkeys(noms_services))
        print(f"\nAccessoire | aid={aid} | nom={nom_accessoire or '-'}")

        for service in services:
            service_iid = service.get("iid", "?")
            service_type = service.get("type", "?")
            nom_service = service_name(service)
            print(
                f"  Service | aid={aid} | iid={service_iid} | "
                f"type={service_type} | nom={nom_service or '-'}"
            )

            for characteristic in service.get("characteristics", []):
                name = characteristic.get("name") or characteristic.get("description")
                if not name and str(characteristic.get("type", "")).upper() == NAME_CHARACTERISTIC:
                    name = nom_service
                valeur = characteristic.get("value", "<absente>")
                print(
                    f"    Caractéristique | aid={aid} | iid={characteristic.get('iid', '?')} | "
                    f"type={characteristic.get('type', '?')} | nom={name or '-'} | "
                    f"valeur={json.dumps(valeur, ensure_ascii=False, default=str)}"
                )


async def dump_accessories(args: argparse.Namespace) -> None:
    async with AsyncZeroconf() as zeroconf:
        listener = ZeroconfServiceListener()
        browser = AsyncServiceBrowser(
            zeroconf.zeroconf,
            HAP_SERVICES,
            listener=listener,
        )
        try:
            async with Controller(async_zeroconf_instance=zeroconf) as controller:
                try:
                    controller.load_data(args.pairing_file)
                except Exception as error:
                    LOGGER.error(
                        "Impossible de lire le fichier de pairing (%s).",
                        type(error).__name__,
                    )
                    return

                pairing = controller.aliases.get(args.alias)
                if pairing is None:
                    LOGGER.error(
                        "Alias '%s' absent du fichier %s.",
                        args.alias,
                        args.pairing_file,
                    )
                    return

                try:
                    accessories = await pairing.list_accessories_and_characteristics()
                except Exception as error:
                    LOGGER.error(
                        "Impossible de lire les caractéristiques (%s).",
                        type(error).__name__,
                    )
                    return

                display_accessories(accessories)
        finally:
            await browser.async_cancel()


def main() -> None:
    configure_logging()
    args = parse_args()
    try:
        asyncio.run(dump_accessories(args))
    except KeyboardInterrupt:
        LOGGER.info("Lecture interrompue.")


if __name__ == "__main__":
    main()