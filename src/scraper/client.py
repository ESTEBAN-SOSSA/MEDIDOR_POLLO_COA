"""Login y sesión Playwright contra oss.growatt.com.

Expone `growatt_session()`: async context manager que entrega una `Page` ya
autenticada en `/index` (con jQuery y `window.loadContent` disponibles).

Detalles críticos (confirmados contra producción) para que el login no sea
bloqueado: user-agent realista, `--disable-blink-features=AutomationControlled`,
una espera antes de rellenar el formulario y un init-script que acepta cookies
y marca el checkbox `#agree`. Ante un fallo guarda snapshots HTML+PNG.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import datetime

from playwright.async_api import Page, async_playwright

from src.config import settings
from src.scraper import selectors as S

log = logging.getLogger("scraper.client")

# Acepta cookies, marca el checkbox de términos (#agree) y oculta overlays.
INIT_SCRIPT = """
    try { localStorage.setItem('agreeCookie', 'true'); } catch (e) {}
    const cleanup = () => {
        ['fenquLayer','cookieLayer','noticeLayer'].forEach(id => {
            const e = document.getElementById(id);
            if (e) e.style.display = 'none';
        });
        document.querySelectorAll('.markBox, .cookies_tip').forEach(e => e.style.display = 'none');
        const agree = document.getElementById('agree');
        if (agree && agree.offsetParent !== null) {
            try { agree.click(); } catch (_) {}
        }
    };
    document.addEventListener('DOMContentLoaded', cleanup);
    const iv = setInterval(cleanup, 500);
    setTimeout(() => clearInterval(iv), 8000);
"""

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


async def _save_snapshot(page: Page, label: str) -> None:
    os.makedirs(settings.snapshot_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(settings.snapshot_dir, f"{label}_{stamp}")
    try:
        with open(f"{base}.html", "w", encoding="utf-8") as f:
            f.write(await page.content())
        await page.screenshot(path=f"{base}.png", full_page=True)
        log.warning("Snapshot guardado: %s.{html,png}", base)
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("No se pudo guardar snapshot: %s", exc)


@asynccontextmanager
async def growatt_session() -> AsyncIterator[Page]:
    """Inicia sesión y entrega la Page principal autenticada."""
    if not settings.growatt_user or not settings.growatt_password:
        raise RuntimeError(
            "Faltan GROWATT_USER / GROWATT_PASSWORD en el entorno (.env)."
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=settings.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent=_USER_AGENT,
            locale="en-US",
            viewport={"width": 1600, "height": 1000},
        )
        context.set_default_timeout(settings.nav_timeout_ms)
        await context.add_init_script(INIT_SCRIPT)
        page = await context.new_page()

        try:
            url = settings.login_url
            log.info("Login → %s", url)
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)

            await page.fill(S.LOGIN_USER_INPUT, settings.growatt_user)
            await page.fill(S.LOGIN_PASS_INPUT, settings.growatt_password)
            await page.click(S.LOGIN_SUBMIT_BTN, force=True)

            try:
                await page.wait_for_url(
                    lambda u: "login" not in u.lower(),
                    timeout=settings.nav_timeout_ms,
                )
            except Exception:
                await _save_snapshot(page, "login_failed")
                raise RuntimeError(
                    f"Login no navegó. URL actual: {page.url}. "
                    f"Revisa {settings.snapshot_dir}/login_failed.* (credenciales/captcha)."
                )

            await page.wait_for_load_state("networkidle")
            log.info("Login OK → %s", page.url)
            yield page
        finally:
            await context.close()
            await browser.close()
