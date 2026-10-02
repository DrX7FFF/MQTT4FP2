"""Appaire un accessoire HomeKit et sauvegarde les données de pairing."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import re

from aiohomekit import Controller
from aiohomekit.zeroconf import ZeroconfServiceListener
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf


LOGGER = logging.getLogger("fp2.pair")
HAP_SERVICES = ["_hap._tcp.local.", "_hap._udp.local."]
PAIRING_CODE = re.compile(r"^\d{3}-\d{2}-\d{3}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Appairer un accessoire HomeKit.")
    parser.add_argument(
        "--pairing-file",
        default=os.getenv("PAIRING_FILE", "data/pairing.json"),
        help="Chemin du fichier de pairing (défaut : %(default)s)",
    )
    parser.add_argument(
        "--alias",
        default=os.getenv("PAIRING_ALIAS", "fp2"),
        help="Nom local du pairing (défaut : %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10,
        help="Durée de découverte mDNS en secondes (défaut : %(default)s)",
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


def ask_pairing_code() -> str:
    while True:
        code = getpass.getpass("Code HomeKit (XXX-XX-XXX) : ")
        if PAIRING_CODE.fullmatch(code):
            return code
        print("Format invalide. Le code n'a pas été affiché.")


def select_accessory(discoveries: list) -> object:
    for index, discovery in enumerate(discoveries, start=1):
        description = discovery.description
        etat = "déjà appairé" if discovery.paired else "non appairé"
        print(
            f"{index}. {description.name} | {description.id} | "
            f"modèle : {description.model or 'inconnu'} | {etat}"
        )

    while True:
        choice = input("Numéro de l'accessoire à appairer : ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(discoveries):
            return discoveries[int(choice) - 1]
        print("Choix invalide.")


async def pair_accessory(args: argparse.Namespace) -> None:
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

                if args.alias in controller.aliases:
                    LOGGER.error(
                        "L'alias '%s' existe déjà dans le fichier de pairing.",
                        args.alias,
                    )
                    return

                print(f"Recherche des accessoires HomeKit pendant {args.timeout:g} s…")
                await asyncio.sleep(args.timeout)
                discoveries = [
                    discovery async for discovery in controller.async_discover()
                ]

                if not discoveries:
                    LOGGER.error("Aucun accessoire HomeKit découvert sur le réseau.")
                    return

                discovery = select_accessory(discoveries)
                if discovery.paired:
                    LOGGER.error(
                        "Cet accessoire est déjà appairé. Aucun nouvel appairage effectué."
                    )
                    return

                code = ask_pairing_code()
                try:
                    finish_pairing = await discovery.async_start_pairing(args.alias)
                    await finish_pairing(code)
                    controller.save_data(args.pairing_file)
                except Exception as error:
                    LOGGER.error(
                        "Échec de l'appairage (%s). Le code HomeKit n'est pas journalisé.",
                        type(error).__name__,
                    )
                    return
                finally:
                    code = ""

                LOGGER.info(
                    "Appairage enregistré dans %s (alias : %s).",
                    args.pairing_file,
                    args.alias,
                )
        finally:
            await browser.async_cancel()


def main() -> None:
    configure_logging()
    args = parse_args()
    try:
        asyncio.run(pair_accessory(args))
    except KeyboardInterrupt:
        LOGGER.info("Opération interrompue.")


if __name__ == "__main__":
    main()