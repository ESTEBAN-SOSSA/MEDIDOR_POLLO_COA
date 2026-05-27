"""Configuración central leída desde el entorno (.env).

Todos los módulos (scraper, db, api) importan `settings` desde aquí para no
repetir lecturas de `os.getenv`. En local cargamos `.env` con python-dotenv;
en Docker las variables ya vienen inyectadas por compose.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

try:  # carga .env si está disponible (no obligatorio en Docker)
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv es opcional en runtime
    pass


def _csv(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    # ── Growatt ────────────────────────────────────────────────────────────
    growatt_user: str = os.getenv("GROWATT_USER", "")
    growatt_password: str = os.getenv("GROWATT_PASSWORD", "")
    login_url: str = os.getenv(
        "GROWATT_LOGIN_URL", "https://oss.growatt.com/login?lang=en"
    )
    oss_base: str = "https://oss.growatt.com"
    server_base: str = "https://server.growatt.com"

    # ── Scraper ────────────────────────────────────────────────────────────
    headless: bool = os.getenv("SCRAPER_HEADLESS", "true").lower() == "true"
    target_plant_ids: list[str] = field(
        default_factory=lambda: _csv(os.getenv("TARGET_PLANT_IDS", "1878757"))
    )
    hourly_days_back: int = int(os.getenv("HOURLY_DAYS_BACK", "7"))
    # Retención de la curva Hour (5-min): el sync borra Hour más viejo que N días.
    # Day/Month NO se podan (histórico indefinido). 0 ⇒ retención desactivada.
    hour_retention_days: int = int(os.getenv("HOUR_RETENTION_DAYS", "180"))
    snapshot_dir: str = os.getenv("SNAPSHOT_DIR", "snapshots")
    nav_timeout_ms: int = int(os.getenv("NAV_TIMEOUT_MS", "60000"))

    # ── Base de datos ──────────────────────────────────────────────────────
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql+psycopg://growatt:growatt@localhost:5432/growatt"
    )

    # ── API ────────────────────────────────────────────────────────────────
    api_key: str = os.getenv("API_KEY", "EDEMCO_2026_GROWAT_GENERACION")
    tz: str = os.getenv("TZ", "America/Bogota")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
