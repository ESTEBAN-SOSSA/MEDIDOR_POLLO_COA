"""Entrypoint del sync completo (acotado a las plantas de TARGET_PLANT_IDS).

Por defecto solo procesa **POLLO COA** (1878757):
  - Day + Month: histórico completo (full refresh, se omiten kWh ≤ 0).
  - Hour: últimos `hourly_days_back` días (default 7) para mantenerlos "calientes".

Uso:
    python -m src.scraper.main                 # usa HOURLY_DAYS_BACK del entorno
    python -m src.scraper.main --hourly-days 14
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime, timedelta

from src.config import settings
from src.db.models import Base, SyncRun
from src.db.session import SessionLocal, engine
from src.scraper.client import growatt_session
from src.scraper.plant_energy import (
    fetch_plant_history,
    fetch_plant_hourly_for_date,
    get_plant_listing,
    open_server_session,
    persist_plant,
    persist_plant_hourly,
    upsert_plant,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("scraper.main")


def _select_target_plants(listing: list[dict]) -> list[dict]:
    """Filtra el listado a las plantas pedidas (por id; fallback por nombre)."""
    wanted_ids = set(settings.target_plant_ids)
    selected = [p for p in listing if p["plant_id"] in wanted_ids]
    if not selected:
        selected = [p for p in listing if "POLLO COA" in p["name_hint"].upper()]
        if selected:
            log.warning(
                "Sin match por ID %s; usando match por nombre POLLO COA: %s",
                wanted_ids,
                [p["plant_id"] for p in selected],
            )
    return selected


async def run_plant_energy_sync(hourly_days_back: int | None = None) -> dict:
    """Corre el sync y devuelve un resumen. Registra un SyncRun."""
    Base.metadata.create_all(bind=engine)
    hourly_days_back = (
        settings.hourly_days_back if hourly_days_back is None else hourly_days_back
    )
    today = date.today()
    current_year = today.year

    session = SessionLocal()
    run = SyncRun(status="running")
    session.add(run)
    session.commit()

    plants_synced = 0
    readings_synced = 0
    try:
        async with growatt_session() as page:
            listing = await get_plant_listing(page)
            targets = _select_target_plants(listing)
            if not targets:
                raise RuntimeError(
                    f"No se encontró ninguna planta objetivo ({settings.target_plant_ids}) "
                    "en el listado de Growatt."
                )
            log.info("Plantas a procesar: %s", [(p["plant_id"], p["name_hint"]) for p in targets])

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
                        log.info("── Procesando %s (%s) ──", pid, plant["name_hint"])
                        upsert_plant(session, pid, plant["name_hint"])

                        history = await fetch_plant_history(sp, pid, current_year)
                        if not history["years"]:
                            log.warning("Planta %s sin datos.", pid)
                            continue
                        d, m = persist_plant(session, pid, history)
                        readings_synced += d + m

                        for back in range(hourly_days_back):
                            day = today - timedelta(days=back)
                            hourly = await fetch_plant_hourly_for_date(sp, pid, day)
                            readings_synced += persist_plant_hourly(session, pid, day, hourly)

                        session.commit()
                        plants_synced += 1
                        log.info("Planta %s OK: %d días, %d meses", pid, d, m)
                finally:
                    try:
                        await sp.close()
                    except Exception:
                        pass

        run.status = "success"
    except Exception as exc:  # noqa: BLE001
        log.exception("Sync falló")
        run.status = "failed"
        run.error = str(exc)[:1990]
        session.commit()
        raise
    finally:
        run.plants_synced = plants_synced
        run.readings_synced = readings_synced
        run.finished_at = datetime.now()
        session.commit()
        summary = {
            "status": run.status,
            "plants_synced": plants_synced,
            "readings_synced": readings_synced,
        }
        session.close()

    log.info("Sync terminado: %s", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync de generación Growatt (POLLO COA)")
    parser.add_argument(
        "--hourly-days",
        type=int,
        default=None,
        help="Días hacia atrás de curva Hour (default: HOURLY_DAYS_BACK).",
    )
    args = parser.parse_args()
    asyncio.run(run_plant_energy_sync(hourly_days_back=args.hourly_days))


if __name__ == "__main__":
    main()
