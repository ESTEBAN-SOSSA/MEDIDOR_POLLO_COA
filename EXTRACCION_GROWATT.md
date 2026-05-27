# Extracción de datos de Growatt — Playbook

> Guía paso a paso de **cómo se extraen los datos de generación de las plantas desde Growatt OSS** en este
> proyecto, pensada para reutilizarse: cuando se pida "extrae los datos de la planta X", basta seguir este
> documento. Cubre el mapeo por **Hour**, **Day** y **Month**, los comandos listos para correr y una
> recomendación sobre límites de extracción.

---

## 0. TL;DR — comandos que más vas a usar

```powershell
# Refresh COMPLETO de todas las plantas (Day + Month histórico + Hour últimos 7 días). ~7 min.
docker compose exec scraper python -m src.scraper.main

# Hour (curva 5-min) de UNA planta en un rango de fechas (on-demand, idempotente).
docker compose exec scraper python -m src.scraper.hourly --plant_id 1878757 --from 2024-08-01 --to 2024-08-07

# Leer lo extraído (API, requiere header X-API-Key: EDEMCO_2026_GROWAT_GENERACION):
#   Day/Month series:  GET /api/v1/plants/{id}/generation?granularity=day|month
#   Mes con desglose:  GET /api/v1/plants/{id}/monthly/{year}/{month}
#   Hour (curva 5-min):GET /api/v1/plants/{id}/day/{YYYY-MM-DD}
```

---

## 1. Arquitectura del flujo de extracción

Growatt **no expone una API pública** para esta cuenta, así que se hace *reverse engineering* del portal con
Playwright (navegador headless) + sus XHR internos.

```
oss.growatt.com (login)                server.growatt.com (datos)
        │                                       │
  1. Login ──► /index (window.loadContent)      │
        │                                       │
  2. Plant List (deviceManage/plantManage)      │
     parsea onclick showPlant(srv,acc,plant)    │
        │                                       │
  3. window.showPlant(...) ─── popup 302 ──────►│  sesión auto-login por ACCOUNT
        │                                       │
  4. fetch() XHR a /energy/compare/get*Chart ──►│  devuelve arrays de kWh / W
        │                                       │
  5. DELETE + INSERT por planta ──► PostgreSQL (energy_readings)
```

Código clave:
- `src/scraper/client.py` — login y sesión Playwright (`growatt_session()`).
- `src/scraper/plant_energy.py` — **corazón de la extracción** (listado, popup, XHRs, persistencia).
- `src/scraper/main.py` — entrypoint del sync completo (`run_plant_energy_sync`).
- `src/scraper/hourly.py` — CLI on-demand para Hour de una planta/rango.
- `src/db/models.py` — modelo `EnergyReading` (donde aterrizan los datos).

---

## 2. Paso a paso de la extracción

### Paso 1 — Login (`client.py`)
1. `GET https://oss.growatt.com/login?lang=en`
2. Rellena `#userName-id` / `#passWd-id` (de `.env`: `GROWATT_USER` / `GROWATT_PASSWORD`) y click `input.loginInput-btn`.
3. Espera a que la URL deje de contener `login` → aterriza en `/index` (carga jQuery y `window.loadContent`).
4. Un `init_script` oculta banners de cookies/avisos para que no tapen los clicks.

> Si el login falla, guarda `snapshots/login_failed.html|png` para diagnosticar (selectores o captcha).

### Paso 2 — Listado de plantas (`get_plant_listing`)
1. `window.loadContent('deviceManage/plantManage')` carga el módulo Plant List.
2. Click en `#btn_search` dispara el XHR `plantManage/list`.
3. Del HTML de la tabla `table.tbl_plantList` se parsean los `onclick` con la regex:
   `showPlant('<server_id>', '<account>', <plant_id>)` + el **nombre** de la planta.
4. **Dedup**: se descartan plantas con sufijo `ETAPA 2` (mismos datos que la base).

Resultado: lista de `{server_id, account, plant_id, name_hint}`.

