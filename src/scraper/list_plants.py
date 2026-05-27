"""CLI: lista todas las plantas visibles en la cuenta Growatt (id, nombre, cuenta).

Sirve para descubrir el `plant_id` de una planta NUEVA antes de agregarla a
`TARGET_PLANT_IDS`. No escribe nada en la BD.

Uso:
    docker compose exec scraper python -m src.scraper.list_plants
"""
from __future__ import annotations

import asyncio
import logging

from src.scraper.client import growatt_session
from src.scraper.plant_energy import get_plant_listing

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)


async def run() -> None:
    async with growatt_session() as page:
        plants = await get_plant_listing(page)

    print(f"\n{'PLANT_ID':<12} {'CUENTA':<22} {'SERVER':<8} NOMBRE")
    print("-" * 72)
    for p in sorted(plants, key=lambda x: x["name_hint"]):
        print(f"{p['plant_id']:<12} {p['account'][:20]:<22} {p['server_id']:<8} {p['name_hint']}")
    print(f"\nTotal: {len(plants)} plantas\n")


if __name__ == "__main__":
    asyncio.run(run())
