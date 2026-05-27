"""Modelos ORM (SQLAlchemy 2.0).

`EnergyReading` es donde aterrizan todos los datos de generación. Una sola tabla
cubre las tres granularidades (`hour`, `day`, `month`) gracias a la columna
`granularity` + el índice único compuesto descrito en el playbook.

Nota sobre `reading_hour`: en PostgreSQL los NULL se consideran *distintos* en
restricciones UNIQUE, así que para que el índice único funcione también en filas
day/month usamos el centinela **-1** (en vez de NULL) para todo lo que no sea
`hour`. Las filas `hour` usan 0..23.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Centinela para reading_hour cuando la granularidad NO es 'hour'.
NON_HOURLY = -1


class Plant(Base):
    """Catálogo de plantas descubiertas en el listado de Growatt."""

    __tablename__ = "plants"

    plant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    server_id: Mapped[str] = mapped_column(String(32), default="")
    account: Mapped[str] = mapped_column(String(128), default="")
    first_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EnergyReading(Base):
    """Una lectura de energía a una granularidad dada.

    - granularity='month' -> reading_date = YYYY-MM-01, energy_kwh = total del mes.
    - granularity='day'   -> reading_date = YYYY-MM-DD, energy_kwh = total del día.
    - granularity='hour'  -> 24 filas/día (reading_hour 0..23); raw guarda las
      12 muestras de 5-min (W) de esa hora en `samples_5min_w` y el `avg_w`.
    """

    __tablename__ = "energy_readings"
    __table_args__ = (
        UniqueConstraint(
            "plant_id",
            "device_sn",
            "reading_date",
            "granularity",
            "reading_hour",
            name="uq_energy_reading",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plant_id: Mapped[str] = mapped_column(String(32), index=True)
    device_sn: Mapped[str] = mapped_column(String(64), default="")
    reading_date: Mapped[date] = mapped_column(Date, index=True)
    granularity: Mapped[str] = mapped_column(String(8), index=True)  # hour|day|month
    reading_hour: Mapped[int] = mapped_column(Integer, default=NON_HOURLY)
    energy_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    peak_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SyncRun(Base):
    """Bitácora de cada corrida del sync (para auditoría/monitoreo)."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # ok|error|running
    plants_synced: Mapped[int] = mapped_column(Integer, default=0)
    readings_synced: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