### Paso 3 — Abrir sesión en server.growatt.com (`open_server_session`)
- `window.showPlant(server_id, account, plant_id)` abre un **popup** que hace auto-login (302 `login_temp`)
  en `server.growatt.com`. **La sesión queda activa por ACCOUNT**, así que se agrupan las plantas por
  `account` y se abre **un popup por cuenta** (no por planta) → más rápido.

### Paso 4 — Pedir los datos vía XHR (`_post_chart` / `fetch_plant_hourly_for_date`)
Dentro del popup se hace `fetch()` con `credentials:'include'` a estos 4 endpoints
(`POST application/x-www-form-urlencoded`, header `X-Requested-With: XMLHttpRequest`):

| Endpoint (`server.growatt.com/energy/compare/...`) | Body extra | Devuelve | Mapea a |
|---|---|---|---|
| `getDevicesTotalChart` | `year=YYYY` | array kWh por **AÑO** (su longitud = nº de años con datos) | (solo se usa para saber cuántos años hay) |
| `getDevicesYearChart`  | `year=YYYY` | array de **12** kWh (uno por mes) | **MONTH** |
| `getDevicesMonthChart` | `date=YYYY-MM` | array kWh por **DÍA** del mes | **DAY** |
| `getDevicesDayChart`   | `date=YYYY-MM-DD` | **288** muestras de `pac` (W) cada 5 min | **HOUR** |

Body base común:
```
plantId=<plant_id>
jsonData=[{"type":"plant","sn":"<plant_id>","params":"energy,autoEnergy"}]   // pac para DayChart
+ (year=... | date=...)
```
La respuesta útil es `obj[0].datas.energy` (o `.pac` para DayChart). `result != 1` ⇒ array vacío.

### Paso 5 — Lógica por planta (`fetch_plant_history`)
1. `TotalChart` → cuántos años hay (`N`). Se asume `años = [current_year-N+1 … current_year]`.
2. Por cada año: `YearChart` → 12 totales mensuales (**MONTH**).
3. Por cada mes con kWh > 0: `MonthChart` → kWh por día (**DAY**).
4. Hour: por separado, `fetch_plant_hourly_for_date` por cada día solicitado (**HOUR**).

### Paso 6 — Persistencia (`persist_plant` / `persist_plant_hourly`)
**Full refresh, NO upsert** (por pedido del proyecto):
- Day/Month: `DELETE` de `energy_readings` de esa planta con `granularity IN ('day','month')` → `INSERT` fresco.
- Hour: `DELETE` de esa planta para `granularity='hour' AND reading_date=<día>` → `INSERT` de **las 24 horas**
  (incluso las nocturnas en 0), guardando las 12 muestras 5-min en `raw.samples_5min_w`.

Cada corrida registra un `SyncRun` (`status`, `plants_synced`, `readings_synced`, `error`).

---

## 3. Mapeo Hour / Day / Month → BD → API

Granularidades **realmente persistidas**: `hour`, `day`, `month`. (El **año/lifetime** solo se lee para contar
años; **no se guarda** — el endpoint `granularity=lifetime` quedaría vacío salvo que se extienda el sync.)

| Vista | Origen Growatt | Se guarda en `energy_readings` | Campos | API para leerlo |
|---|---|---|---|---|
| **HOUR** | `getDevicesDayChart` (288×5-min) | `granularity='hour'`, 24 filas/día, `reading_hour 0..23` | `energy_kwh`, `peak_power_w`, `raw.samples_5min_w[12]`, `raw.avg_w` | `GET /plants/{id}/day/{YYYY-MM-DD}` |
| **DAY** | `getDevicesMonthChart` | `granularity='day'`, 1 fila/día | `reading_date`, `energy_kwh` | `GET /plants/{id}/generation?granularity=day` · `GET /plants/{id}/monthly/{y}/{m}` |
| **MONTH** | `getDevicesYearChart` | `granularity='month'`, `reading_date=YYYY-MM-01` | `reading_date`, `energy_kwh` | `GET /plants/{id}/generation?granularity=month` |

