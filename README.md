# MEDIDOR_POLLO_COA — Extracción de generación Growatt (POLLO COA)

Stack que extrae los datos de generación de la planta **POLLO COA** (`plant_id 1878757`)
desde el portal **Growatt OSS** (vía reverse-engineering con Playwright) y los expone
mediante una **API FastAPI** sobre **PostgreSQL**.

> El **paso a paso conceptual** completo (cómo funciona el portal, los endpoints XHR,
> el mapeo Hour/Day/Month) está en [`EXTRACCION_GROWATT.md`](./EXTRACCION_GROWATT.md).
> Este README es el **cómo correrlo**.

---

## Arquitectura

```
                 ┌──────────────┐      Playwright (headless)      ┌────────────────────┐
                 │   scraper    │ ─── login + showPlant + XHR ──► │  oss/server.growatt │
                 │ (scheduler)  │                                 └────────────────────┘
                 └──────┬───────┘
                        │  DELETE + INSERT (full refresh)
                        ▼
                 ┌──────────────┐        SQLAlchemy        ┌──────────────┐
                 │  PostgreSQL  │ ◄──────────────────────  │     api      │  GET /api/v1/...
                 │ energy_readings                         │  (FastAPI)   │  (X-API-Key)
                 └──────────────┘                          └──────────────┘
```

Servicios `docker compose`: **`db`** (Postgres 16), **`api`** (FastAPI/uvicorn :8000),
**`scraper`** (Playwright + scheduler diario 03:00 `America/Bogota`).

Granularidades persistidas en `energy_readings`: `hour`, `day`, `month`
(ver mapeo en el playbook §3).

---

## Requisitos

- Docker + Docker Compose
- Credenciales de Growatt OSS en `.env` (ya configuradas para este despliegue).

## Puesta en marcha (Docker)

```powershell
# 1) Configura el entorno (si no existe). Copia el ejemplo y rellena credenciales.
Copy-Item .env.example .env   # luego edita GROWATT_USER / GROWATT_PASSWORD

# 2) Levanta el stack (Postgres + API + scraper)
docker compose up -d --build

# 3) Crea tablas (idempotente) y corre la extracción de POLLO COA AHORA
docker compose exec scraper python -m src.db.init_db
docker compose exec scraper python -m src.scraper.main
```

> El scraper también corre **solo, todos los días a las 03:00** (scheduler interno,
> equivalente al cron del playbook). Para forzar un sync al arrancar el contenedor:
> pon `RUN_ON_START=true` en `.env`.

---

## Comandos que más se usan

```powershell
# Sync completo de POLLO COA (Day + Month histórico + Hour últimos 7 días)
docker compose exec scraper python -m src.scraper.main

# Hour (curva 5-min) de un rango de fechas (on-demand, idempotente)
docker compose exec scraper python -m src.scraper.hourly --plant_id 1878757 --from 2024-08-01 --to 2024-08-07

# Inspeccionar la BD
docker compose exec db psql -U growatt -d growatt -c "SELECT granularity, count(*) FROM energy_readings WHERE plant_id='1878757' GROUP BY granularity;"
```

## Leer los datos por la API

La API exige el header `X-API-Key` (en `main` → `EDEMCO_2026_GROWAT_GENERACION`).

```powershell
$h = @{ "X-API-Key" = "EDEMCO_2026_GROWAT_GENERACION" }

# Serie diaria de un rango
Invoke-RestMethod "http://localhost:8000/api/v1/plants/1878757/generation?granularity=day&date_from=2024-08-01&date_to=2024-08-31" -Headers $h

# Serie mensual (todo el histórico)
Invoke-RestMethod "http://localhost:8000/api/v1/plants/1878757/generation?granularity=month" -Headers $h

# Mes con desglose diario
Invoke-RestMethod "http://localhost:8000/api/v1/plants/1878757/monthly/2024/8" -Headers $h

# Curva horaria 5-min de un día (usa ?full=true para los 288 puntos)
Invoke-RestMethod "http://localhost:8000/api/v1/plants/1878757/day/2024-08-05" -Headers $h
```

Docs interactivas: <http://localhost:8000/docs>. Healthcheck: `GET /health`.

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Liveness (sin auth) |
| GET | `/api/v1/plants` | Plantas registradas |
| GET | `/api/v1/plants/{id}/generation?granularity=day\|month&date_from&date_to` | Serie Day/Month |
| GET | `/api/v1/plants/{id}/monthly/{year}/{month}` | Total del mes + desglose diario |
| GET | `/api/v1/plants/{id}/day/{YYYY-MM-DD}?full=true` | Curva Hour (5-min) reconstruida |

---

## Ejecutar en local (sin Docker, para depurar el scraper)

```powershell
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium

# Apunta la BD a localhost y, opcional, ve el navegador
$env:DATABASE_URL = "postgresql+psycopg://growatt:growatt@localhost:5432/growatt"
$env:SCRAPER_HEADLESS = "false"

python -m src.db.init_db
python -m src.scraper.main
```

---

## Estructura

```
src/
├── config.py              # settings desde .env
├── db/
│   ├── models.py          # EnergyReading, Plant, SyncRun
│   ├── session.py         # engine / SessionLocal
│   └── init_db.py         # create_all
├── scraper/
│   ├── selectors.py       # selectores + endpoints (único punto a tocar si Growatt cambia)
│   ├── client.py          # login Playwright (growatt_session)
│   ├── plant_energy.py    # núcleo: listing, popup, XHRs, persistencia
│   ├── main.py            # sync completo (acotado a POLLO COA)
│   ├── hourly.py          # CLI Hour on-demand por rango
│   └── scheduler.py       # sync diario 03:00
└── api/
    ├── main.py            # endpoints FastAPI
    └── deps.py            # auth X-API-Key
```

---

## Ramas / entornos

| Rama | Propósito | `API_KEY` |
|---|---|---|
| `main` | Producción | `EDEMCO_2026_GROWAT_GENERACION` |
| `staging` | Pre-producción | `PRUEBAS_GROWAT_GENERACION` |
| `DEV` | Desarrollo | `PRUEBAS_GROWAT_GENERACION` |

> El `.env` real **no** se versiona (está en `.gitignore`). Cada entorno define su
> propia `API_KEY` en su `.env`.

## Notas operativas

- **Full refresh, no upsert**: Day/Month se borran e insertan completos por planta;
  Hour borra/inserta por día (24 filas, incluso nocturnas en 0).
- **Snapshots**: ante un fallo de login/selector se guardan `snapshots/*.{html,png}`.
- **Extender a más plantas**: agrega IDs a `TARGET_PLANT_IDS` en `.env`.
