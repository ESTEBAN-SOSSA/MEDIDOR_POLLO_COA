"""CLI on-demand para extraer la curva Hour (5-min) de una planta y un rango.

Idempotente: borra el Hour previo de cada día antes de insertar.

Uso:
    python -m src.scraper.hourly --plant_id 1878757 --from 2024-08-01 --to 2024-08-07
    python -m src.scraper.hourly --from 2024-08-01 --to 2024-08-07   # todas las objetivo
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime, timedelta

from src.config import settings
from src.db.models import Base
from src.db.session import SessionLocal, engine
from src.scraper.client import growatt_session
from src.scraper.plant_energy import (
    fetch_plant_hourly_for_date,
    get_plant_listing,
    open_server_session,
    persist_plant_hourly,
    upsert_plant,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("scraper.hourly")


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _daterange(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


async def run_hourly(plant_id: str | None, date_from: date, date_to: date) -> int:
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    total = 0
    try:
        async with growatt_session() as page:
            listing = await get_plant_listing(page)
            if plant_id:
                targets = [p for p in listing if p["plant_id"] == plant_id]
            else:
                wanted = set(settings.target_plant_ids)
                targets = [p for p in listing if p["plant_id"] in wanted]
            if not targets:
                raise RuntimeError(
                    f"No se encontró la planta {plant_id or settings.target_plant_ids} "
                    "en el listado."
                )

            by_account: dict[str, list[dict]] = {}
            for p in targets:
                by_account.setdefault(p["account"], []).append(p)

            for account, plants in by_account.items():
                head = plants[0]
                sp = await open_server_session(
                    page, head["server_id"], account, head["plant_id"]
                )
                try:
                    for plant in plants:
                        pid = plant["plant_id"]
                        upsert_plant(session, pid, plant["name_hint"])
                        for day in _daterange(date_from, date_to):
                            hourly = await fetch_plant_hourly_for_date(sp, pid, day)
                            n = persist_plant_hourly(session, pid, day, hourly)
                            total += n
                            log.info("Hour %s %s → %d filas", pid, day, n)
                        session.commit()
                finally:
                    try:
                        await sp.close()
                    except Exception:
                        pass
    finally:
        session.close()
    log.info("Hour terminado: %d filas", total)
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Extracción Hour on-demand (Growatt)")
    parser.add_argument("--plant_id", default=None, help="ID de planta (omitir = objetivo)")
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()
    asyncio.run(
        run_hourly(args.plant_id, _parse_date(args.date_from), _parse_date(args.date_to))
    )


if __name__ == "__main__":
    main()
