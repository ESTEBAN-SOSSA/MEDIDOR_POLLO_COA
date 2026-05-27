"""API de lectura de la generación extraída de Growatt (POLLO COA).

Todos los endpoints /api/v1/** exigen el header `X-API-Key`.

Endpoints:
  GET /health
  GET /api/v1/plants
  GET /api/v1/plants/{id}/generation?granularity=day|month&date_from&date_to
  GET /api/v1/plants/{id}/monthly/{year}/{month}
  GET /api/v1/plants/{id}/day/{YYYY-MM-DD}?full=true
"""
from __future__ import annotations

from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.deps import require_api_key
from src.db.models import EnergyReading, Plant
from src.db.session import get_db

app = FastAPI(
    title="Growatt Generación — POLLO COA",
    version="1.0.0",
    description="Lectura de Day/Month/Hour extraídos del portal Growatt OSS.",
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/v1/plants", dependencies=[Depends(require_api_key)], tags=["plants"])
def list_plants(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(Plant)).scalars().all()
    return [
        {
            "plant_id": p.plant_id,
            "name": p.name,
            "server_id": p.server_id,
            "account": p.account,
        }
        for p in rows
    ]


@app.get(
    "/api/v1/plants/{plant_id}/generation",
    dependencies=[Depends(require_api_key)],
    tags=["generation"],
)
def generation(
    plant_id: str,
    granularity: str = Query("day", pattern="^(day|month)$"),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """Serie Day o Month de una planta, opcionalmente acotada por rango."""
    stmt = select(EnergyReading).where(
        EnergyReading.plant_id == plant_id,
        EnergyReading.granularity == granularity,
    )
    if date_from:
        stmt = stmt.where(EnergyReading.reading_date >= date_from)
    if date_to:
        stmt = stmt.where(EnergyReading.reading_date <= date_to)
    stmt = stmt.order_by(EnergyReading.reading_date)

    rows = db.execute(stmt).scalars().all()
    return {
        "plant_id": plant_id,
        "granularity": granularity,
        "count": len(rows),
        "series": [
            {"date": r.reading_date.isoformat(), "energy_kwh": round(r.energy_kwh, 3)}
            for r in rows
        ],
    }


@app.get(
    "/api/v1/plants/{plant_id}/monthly/{year}/{month}",
    dependencies=[Depends(require_api_key)],
    tags=["generation"],
)
def monthly(
    plant_id: str, year: int, month: int, db: Session = Depends(get_db)
) -> dict:
    """Total del mes + desglose diario."""
    if not 1 <= month <= 12:
        raise HTTPException(status_code=422, detail="month debe estar entre 1 y 12")

    month_start = date(year, month, 1)
    month_total = db.execute(
        select(EnergyReading).where(
            EnergyReading.plant_id == plant_id,
            EnergyReading.granularity == "month",
            EnergyReading.reading_date == month_start,
        )
    ).scalar_one_or_none()

    days = (
        db.execute(
            select(EnergyReading)
            .where(
                EnergyReading.plant_id == plant_id,
                EnergyReading.granularity == "day",
                EnergyReading.reading_date >= month_start,
                EnergyReading.reading_date
                < date(year + (month // 12), (month % 12) + 1, 1),
            )
            .order_by(EnergyReading.reading_date)
        )
        .scalars()
        .all()
    )

    return {
        "plant_id": plant_id,
        "year": year,
        "month": month,
        "month_total_kwh": round(month_total.energy_kwh, 3) if month_total else 0.0,
        "days": [
            {"date": d.reading_date.isoformat(), "energy_kwh": round(d.energy_kwh, 3)}
            for d in days
        ],
    }


def _idx_to_hhmm(idx: int) -> str:
    minutes = idx * 5
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@app.get(
    "/api/v1/plants/{plant_id}/day/{day}",
    dependencies=[Depends(require_api_key)],
    tags=["generation"],
)
def day_curve(
    plant_id: str,
    day: date,
    full: bool = Query(False, description="true ⇒ 288 puntos 00:00→23:55"),
    db: Session = Depends(get_db),
) -> dict:
    """Reconstruye la curva Hour (5-min) del día a partir de las 24 filas hour."""
    rows = (
        db.execute(
            select(EnergyReading)
            .where(
                EnergyReading.plant_id == plant_id,
                EnergyReading.granularity == "hour",
                EnergyReading.reading_date == day,
            )
            .order_by(EnergyReading.reading_hour)
        )
        .scalars()
        .all()
    )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"Sin datos Hour para {plant_id} en {day.isoformat()}.",
        )

    # Reconstruye los 288 valores W (12 por hora, ordenados por hora).
    by_hour = {r.reading_hour: r for r in rows}
    power: list[float] = []
    for h in range(24):
        r = by_hour.get(h)
        samples = (r.raw or {}).get("samples_5min_w") if r else None
        samples = [float(x) for x in (samples or [])]
        samples = (samples + [0.0] * 12)[:12]
        power.extend(samples)

    total_kwh = sum(power) * 5 / 60 / 1000
    peak_idx = max(range(len(power)), key=lambda i: power[i]) if power else 0
    peak_power_w = power[peak_idx] if power else 0.0
    gen_idx = [i for i, w in enumerate(power) if w > 0]
    first_idx = gen_idx[0] if gen_idx else None
    last_idx = gen_idx[-1] if gen_idx else None

    if full or first_idx is None:
        lo, hi = 0, len(power) - 1
    else:
        lo, hi = first_idx, last_idx

    data = [
        {"time": _idx_to_hhmm(i), "power_w": round(power[i], 1)}
        for i in range(lo, hi + 1)
    ]

    return {
        "plant_id": plant_id,
        "date": day.isoformat(),
        "total_kwh": round(total_kwh, 3),
        "peak_power_w": round(peak_power_w, 1),
        "peak_time": _idx_to_hhmm(peak_idx) if power else None,
        "first_generation_at": _idx_to_hhmm(first_idx) if first_idx is not None else None,
        "last_generation_at": _idx_to_hhmm(last_idx) if last_idx is not None else None,
        "points": len(data),
        "data": data,
    }
