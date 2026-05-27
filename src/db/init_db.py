"""Crea las tablas si no existen. Idempotente.

Uso:
    python -m src.db.init_db
"""
from __future__ import annotations

import logging

from src.db.models import Base
from src.db.session import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("init_db")


def init_db() -> None:
    log.info("Creando tablas (create_all)…")
    Base.metadata.create_all(bind=engine)
    log.info("Listo. Tablas: %s", ", ".join(Base.metadata.tables))


if __name__ == "__main__":
    init_db()
