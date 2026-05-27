# Guía: extraer los datos de una planta (MEDIDOR_POLLO_COA)

> **Propósito.** Pasos concretos y copy-paste para traer la generación de **cualquier
> planta** de la cuenta Growatt a este stack. Si te piden "extrae los datos de la planta X",
> sigue la **§4**. El resto del documento explica la arquitectura, el flujo y el código por si
> necesitas entender o ajustar algo.
>
> Este documento es **específico de este repositorio** (el código que aquí se referencia
> existe en `src/`). El playbook conceptual original está en
> [`EXTRACCION_GROWATT.md`](./EXTRACCION_GROWATT.md).

---

## 0. TL;DR — extraer una planta NUEVA en 4 pasos

```powershell
# 1) Descubre el plant_id de la planta (lista todas las de la cuenta).
docker compose exec scraper python -m src.scraper.list_plants

# 2) Agrega ese ID a TARGET_PLANT_IDS en .env (coma-separado) y recrea el scraper.
#    Ej:  TARGET_PLANT_IDS=1878757,2771022
docker compose up -d scraper

# 3) Trae el histórico: Day + Month completos + Hour de los últimos 7 días.
docker compose exec scraper python -m src.scraper.main

# 4) (Opcional) Hour (curva 5-min) de un rango histórico puntual.
docker compose exec scraper python -m src.scraper.hourly --plant_id <ID> --from 2024-01-01 --to 2024-01-31
```

Leer lo extraído (header `X-API-Key`, ver §5):
`GET http://localhost:8001/api/v1/plants/<ID>/generation?granularity=month`

---

## 1. Arquitectura

```
                 ┌──────────────┐   Playwright (headless)      ┌─────────────────────┐
                 │   scraper    │ ─ login + showPlant + XHR ─► │ oss/server.growatt  │
                 │ (scheduler)  │                              └─────────────────────┘
                 └──────┬───────┘
                        │  DELETE + INSERT (full refresh) + poda Hour
                        ▼
                 ┌──────────────┐        SQLAlchemy        ┌──────────────┐
                 │  PostgreSQL  │ ◄──────────────────────  │     api      │  GET /api/v1/...
                 │ energy_readings                         │  (FastAPI)   │  (X-API-Key)
                 └──────────────┘                          └──────────────┘
```

Servicios `docker compose`:

| Servicio | Imagen / base | Rol | Puerto host |
|---|---|---|---|
| `db` | postgres:16-alpine | Almacena `energy_readings`, `plants`, `sync_runs` | `${DB_PORT}` (esta instalación: **5433**; default 5432) |
| `api` | FastAPI + uvicorn | Lectura de datos vía REST con `X-API-Key` | `${API_PORT}` (esta instalación: **8001**; default 8000) |
| `scraper` | mcr.microsoft.com/playwright/python | Login + extracción; corre el sync diario 03:00 (`TZ`) | — |

Growatt **no expone API pública** para esta cuenta → se hace *reverse engineering* del
portal con Playwright + sus XHR internos.

---

## 2. Flujo de extracción, paso a paso (con el código real)

### Paso 1 — Login · `src/scraper/client.py` → `growatt_session()`
1. `GET https://oss.growatt.com/login?lang=en`.
2. Rellena `#userName-id` / `#passWd-id` (de `.env`: `GROWATT_USER` / `GROWATT_PASSWORD`) y
   click en `input.loginInput-btn` (con `force=True`).
3. Espera a que la URL deje de contener `login` → aterriza en `/index` (jQuery + `window.loadContent`).
4. **Crítico para no ser bloqueado**: user-agent realista, `--disable-blink-features=AutomationControlled`,
   espera de 1,5 s antes de rellenar y un *init-script* que acepta cookies y marca el checkbox `#agree`.
5. Si falla, guarda `snapshots/login_failed_*.{html,png}`.

### Paso 2 — Listado de plantas · `plant_energy.py` → `get_plant_listing(page)`
1. `window.loadContent('deviceManage/plantManage')` carga el módulo Plant List.
2. Click en `#btn_search` (espera la respuesta XHR `plantManage/list`).
3. Del HTML parsea los botones `.allSeeSmallBtn[onclick*="showPlant"]` con la regex
   `showPlant('<server>', '<account>', <plant_id>)` y el **nombre** de la columna *Plant Name*.