Modelo `EnergyReading` (resumen): índice único por
`(plant_id, device_sn, reading_date, granularity, reading_hour)`.

### Cómo se reconstruye HOUR en el API (`/plants/{id}/day/{date}`)
- Toma las 24 filas `hour` del día, concatena los 12 samples de cada hora → array de **288** valores W.
- Calcula `total_kwh = Σ(W) × 5/60/1000`, `peak_power_w`, `peak_time`, `first/last_generation_at`.
- Devuelve `data[]` = `{time:"HH:MM", power_w}` **recortado** al rango con generación (réplica del chart Hour del
  OSS). Usa `?full=true` para los 288 puntos completos `00:00 → 23:55`.

---

## 4. Cómo extraer una planta específica (paso a paso reutilizable)

> Requisito: stack arriba (`docker compose ps` → `growatt_db`, `growatt_api`, `growatt_scraper`).

### A) Hour (curva 5-min) de UNA planta y rango — soporte directo
```powershell
docker compose exec scraper python -m src.scraper.hourly --plant_id <PLANT_ID> --from <YYYY-MM-DD> --to <YYYY-MM-DD>
# Ej: POLLO COA, primera semana de agosto 2024
docker compose exec scraper python -m src.scraper.hourly --plant_id 1878757 --from 2024-08-01 --to 2024-08-07
```
- Idempotente (borra el Hour previo de cada día antes de insertar).
- Omitir `--plant_id` ⇒ todas las plantas.

### B) Day + Month (histórico) — vía sync completo
No hay flag por-planta para Day/Month; el sync los trae para **todas** las plantas en una pasada (~7 min):
```powershell
docker compose exec scraper python -m src.scraper.main
```
El `run_plant_energy_sync(hourly_days_back=7)` también refresca Hour de los últimos 7 días por planta.

> Si necesitas Day/Month de **una sola** planta de forma aislada, hoy la opción más limpia es correr el sync
> completo. (Extender `hourly.py` con un modo `--day-month` por planta sería un cambio pequeño si se vuelve
> recurrente.)

### C) Leer lo extraído desde la API
```powershell
$h = @{ "X-API-Key" = "EDEMCO_2026_GROWAT_GENERACION" }
# Serie diaria de un rango
Invoke-RestMethod "http://localhost:8000/api/v1/plants/1878757/generation?granularity=day&date_from=2024-08-01&date_to=2024-08-31" -Headers $h
# Mes con desglose diario
Invoke-RestMethod "http://localhost:8000/api/v1/plants/1878757/monthly/2024/8" -Headers $h
# Curva horaria 5-min de un día
Invoke-RestMethod "http://localhost:8000/api/v1/plants/1878757/day/2024-08-05" -Headers $h
```

### D) Consultar la BD directamente (postgres)
```powershell
docker compose exec db psql -U growatt -d growatt -c "SELECT reading_date, energy_kwh FROM energy_readings WHERE plant_id='1878757' AND granularity='day' ORDER BY reading_date DESC LIMIT 10;"
```

---

## 5. Plantas disponibles (referencia)

| plant_id | Nombre | Días | Meses | Desde | Hasta |
|---|---|---|---|---|---|
| 1113339 | CEIPA BARRANQUILLA | 1376 | 49 | 2022-05-10 | 2026-05-26 |
| 915129 | CEIPA SABANETA | 1465 | 52 | 2022-02-09 | 2026-05-26 |
| 2771022 | GUATAPURÍ | 234 | 16 | 2024-07-05 | 2026-05-26 |
| 1504765 | INCUBANT | 1253 | 43 | 2022-11-16 | 2026-05-26 |
| 2266007 | LEMONT PORTERIA | 950 | 32 | 2023-10-12 | 2026-05-26 |
| 10609700 | LEMONT SALÓN SOCIAL | 1283 | 43 | 2022-05-06 | 2026-05-26 |
| 783513 | LICEO FRANCES | 1607 | 53 | 2022-01-01 | 2026-05-26 |
| 1878757 | POLLO COA | 1192 | 51 | 2022-04-16 | 2026-05-26 |
| 1615068 | PUNTO CLAVE | 1231 | 42 | 2022-12-02 | 2026-05-26 |
| 798662 | SSFV Mario Restrepo | 1559 | 53 | 2022-01-01 | 2026-05-26 |
| 411711 | Sede Edemco | 0 | 0 | — | — |

