"""API de lectura de la generación extraída de Growatt (POLLO COA).

Todos los endpoints /api/v1/** exigen el header `X-API-Key`.

Endpoints:
  GET /health
  GET /api/v1/plants
  GET /api/v1/plants/{id}/generation?granularity=interval|hour|day|month&date_from&date_to
  GET /api/v1/plants/{id}/monthly/{year}/{month}
  GET /api/v1/plants/{id}/day/{YYYY-MM-DD}?full=true

Series por intervalo (regla de DePow, 1 oct 2026 — issue jhoyosp/DePow#123):
toda lectura de energía llega con fecha, hora, minuto y ZONA explícita, por
intervalo. `granularity=hour` (60 min) e `interval` (15 min) cumplen eso:

  {"date": "2026-09-30T14:00:00-05:00",       ← INICIO del intervalo
   "period_end": "2026-09-30T15:00:00-05:00",
   "energy_kwh": 312.4, "energy_type": "interval", "interval_minutes": 60, ...}

⚠️ De dónde sale la energía: Growatt NO entrega energía por hora. Entrega el
total del día y la POTENCIA cada 5 minutos; el scraper integra esa potencia
(Σ W × 5/60 / 1000) para cada hora. Integrada, difiere del total diario del
portal entre −0,4 % y +19 % según el día. `ajustar_al_total_diario=true`
escala las horas de cada día para que sumen exactamente el total diario
(decisión pendiente de DePow; por defecto NO se ajusta).

Sólo se entregan días CERRADOS: las filas Hour de un día capturadas después de
que el día terminó. El scraper corre a las 03:00, así que las horas de hoy (y
las que quedaron en cero en esa corrida) no salen hasta que el día siguiente
las refresca: un consumidor que guarda cada lectura una vez no se queda con un
cero provisional.
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.deps import require_api_key
from src.db.models import EnergyReading, Plant
from src.db.session import get_db

# Zona de la planta. Colombia no tiene horario de verano: el desfase es fijo.
PLANT_TZ = timezone(timedelta(hours=float(os.getenv("PLANT_UTC_OFFSET_HOURS", "-5"))))

# Minutos por registro en las series finas.
FINE_GRANULARITIES = {"interval": 15, "hour": 60}
SAMPLES_PER_HOUR = 12  # muestras de 5 min
DEFAULT_FINE_DAYS = 31  # sin date_from, el último mes

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
    granularity: str = Query("day", pattern="^(interval|hour|day|month)$"),
    date_from: date | None = None,
    date_to: date | None = None,
    ajustar_al_total_diario: bool = Query(
        False,
        description=(
            "Sólo interval/hour: escala las horas de cada día para que sumen "
            "el total diario del portal"
        ),
    ),
    db: Session = Depends(get_db),
) -> dict:
    """Serie de una planta: interval (15 min) / hour con fecha, hora y zona; day o month."""
    if granularity in FINE_GRANULARITIES:
        return _fine_series(
            db, plant_id, granularity, date_from, date_to, ajustar_al_total_diario
        )

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


def _aware(value: datetime | None) -> datetime | None:
    """`created_at` con zona (SQLite lo devuelve sin ella: es UTC)."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _fine_series(
    db: Session,
    plant_id: str,
    granularity: str,
    date_from: date | None,
    date_to: date | None,
    ajustar_al_total_diario: bool,
    now: datetime | None = None,
) -> dict:
    """Serie por intervalo (15 min o 1 h) a partir de las filas Hour.

    Cada registro trae el INICIO del intervalo con zona en `date`, su fin en
    `period_end` y `energy_type="interval"`. Sólo días cerrados (ver docstring
    del módulo). Una hora sin fila o, para 15 min, sin sus 12 muestras, no se
    inventa: simplemente no sale.
    """
    now = now or datetime.now(PLANT_TZ)
    today = now.astimezone(PLANT_TZ).date()
    if date_to is None or date_to >= today:
        date_to = today - timedelta(days=1)
    if date_from is None:
        date_from = date_to - timedelta(days=DEFAULT_FINE_DAYS - 1)

    minutes = FINE_GRANULARITIES[granularity]
    empty = {
        "plant_id": plant_id,
        "granularity": granularity,
        "energy_basis": "integrated_5min_power",
        "adjusted_to_daily_total": ajustar_al_total_diario,
        "count": 0,
        "series": [],
    }
    if date_to < date_from:
        return empty

    rows = (
        db.execute(
            select(EnergyReading)
            .where(
                EnergyReading.plant_id == plant_id,
                EnergyReading.granularity == "hour",
                EnergyReading.reading_date >= date_from,
                EnergyReading.reading_date <= date_to,
            )
            .order_by(EnergyReading.reading_date, EnergyReading.reading_hour)
        )
        .scalars()
        .all()
    )
    by_day: dict[date, list[EnergyReading]] = defaultdict(list)
    for r in rows:
        by_day[r.reading_date].append(r)

    daily_totals: dict[date, float] = {}
    for d in db.execute(
        select(EnergyReading).where(
            EnergyReading.plant_id == plant_id,
            EnergyReading.granularity == "day",
            EnergyReading.reading_date >= date_from,
            EnergyReading.reading_date <= date_to,
        )
    ).scalars():
        daily_totals[d.reading_date] = d.energy_kwh

    series: list[dict] = []
    for day in sorted(by_day):
        day_end = datetime.combine(day + timedelta(days=1), time(), PLANT_TZ)
        hours = by_day[day]
        # Día cerrado: TODAS sus filas Hour se capturaron después de que terminó.
        if any((_aware(h.created_at) or day_end) < day_end for h in hours):
            continue
        # Día SIN detalle: el portal sólo guarda la curva de 5 min unos ~3 meses;
        # pedida después devuelve 288 ceros. Si el día generó energía (total
        # diario > 0) y sus horas suman 0, esas horas no son un dato: no salen.
        if sum(h.energy_kwh for h in hours) <= 0 and (daily_totals.get(day) or 0) > 0:
            continue

        # (inicio, kWh, pico W) de cada intervalo del día.
        segments: list[tuple[datetime, float, float | None]] = []
        for h in hours:
            start = datetime.combine(day, time(hour=h.reading_hour), PLANT_TZ)
            if granularity == "hour":
                segments.append((start, h.energy_kwh, h.peak_power_w))
                continue
            samples = [float(x) for x in ((h.raw or {}).get("samples_5min_w") or [])]
            if len(samples) != SAMPLES_PER_HOUR:
                continue
            for q in range(4):
                chunk = samples[q * 3 : q * 3 + 3]
                segments.append(
                    (start + timedelta(minutes=15 * q), sum(chunk) * 5 / 60 / 1000, max(chunk))
                )

        factor = 1.0
        if ajustar_al_total_diario:
            integrated = sum(kwh for _, kwh, _ in segments)
            total = daily_totals.get(day)
            if total is None or integrated <= 0:
                continue  # sin total diario no hay a qué ajustar: no se inventa
            factor = total / integrated

        for start, kwh, peak in segments:
            series.append(
                {
                    "date": start.isoformat(timespec="seconds"),
                    "period_end": (start + timedelta(minutes=minutes)).isoformat(
                        timespec="seconds"
                    ),
                    "energy_kwh": round(kwh * factor, 4),
                    "energy_type": "interval",
                    "interval_minutes": minutes,
                    "peak_power_w": round(peak, 1) if peak is not None else None,
                }
            )

    return {**empty, "count": len(series), "series": series}


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