4. **Dedup**: descarta plantas con sufijo `… ETAPA N` (mismos datos que la base).

Resultado: `[{server_id, account, plant_id, name_hint}]`.

### Paso 3 — Abrir sesión en server.growatt.com · `open_server_session(page, server_id, account, plant_id)`
- `window.showPlant(server_id, account, plant_id)` abre un **popup** con auto-login (302 `login_temp`).
- **La sesión queda activa por ACCOUNT** → se agrupan las plantas por `account` y se abre
  **un popup por cuenta** (no por planta).

### Paso 4 — Pedir los datos vía XHR · `_post_chart(...)` / `fetch_plant_hourly_for_date(...)`
`POST application/x-www-form-urlencoded` con `credentials:'include'` y header
`X-Requested-With: XMLHttpRequest` a `server.growatt.com/energy/compare/...`:

| Endpoint | Body extra | Devuelve | Mapea a |
|---|---|---|---|
| `getDevicesTotalChart` | `year=YYYY` | kWh por **AÑO** (su longitud = nº de años con datos) | (solo cuenta años) |
| `getDevicesYearChart`  | `year=YYYY` | **12** kWh (uno por mes) | **MONTH** |
| `getDevicesMonthChart` | `date=YYYY-MM` | kWh por **DÍA** del mes | **DAY** |
| `getDevicesDayChart`   | `date=YYYY-MM-DD` | **288** muestras de `pac` (W) cada 5 min | **HOUR** |

Body base: `plantId=<id>` + `jsonData=[{"type":"plant","sn":"<id>","params":"energy,autoEnergy"}]`
(`params:"pac"` para DayChart) + (`year=…` | `date=…`).
Respuesta útil: `obj[0].datas.energy` (o `.pac`). `result != 1` ⇒ array vacío.

### Paso 5 — Lógica por planta · `fetch_plant_history(sp, plant_id, current_year)`
1. `TotalChart` → cuántos años hay (`N`). Se asume `años = [current_year-N+1 … current_year]`.
2. Por cada año: `YearChart` → 12 totales mensuales (**MONTH**).
3. Por cada mes con kWh > 0: `MonthChart` → kWh por día (**DAY**).
4. Hour: por separado, `fetch_plant_hourly_for_date` por cada día solicitado → 24 horas
   (12 muestras de 5-min c/u; `slots = len(arr)//24`).

### Paso 6 — Persistencia · `persist_plant` / `persist_plant_hourly` / `prune_hourly`
**Full refresh, NO upsert**:
- Day/Month: `DELETE` de la planta (`granularity IN ('day','month')`) → `INSERT` fresco
  (se **omiten** lecturas con kWh ≤ 0).
- Hour: `DELETE` de esa planta para `granularity='hour' AND reading_date=<día>` → `INSERT` de
  **las 24 horas** (incluso nocturnas en 0), guardando las 12 muestras en `raw.samples_5min_w`.
- **Retención**: `prune_hourly` borra el Hour más viejo que `HOUR_RETENTION_DAYS` (default 180).
  **Day/Month nunca se podan.**

Cada corrida registra un `SyncRun` (`status`, `plants_synced`, `readings_synced`, `error`).

---

## 3. Modelo de datos (`src/db/models.py`)

Granularidades persistidas en `energy_readings`: `hour`, `day`, `month`.

| Vista | Origen Growatt | granularity | Campos clave | API |
|---|---|---|---|---|
| **HOUR** | `getDevicesDayChart` (288×5-min) | `hour`, 24 filas/día, `reading_hour 0..23` | `energy_kwh`, `peak_power_w`, `raw.samples_5min_w[12]`, `raw.avg_w` | `/day/{fecha}` |
| **DAY** | `getDevicesMonthChart` | `day`, 1 fila/día | `reading_date`, `energy_kwh` | `/generation?granularity=day` · `/monthly/{y}/{m}` |
| **MONTH** | `getDevicesYearChart` | `month`, `reading_date=YYYY-MM-01` | `reading_date`, `energy_kwh` | `/generation?granularity=month` |

