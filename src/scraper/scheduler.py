"""Scheduler del contenedor scraper: corre el sync diario a las 03:00 (TZ).

Equivale al cron de las 03:00 mencionado en el playbook, pero implementado en
Python para heredar el entorno de forma fiable dentro de Docker.

Variables:
  SYNC_HOUR     hora local del sync diario (default 3)
  RUN_ON_START  "true" para correr un sync al arrancar el contenedor (default false)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.config import settings
from src.db.init_db import init_db
from src.scraper.main import run_plant_energy_sync

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("scraper.scheduler")

SYNC_HOUR = int(os.getenv("SYNC_HOUR", "3"))


def _next_run(tz: ZoneInfo) -> datetime:
    now = datetime.now(tz)
    nxt = now.replace(hour=SYNC_HOUR, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return nxt


async def _safe_sync() -> None:
    try:
        await run_plant_energy_sync()
    except Exception:  # noqa: BLE001
        log.exception("Sync falló (se reintenta en la próxima corrida)")


async def main() -> None:
    init_db()
    tz = ZoneInfo(settings.tz)
    log.info("Scheduler arriba. TZ=%s, sync diario a las %02d:00.", settings.tz, SYNC_HOUR)

    if os.getenv("RUN_ON_START", "false").lower() == "true":
        log.info("RUN_ON_START=true → sync inicial…")
        await _safe_sync()

    while True:
        nxt = _next_run(tz)
        secs = (nxt - datetime.now(tz)).total_seconds()
        log.info("Próximo sync: %s (en %.1f h)", nxt.isoformat(), secs / 3600)
        await asyncio.sleep(max(secs, 1))
        await _safe_sync()


if __name__ == "__main__":
    asyncio.run(main())