> "Sede Edemco" no genera datos de energía (totales en 0); solo aparece su Hour vacío. Los `plant_id` pueden
> cambiar si Growatt reorganiza la cuenta — re-verifica con `get_plant_listing` o la tabla `plants`.

---

## 6. ⚖️ Recomendación: ¿límite de 1 año o serie de tiempo en PostgreSQL?

**Respuesta corta: serie de tiempo completa en PostgreSQL es mejor — pero acotando *solo* la granularidad Hour
por rango de fechas.** No conviene un límite global de "1 año".

### Por qué (con los números reales de este proyecto)
- **Day + Month** pesan poquísimo: ~1.200–1.600 filas/día + ~50 filas/mes por planta. Las 11 plantas suman
  **~15.900 filas** en total (lo verificado en BD). Postgres maneja eso sin despeinarse, con índices ya creados.
  Además **Growatt te entrega el histórico completo "gratis"** en una sola pasada (`TotalChart`/`YearChart`/
  `MonthChart`), así que limitarlo a 1 año **tira valor analítico** (comparativos año-contra-año, tendencias,
  estacionalidad) **sin ahorrar nada** relevante de almacenamiento.
- **Hour (5-min)** es lo único pesado: son 24 filas/día con arrays de 12 muestras. El histórico completo serían
  ~24 × ~1.500 días × 11 plantas ≈ **400k filas** + JSON voluminoso, y Growatt no garantiza datos 5-min
  antiguos. Por eso Hour se extrae **on-demand por rango** (`hourly.py`) y el sync diario solo mantiene
  "caliente" los **últimos 7 días**.

### Regla práctica recomendada
| Granularidad | Estrategia | Límite |
|---|---|---|
| **Month / Day** | Serie completa en Postgres (full refresh) | **Sin límite** — guarda todo el histórico |
| **Hour (5-min)** | On-demand por rango cuando se necesite | Acota por **fechas** (no por "1 año"); 7 días en el sync diario |

### Cuándo sí pensar en límites / escalar
- Si algún día quieres **Hour histórico masivo**, no uses Postgres "plano": migra esa tabla a **TimescaleDB**
  (hypertables + compresión + retención automática) — ya está en el roadmap del README.
- Política de retención sugerida si crece: Day/Month → indefinido; Hour → retención (p.ej. 90–180 días) +
  re-extracción on-demand para fechas viejas puntuales.

**Conclusión:** PostgreSQL como serie de tiempo para Day/Month (todo el histórico) + Hour on-demand acotado por
fechas. El "límite de 1 año" solo tendría sentido como tope del rango Hour, nunca para Day/Month.

---

## 7. Notas operativas
- **API Key** (header `X-API-Key`) por entorno/rama: `main` → `EDEMCO_2026_GROWAT_GENERACION`;
  `staging`/`dev` → `PRUEBAS_GROWAT_GENERACION`. Si recreas contenedores releen `.env` (`docker compose up -d`).
- **Headless**: en Docker `SCRAPER_HEADLESS=true`; en local `false` (ves el navegador) — útil para depurar
  selectores con `python -m src.scraper.main` fuera de Docker.
- **Cron**: el contenedor `scraper` corre el sync diario a las **03:00** (`TZ=America/Bogota`).
- **Snapshots**: ante fallos se guardan HTML+PNG en `snapshots/` para diagnosticar selectores.
- **Selectores**: viven en `src/scraper/selectors.py`; si Growatt cambia el portal, ajústalos ahí.