- Índice único: `(plant_id, device_sn, reading_date, granularity, reading_hour)`.
  Para que funcione también en Day/Month (donde no hay hora) se usa el centinela
  `reading_hour = -1` (`NON_HOURLY`) en vez de NULL.
- Tabla `plants`: catálogo `{plant_id, name, server_id, account}`.
- Tabla `sync_runs`: bitácora de cada corrida.

---

## 4. Procedimiento: extraer una planta NUEVA  ⭐

> Requisito: stack arriba → `docker compose ps` muestra `db` (healthy), `api`, `scraper`.
> Si no, `docker compose up -d --build`.

### 4.1 Descubre el `plant_id`
```powershell
docker compose exec scraper python -m src.scraper.list_plants
```
Imprime `PLANT_ID | CUENTA | SERVER | NOMBRE` de **todas** las plantas de la cuenta.
Copia el `plant_id` de la planta que necesitas.

> Alternativa sin código: en el portal Growatt OSS, al abrir una planta el `onclick` es
> `showPlant('server','account', <plant_id>)` y la URL de `server.growatt.com` incluye ese id.

### 4.2 Agrégalo a `TARGET_PLANT_IDS`
Edita `.env` (coma-separado, sin espacios) y recrea el scraper para que relea el entorno:
```dotenv
TARGET_PLANT_IDS=1878757,<NUEVO_ID>
```
```powershell
docker compose up -d scraper
```
> Para extraer **solo** la nueva, pon únicamente su id. Para todas, lista todos los ids.

### 4.3 Trae Day + Month (histórico completo) + Hour (últimos 7 días)
```powershell
docker compose exec scraper python -m src.scraper.main
```
El log muestra: `Plantas a procesar`, los años detectados y `Planta <ID> OK: N días, M meses`.

### 4.4 (Opcional) Hour de un rango histórico puntual
El sync solo mantiene "caliente" los últimos 7 días. Para una curva 5-min de fechas viejas:
```powershell
docker compose exec scraper python -m src.scraper.hourly --plant_id <NUEVO_ID> --from 2024-08-01 --to 2024-08-07
```
Idempotente (borra el Hour previo de cada día antes de insertar).
> Ojo: el Hour viejo se re-podará en el siguiente sync diario si supera `HOUR_RETENTION_DAYS`.

### 4.5 Verifica
```powershell
# En la BD
docker compose exec db psql -U growatt -d growatt -c "SELECT granularity, count(*), min(reading_date), max(reading_date) FROM energy_readings WHERE plant_id='<NUEVO_ID>' GROUP BY granularity ORDER BY granularity;"

# Por la API (ver §5)
curl -s -H "X-API-Key: EDEMCO_2026_GROWAT_GENERACION" "http://localhost:8001/api/v1/plants/<NUEVO_ID>/generation?granularity=month"
```

### 4.6 (Opcional) Ajusta retención de Hour
En `.env`: `HOUR_RETENTION_DAYS=180` (0 = guardar todo). Luego `docker compose up -d scraper`.

---

## 5. Leer los datos (API REST)

La API escucha en `http://localhost:${API_PORT}` (**esta instalación: 8001**; default 8000) y
exige el header `X-API-Key` en todo `/api/v1/**` (`main` → `EDEMCO_2026_GROWAT_GENERACION`;
`staging`/`dev` → `PRUEBAS_GROWAT_GENERACION`). Swagger: `http://localhost:8001/docs`.

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Liveness (sin auth) |
| GET | `/api/v1/plants` | Plantas registradas |
| GET | `/api/v1/plants/{id}/generation?granularity=day\|month&date_from&date_to` | Serie Day/Month |
| GET | `/api/v1/plants/{id}/monthly/{year}/{month}` | Total del mes + desglose diario |
| GET | `/api/v1/plants/{id}/day/{YYYY-MM-DD}?full=true` | Curva Hour (5-min) reconstruida |

