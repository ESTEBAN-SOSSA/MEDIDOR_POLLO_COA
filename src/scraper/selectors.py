"""Selectores y endpoints del portal Growatt.

Centralizados aquí: si Growatt reorganiza el portal, este es el único archivo
que normalmente hay que tocar (ver nota del playbook, sección 7).

Valores confirmados contra la implementación en producción del portal OSS.
"""
from __future__ import annotations

# ── Login (oss.growatt.com/login) ───────────────────────────────────────────
LOGIN_USER_INPUT = "#userName-id"
LOGIN_PASS_INPUT = "#passWd-id"
LOGIN_SUBMIT_BTN = "input.loginInput-btn"

# ── Listado de plantas (deviceManage/plantManage) ───────────────────────────
PLANT_LIST_MODULE = "deviceManage/plantManage"
PLANT_SEARCH_BTN = "#btn_search"
PLANT_TABLE = "table.tbl_plantList"
PLANT_SHOW_BTN = ".allSeeSmallBtn[onclick*=\"showPlant\"]"
PLANT_LIST_XHR = "plantManage/list"  # respuesta que dispara #btn_search

# showPlant('<server_id>', '<account>', <plant_id>)  ← server/account citados,
# plant_id va SIN comillas. Soporta comillas simples o dobles.
SHOW_PLANT_REGEX = (
    r"showPlant\(['\"](\d+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*(\d+)\)"
)

# Plantas a descartar por ser duplicado de la base (mismos datos): "… ETAPA N".
DEDUP_ETAPA_REGEX = r"\s+ETAPA\s+\d+\s*$"

# ── Endpoints de datos (server.growatt.com/energy/compare/...) ──────────────
CHART_TOTAL = "getDevicesTotalChart"  # kWh por año (solo para contar años)
CHART_YEAR = "getDevicesYearChart"    # 12 kWh (uno por mes) -> MONTH
CHART_MONTH = "getDevicesMonthChart"  # kWh por día del mes  -> DAY
CHART_DAY = "getDevicesDayChart"      # 288 muestras pac (W) -> HOUR
