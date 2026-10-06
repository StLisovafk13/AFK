"""Bounded startup probes using the transports employed by media workers."""
import asyncio
import contextlib
import ipaddress
import json
from html import escape

import aiohttp

ENDPOINT = "https://ipwho.is/"
TIMEOUT = 10


def describe_response(data):
    if not isinstance(data, dict) or data.get("success") is False:
        raise ValueError("Сервис определения IP не вернул успешный результат")
    address = str(ipaddress.ip_address(data.get("ip", "")))
    country = str(data.get("country") or data.get("country_code") or "не определена")
    return f"IP: {escape(address)}; страна: {escape(country)}"


async def _probe(label, operation):
    try:
        data = await asyncio.wait_for(operation(), TIMEOUT)
        return f"• {label}: {describe_response(data)}"
    except Exception as exc:
        # Error messages can contain proxy URLs with credentials. Report only type.
        return f"• {label}: проверка не удалась ({type(exc).__name__})"


async def http_report():
    async def request():
        # Match collect_profile_media: default trust_env=False, no explicit proxy.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
            async with session.get(ENDPOINT) as response:
                response.raise_for_status()
                return await response.json()
    return await _probe("HTTP-сканирование (aiohttp)", request)


async def browser_report():
    browser = context = None
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            try:
                browser = await asyncio.wait_for(playwright.firefox.launch(headless=True), TIMEOUT)
                context = await asyncio.wait_for(browser.new_context(), TIMEOUT)

                async def navigate():
                    page = await context.new_page()
                    try:
                        response = await page.goto(ENDPOINT, wait_until="domcontentloaded", timeout=TIMEOUT * 1000)
                        if response is None or not response.ok:
                            raise ValueError("IP endpoint returned an HTTP error")
                        return json.loads(await page.locator("body").inner_text())
                    finally:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(page.close(), 2)

                async def download():
                    response = await context.request.get(ENDPOINT, timeout=TIMEOUT * 1000)
                    try:
                        if not response.ok:
                            raise ValueError("IP endpoint returned an HTTP error")
                        return await response.json()
                    finally:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(response.dispose(), 2)

                return list(await asyncio.gather(
                    _probe("Браузерное сканирование (Firefox)", navigate),
                    _probe("Скачивание (Playwright context.request)", download),
                ))
            finally:
                for handle in (context, browser):
                    if handle is not None:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(handle.close(), 2)
    except Exception as exc:
        return [f"• Playwright/Firefox: проверка не удалась ({type(exc).__name__}); "
                "проверьте установку и запуск Firefox"]


async def startup_network_report(local_ip):
    http, browser = await asyncio.gather(http_report(), browser_report())
    return "\n".join([
        "🤖 Бот запущен. Проверка сети:",
        f"Локальный IP: <code>{escape(local_ip or 'не определён')}</code>",
        http, *browser,
        "В коде этих соединений явный прокси не задан; aiohttp использует trust_env=False.",
        "IP проверен через ipwho.is для каждого транспорта. Системный VPN/NAT может менять маршрут; "
        "маршрут к VSCO/CDN может отличаться от маршрута к сервису проверки.",
    ])