```powershell
$h = @{ "X-API-Key" = "EDEMCO_2026_GROWAT_GENERACION" }
Invoke-RestMethod "http://localhost:8001/api/v1/plants/<ID>/generation?granularity=day&date_from=2024-08-01&date_to=2024-08-31" -Headers $h
Invoke-RestMethod "http://localhost:8001/api/v1/plants/<ID>/monthly/2024/8" -Headers $h
Invoke-RestMethod "http://localhost:8001/api/v1/plants/<ID>/day/2024-08-05" -Headers $h
```

---

## 6. Mapa del código

| Archivo | Propósito |
|---|---|
| `src/config.py` | Settings desde `.env` (credenciales, `TARGET_PLANT_IDS`, retención, DB, API). |
| `src/scraper/selectors.py` | Selectores + endpoints. **Único punto a tocar si Growatt cambia el portal.** |
| `src/scraper/client.py` | Login Playwright (`growatt_session`) + snapshots. |
| `src/scraper/plant_energy.py` | Núcleo: listing, popup, XHRs, persistencia, `prune_hourly`. |
| `src/scraper/list_plants.py` | CLI: lista plantas e IDs (descubrir planta nueva). |
| `src/scraper/main.py` | Sync completo (`run_plant_energy_sync`), filtra por `TARGET_PLANT_IDS`. |
| `src/scraper/hourly.py` | CLI Hour on-demand por rango. |
| `src/scraper/scheduler.py` | Sync diario 03:00 (`TZ`). Comando por defecto del contenedor. |
| `src/db/models.py` | `EnergyReading`, `Plant`, `SyncRun`. |
| `src/db/session.py` · `init_db.py` | Engine/sesión · `create_all`. |
| `src/api/main.py` · `deps.py` | Endpoints FastAPI · auth `X-API-Key`. |

---

## 7. Configuración (`.env`)

| Variable | Default | Para qué |
|---|---|---|
| `GROWATT_USER` / `GROWATT_PASSWORD` | — | Credenciales del portal OSS. |
| `TARGET_PLANT_IDS` | `1878757` | IDs a procesar (coma-separado). **Aquí agregas una planta nueva.** |
| `HOURLY_DAYS_BACK` | `7` | Días de Hour que mantiene caliente el sync. |
| `HOUR_RETENTION_DAYS` | `180` | Poda Hour más viejo que N días (0 = sin poda). |
| `SCRAPER_HEADLESS` | `true` | `false` en local para ver el navegador y depurar. |
| `NAV_TIMEOUT_MS` | `60000` | Timeout de navegación/XHR. |
| `DATABASE_URL` | `…@db:5432/growatt` | Conexión Postgres (host `db` en Docker). |
| `API_KEY` | `EDEMCO_2026_GROWAT_GENERACION` | Clave del header `X-API-Key`. |
| `DB_PORT` / `API_PORT` | `5432` / `8000` | Puertos del host (esta máquina: 5433 / 8001). |
| `TZ` | `America/Bogota` | Zona horaria del sync diario. |

> El `.env` real **no** se versiona (`.gitignore`). `.env.example` es la plantilla por entorno/rama.

---

## 8. Operación y troubleshooting

- **Sync diario**: el contenedor `scraper` corre el sync a las **03:00** (`TZ`). Para uno puntual:
  `docker compose exec scraper python -m src.scraper.main`.
- **Login falla** (`login_failed.*` en `snapshots/`): credenciales, captcha o selectores cambiados
  → ajusta `src/scraper/selectors.py`.
- **`showPlant() no abrió popup`**: bloqueador de popups o cambió `showPlant`; revisa `selectors.py`
  y que el `account`/`server_id` sean correctos (relista con `list_plants`).
- **No encuentra la planta**: confirma el `plant_id` con `list_plants`; `main` cae a buscar
  por nombre "POLLO COA" solo como respaldo.
- **Puerto ocupado**: en esta máquina ya corre otro stack Growatt en 5432/8000; por eso este
  usa `DB_PORT=5433` / `API_PORT=8001`. Cámbialos en `.env` si hace falta.
- **Recrear tras cambiar `.env`**: `docker compose up -d` (el scraper/api releen el entorno).
- **Inspeccionar la última corrida**:
  `docker compose exec db psql -U growatt -d growatt -c "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 3;"`
