"""Series por intervalo con fecha, hora, minuto y zona (issue jhoyosp/DePow#123).

Corre sin PostgreSQL: SQLite en memoria y la dependencia `get_db` reemplazada.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["API_KEY"] = "clave-de-prueba"

from datetime import date, datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.api import main as api  # noqa: E402
from src.db.models import NON_HOURLY, Base, EnergyReading, Plant  # noqa: E402
from src.db.session import get_db  # noqa: E402

COL = timezone(timedelta(hours=-5))
PLANTA = "1878757"
HEADERS = {"X-API-Key": "clave-de-prueba"}
RUTA = f"/api/v1/plants/{PLANTA}/generation"


def _muestras(hora: int) -> list[float]:
    """12 muestras de 5 min: potencia sólo de 6 a 17 h, con forma de campana."""
    if not 6 <= hora <= 17:
        return [0.0] * 12
    base = 1000.0 * (6 - abs(hora - 11.5))
    return [base + i * 10 for i in range(12)]


@pytest.fixture
def db_y_cliente():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Sesion = sessionmaker(bind=engine, expire_on_commit=False)

    def _db():
        s = Sesion()
        try:
            yield s
        finally:
            s.close()

    api.app.dependency_overrides[get_db] = _db
    with Sesion() as s:
        s.add(Plant(plant_id=PLANTA, name="POLLO COA"))
        s.commit()
    yield Sesion, TestClient(api.app)
    api.app.dependency_overrides.clear()


def _sembrar_dia(Sesion, dia: date, capturado: datetime, total_diario: float | None):
    with Sesion() as s:
        for h in range(24):
            m = _muestras(h)
            s.add(
                EnergyReading(
                    plant_id=PLANTA,
                    reading_date=dia,
                    granularity="hour",
                    reading_hour=h,
                    energy_kwh=sum(m) * 5 / 60 / 1000,
                    peak_power_w=max(m),
                    raw={"samples_5min_w": m},
                    created_at=capturado,
                )
            )
        if total_diario is not None:
            s.add(
                EnergyReading(
                    plant_id=PLANTA,
                    reading_date=dia,
                    granularity="day",
                    reading_hour=NON_HOURLY,
                    energy_kwh=total_diario,
                )
            )
        s.commit()


def _ayer():
    return datetime.now(COL).date() - timedelta(days=1)


@pytest.mark.parametrize("granularidad, minutos, por_dia", [("hour", 60, 24), ("interval", 15, 96)])
def test_cada_lectura_trae_hora_minuto_zona_y_tipo(db_y_cliente, granularidad, minutos, por_dia):
    Sesion, cliente = db_y_cliente
    ayer = _ayer()
    _sembrar_dia(Sesion, ayer, datetime.now(timezone.utc), 50.0)

    cuerpo = cliente.get(RUTA, params={"granularity": granularidad}, headers=HEADERS).json()
    serie = cuerpo["series"]
    assert cuerpo["granularity"] == granularidad
    assert len(serie) == por_dia

    instantes = set()
    for r in serie:
        inicio = datetime.fromisoformat(r["date"])
        fin = datetime.fromisoformat(r["period_end"])
        assert r["date"].endswith("-05:00")
        assert inicio.utcoffset() == timedelta(hours=-5)
        assert inicio.date() == ayer
        assert fin - inicio == timedelta(minutes=minutos)
        assert r["energy_type"] == "interval"
        assert r["interval_minutes"] == minutos
        instantes.add(inicio)
    # Lo que pide verificar el issue: horas distintas de 00:00 y más de una por día.
    assert len(instantes) == por_dia
    assert any(i.hour != 0 for i in instantes)


def test_los_15_minutos_suman_la_hora(db_y_cliente):
    Sesion, cliente = db_y_cliente
    _sembrar_dia(Sesion, _ayer(), datetime.now(timezone.utc), None)
    horas = cliente.get(RUTA, params={"granularity": "hour"}, headers=HEADERS).json()["series"]
    cuartos = cliente.get(RUTA, params={"granularity": "interval"}, headers=HEADERS).json()["series"]
    assert sum(r["energy_kwh"] for r in cuartos) == pytest.approx(
        sum(r["energy_kwh"] for r in horas), rel=1e-6
    )


def test_un_dia_capturado_antes_de_cerrar_no_sale(db_y_cliente):
    """La corrida de las 03:00 deja el resto del día en cero: no es un dato."""
    Sesion, cliente = db_y_cliente
    ayer = _ayer()
    a_medio_dia = datetime.combine(ayer, datetime.min.time(), COL) + timedelta(hours=3)
    _sembrar_dia(Sesion, ayer, a_medio_dia, 50.0)
    cuerpo = cliente.get(RUTA, params={"granularity": "hour"}, headers=HEADERS).json()
    assert cuerpo["count"] == 0


def test_hoy_nunca_sale_aunque_se_pida(db_y_cliente):
    Sesion, cliente = db_y_cliente
    hoy = datetime.now(COL).date()
    _sembrar_dia(Sesion, hoy, datetime.now(timezone.utc), 50.0)
    cuerpo = cliente.get(
        RUTA,
        params={"granularity": "hour", "date_from": hoy.isoformat(), "date_to": hoy.isoformat()},
        headers=HEADERS,
    ).json()
    assert cuerpo["count"] == 0


def test_ajustar_al_total_diario(db_y_cliente):
    Sesion, cliente = db_y_cliente
    _sembrar_dia(Sesion, _ayer(), datetime.now(timezone.utc), 123.0)
    for g in ("hour", "interval"):
        serie = cliente.get(
            RUTA,
            params={"granularity": g, "ajustar_al_total_diario": "true"},
            headers=HEADERS,
        ).json()["series"]
        assert sum(r["energy_kwh"] for r in serie) == pytest.approx(123.0, abs=0.01)
    sin_ajuste = cliente.get(RUTA, params={"granularity": "hour"}, headers=HEADERS).json()
    assert sum(r["energy_kwh"] for r in sin_ajuste["series"]) != pytest.approx(123.0, abs=0.01)


def test_ajustar_sin_total_diario_no_inventa(db_y_cliente):
    Sesion, cliente = db_y_cliente
    _sembrar_dia(Sesion, _ayer(), datetime.now(timezone.utc), None)
    cuerpo = cliente.get(
        RUTA, params={"granularity": "hour", "ajustar_al_total_diario": "true"}, headers=HEADERS
    ).json()
    assert cuerpo["count"] == 0


def test_la_serie_diaria_no_cambia(db_y_cliente):
    Sesion, cliente = db_y_cliente
    ayer = _ayer()
    _sembrar_dia(Sesion, ayer, datetime.now(timezone.utc), 50.0)
    cuerpo = cliente.get(RUTA, params={"granularity": "day"}, headers=HEADERS).json()
    assert cuerpo["series"] == [{"date": ayer.isoformat(), "energy_kwh": 50.0}]


def test_granularidad_invalida(db_y_cliente):
    _, cliente = db_y_cliente
    assert cliente.get(RUTA, params={"granularity": "semana"}, headers=HEADERS).status_code == 422


def test_exige_api_key(db_y_cliente):
    _, cliente = db_y_cliente
    assert cliente.get(RUTA, params={"granularity": "hour"}).status_code == 401
