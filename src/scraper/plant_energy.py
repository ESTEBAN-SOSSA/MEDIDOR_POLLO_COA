"""Corazón de la extracción: listado, popup por cuenta, XHRs de charts y
persistencia (full refresh) en `energy_readings`.

Reverse engineering del flujo real del portal (confirmado contra producción):
  1. Plant List (deviceManage/plantManage): el click en #btn_search dispara el
     XHR `plantManage/list`; del HTML se parsean los botones `.allSeeSmallBtn`
     con onclick `showPlant(serverId, account, plantId)` y el nombre de la
     columna "Plant Name".
  2. window.showPlant(...) abre un popup con auto-login (302 login_temp) en
     server.growatt.com. La sesión queda activa por ACCOUNT.
  3. XHRs POST a /energy/compare/get*Chart:
       getDevicesTotalChart (year)  -> kWh por AÑO (longitud = nº de años)
       getDevicesYearChart  (year)  -> 12 kWh (uno por mes)      -> MONTH
       getDevicesMonthChart (date)  -> kWh por día del mes        -> DAY
       getDevicesDayChart   (date)  -> 288 muestras pac (W) 5-min -> HOUR
     Respuesta útil: obj[0].datas.energy (o .pac). result != 1 ⇒ vacío.
  4. Persistencia FULL REFRESH (no upsert): DELETE previo + INSERT. Se omiten
     lecturas con kWh ≤ 0 (día/mes); Hour guarda SIEMPRE las 24 horas.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from calendar import monthrange
from datetime import date

from playwright.async_api import Page
from sqlalchemy import delete
from sqlalchemy.orm import Session

from src.config import settings
from src.db.models import NON_HOURLY, EnergyReading, Plant
from src.scraper import selectors as S

log = logging.getLogger("scraper.plant_energy")

_SHOW_PLANT_RE = re.compile(S.SHOW_PLANT_REGEX)

# Factor para convertir una muestra de potencia (W) tomada cada 5 min a kWh.
SLOT_FACTOR = 5.0 / 60.0 / 1000.0


def _to_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ── Paso 2: listado de plantas ──────────────────────────────────────────────
async def get_plant_listing(page: Page) -> list[dict]:
    """Carga Plant List y extrae [{server_id, account, plant_id, name_hint}].

    Descarta duplicados con sufijo "… ETAPA N" (mismos datos que la base).
    """
    log.info("Cargando módulo de plantas (%s)…", S.PLANT_LIST_MODULE)
    await page.evaluate(f"() => window.loadContent('{S.PLANT_LIST_MODULE}')")
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(2500)

    async with page.expect_response(
        lambda r: S.PLANT_LIST_XHR in r.url, timeout=settings.nav_timeout_ms
    ):
        await page.click(S.PLANT_SEARCH_BTN, force=True)
    await page.wait_for_timeout(1500)

    raw = await page.evaluate(
        """() => {
            const headers = Array.from(
                document.querySelectorAll('table.tbl_plantList thead th')
            ).map(t => t.innerText.trim().toLowerCase());
            let nameIdx = headers.findIndex(
                h => h === 'plant name' || h === 'electricity station name'
            );
            if (nameIdx < 0) nameIdx = 4;  // fallback típico

            const rows = Array.from(
                document.querySelectorAll('table.tbl_plantList tbody tr')
            );
            const out = [];
            rows.forEach(tr => {
                const btn = tr.querySelector('.allSeeSmallBtn[onclick*="showPlant"]');
                if (!btn) return;
                const cells = tr.querySelectorAll('td');
                const name = nameIdx < cells.length
                    ? (cells[nameIdx].innerText || '').trim() : '';
                out.push({
                    onclick: btn.getAttribute('onclick'),
                    name: name,
                    allCells: Array.from(cells).map(c => c.innerText.trim()),
                });
            });
            return out;
        }"""
    )

    raw_plants: list[dict] = []
    for r in raw:
        m = _SHOW_PLANT_RE.search(r.get("onclick") or "")
        if not m:
            continue
        name = r.get("name") or ""
        if not name:
            for t in r.get("allCells", []):
                if (
                    3 < len(t) < 80
                    and t.lower() not in ("online", "offline", "fault", "waiting")
                    and not t.isdigit()
                ):
                    name = t
                    break
        raw_plants.append(
            {
                "server_id": m.group(1),
                "account": m.group(2),
                "plant_id": m.group(3),
                "name_hint": name or f"Plant {m.group(3)}",
            }
        )

    # Dedup por nombre base (quita "ETAPA N").
    seen: dict[str, dict] = {}
    dropped: list[str] = []
    for pl in raw_plants:
        base = (
            re.sub(S.DEDUP_ETAPA_REGEX, "", pl["name_hint"], flags=re.IGNORECASE)
            .strip()
            .upper()
        ) or pl["plant_id"]
        if base in seen:
            dropped.append(pl["name_hint"])
        else:
            seen[base] = pl

    if dropped:
        log.info("Descartadas por duplicado ETAPA: %s", dropped)
    plants = list(seen.values())
    log.info("Plantas únicas encontradas: %d", len(plants))
    return plants


# ── Paso 3: abrir popup (sesión por cuenta) ─────────────────────────────────
async def open_server_session(
    page: Page, server_id: str, account: str, plant_id: str
) -> Page:
    """Dispara window.showPlant(...) y devuelve el popup ya en server.growatt.com."""
    new_pages: list[Page] = []
    page.context.on("page", lambda np: new_pages.append(np))

    await page.evaluate(
        f"() => window.showPlant('{server_id}', '{account}', {plant_id})"
    )
    for _ in range(int(settings.nav_timeout_ms / 500)):
        await asyncio.sleep(0.5)
        if new_pages:
            break
    if not new_pages:
        raise RuntimeError(f"showPlant() no abrió popup (account={account[:8]}…)")

    sp = new_pages[-1]
    await sp.wait_for_load_state("networkidle", timeout=settings.nav_timeout_ms)
    await sp.wait_for_timeout(2000)
    if "server.growatt.com" not in sp.url:
        raise RuntimeError(f"Popup no llegó a server.growatt.com: {sp.url}")
    return sp


# ── Paso 4: XHR a los charts ────────────────────────────────────────────────
async def _post_chart(sp: Page, endpoint: str, plant_id: str, params: str, **extra) -> list[float]:
    url = f"{settings.server_base}/energy/compare/{endpoint}"
    body = {
        "plantId": str(plant_id),
        "jsonData": json.dumps(
            [{"type": "plant", "sn": str(plant_id), "params": params}]
        ),
        **{k: str(v) for k, v in extra.items()},
    }
    js = """async ({url, body}) => {
        const params = new URLSearchParams(body);
        const r = await fetch(url, {
            method: 'POST',
            credentials: 'include',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest'
            },
            body: params.toString()
        });
        return await r.json();
    }"""
    data = await sp.evaluate(js, {"url": url, "body": body})
    key = "pac" if endpoint == S.CHART_DAY else "energy"
    if not isinstance(data, dict) or data.get("result") != 1:
        return []
    arr = ((data.get("obj") or [{}])[0].get("datas") or {}).get(key) or []
    return [_to_float(x) for x in arr]


# ── Paso 5: lógica por planta ───────────────────────────────────────────────
async def fetch_plant_history(sp: Page, plant_id: str, current_year: int) -> dict:
    """Devuelve {years:[int], monthly:{yr:[12]}, daily:{(yr,mi):[...]}}."""
    yearly = await _post_chart(sp, S.CHART_TOTAL, plant_id, "energy,autoEnergy", year=current_year)
    n = len(yearly)
    if n == 0:
        return {"years": [], "monthly": {}, "daily": {}}
    years = list(range(current_year - n + 1, current_year + 1))
    log.info("Planta %s → %d año(s): %s", plant_id, n, years)

    monthly: dict[int, list[float]] = {}
    daily: dict[tuple[int, int], list[float]] = {}
    for yr in years:
        m12 = await _post_chart(sp, S.CHART_YEAR, plant_id, "energy,autoEnergy", year=yr)
        m12 = (m12 + [0.0] * 12)[:12]
        monthly[yr] = m12
        for mi, mkwh in enumerate(m12, start=1):
            if mkwh <= 0:
                continue
            days = await _post_chart(
                sp, S.CHART_MONTH, plant_id, "energy,autoEnergy", date=f"{yr:04d}-{mi:02d}"
            )
            daily[(yr, mi)] = days
        await asyncio.sleep(0.1)  # cortesía con el portal
    return {"years": years, "monthly": monthly, "daily": daily}


async def fetch_plant_hourly_for_date(sp: Page, plant_id: str, day: date) -> dict[int, dict]:
    """{hour 0..23: {kwh, peak_w, avg_w, samples}} a partir de las muestras pac 5-min.

    Devuelve SIEMPRE las 24 horas (las nocturnas en 0).
    """
    empty = {h: {"kwh": 0.0, "peak_w": 0.0, "avg_w": 0.0, "samples": []} for h in range(24)}
    arr = await _post_chart(sp, S.CHART_DAY, plant_id, "pac", date=day.isoformat())
    if not arr:
        return empty

    slots = max(1, len(arr) // 24)
    hourly: dict[int, dict] = {}
    for h in range(24):
        chunk = arr[h * slots : h * slots + slots]
        kwh = sum(chunk) * SLOT_FACTOR
        hourly[h] = {
            "kwh": round(kwh, 3),
            "peak_w": round(max(chunk), 1) if chunk else 0.0,
            "avg_w": round(sum(chunk) / len(chunk), 1) if chunk else 0.0,
            "samples": [round(x, 1) for x in chunk],
        }
    return hourly


# ── Paso 6: persistencia (FULL REFRESH, no upsert) ──────────────────────────
def upsert_plant(session: Session, plant_id: str, name_hint: str) -> None:
    row = session.get(Plant, plant_id)
    if row is None:
        session.add(Plant(plant_id=plant_id, name=(name_hint or f"Plant {plant_id}")[:255]))
        session.flush()


def persist_plant(session: Session, plant_id: str, history: dict) -> tuple[int, int]:
    """Borra Day/Month de la planta e inserta fresco (omitiendo kWh ≤ 0)."""
    session.execute(
        delete(EnergyReading).where(
            EnergyReading.plant_id == plant_id,
            EnergyReading.granularity.in_(("day", "month")),
        )
    )
    months_inserted = 0
    for yr, m12 in history["monthly"].items():
        for mi, kwh in enumerate(m12, start=1):
            if kwh <= 0:
                continue
            session.add(
                EnergyReading(
                    plant_id=plant_id,
                    device_sn="",
                    reading_date=date(yr, mi, 1),
                    granularity="month",
                    reading_hour=NON_HOURLY,
                    energy_kwh=kwh,
                    raw={"source": "plant_energy"},
                )
            )
            months_inserted += 1

    days_inserted = 0
    for (yr, mi), arr in history["daily"].items():
        _, max_day = monthrange(yr, mi)
        for di, kwh in enumerate(arr, start=1):
            if di > max_day:
                break
            if kwh <= 0:
                continue
            session.add(
                EnergyReading(
                    plant_id=plant_id,
                    device_sn="",
                    reading_date=date(yr, mi, di),
                    granularity="day",
                    reading_hour=NON_HOURLY,
                    energy_kwh=kwh,
                    raw={"source": "plant_energy"},
                )
            )
            days_inserted += 1

    session.flush()
    return days_inserted, months_inserted


def persist_plant_hourly(session: Session, plant_id: str, day: date, hourly: dict[int, dict]) -> int:
    """Borra el Hour previo de `day` e inserta las 24 horas (12 muestras c/u)."""
    session.execute(
        delete(EnergyReading).where(
            EnergyReading.plant_id == plant_id,
            EnergyReading.granularity == "hour",
            EnergyReading.reading_date == day,
        )
    )
    for h in range(24):
        entry = hourly.get(h) or {"kwh": 0.0, "peak_w": 0.0, "avg_w": 0.0, "samples": []}
        session.add(
            EnergyReading(
                plant_id=plant_id,
                device_sn="",
                reading_date=day,
                granularity="hour",
                reading_hour=h,
                energy_kwh=entry["kwh"],
                peak_power_w=entry["peak_w"],
                raw={
                    "source": "plant_energy_hour",
                    "avg_w": entry["avg_w"],
                    "samples_5min_w": entry["samples"],
                },
            )
        )
    session.flush()
    return 24
