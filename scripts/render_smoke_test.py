#!/usr/bin/env python3
"""Hourly post-deploy smoke test for the three public BJN instances.

Checks service/worker readiness, searches for more than 10 results, opens the
second result card, and asserts that its identifier matches the detail title.
Any observed HTTP 5xx response also fails the run.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import async_playwright

TARGETS = [
    "https://bjn-buscador.onrender.com",
    "https://alucionacion-buscador-uy.onrender.com",
    "https://bjn-buscador-1.onrender.com",
]
SEARCH_TEXT = "despido abusivo"
RESULT_LIMIT = "20 resultados"
SEARCH_TIMEOUT_MS = 120_000
WORKER_READY_TIMEOUT_SECONDS = 120
WORKER_READY_POLL_SECONDS = 5


@dataclass
class CheckResult:
    url: str
    checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    http_5xx: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "result": "PASS" if not self.errors and not self.http_5xx else "FAIL",
            "checks": self.checks,
            "errors": self.errors,
            "http_5xx": self.http_5xx,
        }


def normalized_id(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def first_sentence_id(text: str) -> str | None:
    """Extract the ID from a card or title like '194/2026 DEFINITIVA ...'."""
    match = re.match(
        r"^\s*([^\s]+)\s+(?:DEFINITIVA|INTERLOCUTORIA)\b",
        text.replace("\n", " "),
        flags=re.IGNORECASE,
    )
    return normalized_id(match.group(1)) if match else None


async def wait_for_worker_ready(page: Any, base_url: str, result: CheckResult) -> bool:
    """Poll after waking the site so a cold start does not create a false failure."""
    deadline = time.monotonic() + WORKER_READY_TIMEOUT_SECONDS
    last_status: int | None = None
    last_data: dict[str, Any] = {}
    saw_5xx = False

    while time.monotonic() < deadline:
        try:
            response = await page.request.get(
                f"{base_url}/api/status", timeout=10_000
            )
            last_status = response.status
            if response.status >= 500:
                item = {"status": response.status, "url": response.url}
                if item not in result.http_5xx:
                    result.http_5xx.append(item)
                saw_5xx = True
            try:
                last_data = await response.json()
            except Exception:
                last_data = {}
            if response.status == 200 and last_data.get("worker_ready") is True:
                result.checks.append("/api/status HTTP 200 y worker_ready=true")
                return True
        except Exception as exc:
            last_data = {"request_error": f"{type(exc).__name__}: {exc}"}

        await asyncio.sleep(WORKER_READY_POLL_SECONDS)

    result.errors.append(
        "El worker no quedó listo dentro de "
        f"{WORKER_READY_TIMEOUT_SECONDS}s; último status HTTP={last_status}, "
        f"respuesta={json.dumps(last_data, ensure_ascii=False)}"
    )
    if saw_5xx:
        result.errors.append("Se observó al menos una respuesta HTTP 5xx al consultar /api/status")
    return False


async def run_one(browser: Any, base_url: str) -> CheckResult:
    result = CheckResult(url=base_url)
    page = await browser.new_page()

    def record_response(response: Any) -> None:
        if response.status >= 500:
            item = {"status": response.status, "url": response.url}
            if item not in result.http_5xx:
                result.http_5xx.append(item)

    page.on("response", record_response)

    try:
        home_response = await page.goto(
            base_url, wait_until="domcontentloaded", timeout=60_000
        )
        if home_response and home_response.status >= 500:
            record_response(home_response)
        if not home_response or home_response.status != 200:
            status = home_response.status if home_response else "sin respuesta"
            result.errors.append(f"La página principal respondió {status}")
            return result

        await page.locator("#s-texto").wait_for(timeout=30_000)
        if not await wait_for_worker_ready(page, base_url, result):
            return result

        await page.locator("#s-texto").fill(SEARCH_TEXT)
        await page.locator("#s-tipo").select_option(label="Frase exacta")
        await page.locator("#s-ordenar").select_option(
            label="Fecha (más reciente)"
        )
        await page.locator("#s-paginas").select_option(label=RESULT_LIMIT)
        await page.get_by_role("button", name="Buscar", exact=True).click(
            timeout=15_000
        )
        await page.wait_for_function(
            "document.body.innerText.includes('resultados encontrados')",
            timeout=SEARCH_TIMEOUT_MS,
        )

        body_text = await page.locator("body").inner_text()
        count_match = re.search(r"(\d+)\s+resultados encontrados", body_text)
        count = int(count_match.group(1)) if count_match else 0
        if count <= 10:
            result.errors.append(
                f"La búsqueda no devolvió más de 10 resultados (conteo={count})"
            )
            return result
        result.checks.append(f"Búsqueda devolvió {count} resultados (>10)")

        cards = await page.locator('[role="button"]').evaluate_all(
            """elements => elements.map((element, index) => ({
                index,
                text: (element.innerText || '').trim()
            })).filter(item => /^\\S+\\s+(?:DEFINITIVA|INTERLOCUTORIA)\\b/i.test(item.text))"""
        )
        if len(cards) < 2:
            result.errors.append(
                f"Se encontraron {len(cards)} tarjetas de sentencia; se requieren al menos 2"
            )
            return result

        second_card = cards[1]
        expected_id = first_sentence_id(second_card["text"])
        if not expected_id:
            result.errors.append(
                f"No se pudo extraer el identificador de la segunda tarjeta: {second_card['text'][:160]}"
            )
            return result
        result.checks.append(f"Segunda tarjeta: {expected_id}")

        await page.locator('[role="button"]').nth(second_card["index"]).click(
            timeout=15_000
        )
        await page.wait_for_function(
            "document.body.innerText.includes('Texto completo')",
            timeout=SEARCH_TIMEOUT_MS,
        )
        detail_body = await page.locator("body").inner_text()
        detail_ids = []
        for line in detail_body.splitlines():
            detail_id = first_sentence_id(line.strip())
            if detail_id:
                detail_ids.append(detail_id)
        if expected_id not in detail_ids:
            result.errors.append(
                f"El detalle no coincide: tarjeta={expected_id}, encabezados_detectados={detail_ids[:5]}"
            )
        else:
            result.checks.append(f"Encabezado del detalle coincide: {expected_id}")
    except Exception as exc:
        result.errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        await page.close()

    return result


async def main() -> int:
    async with async_playwright() as playwright:
        launch_options: dict[str, Any] = {}
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which(
            "chromium"
        )
        if executable:
            launch_options["executable_path"] = executable
            launch_options["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]
        browser = await playwright.chromium.launch(**launch_options)
        results = [await run_one(browser, url) for url in TARGETS]
        await browser.close()

    failed = False
    for result in results:
        data = result.as_dict()
        print(json.dumps(data, ensure_ascii=False))
        if data["result"] != "PASS":
            failed = True

    print("SMOKE TEST: FAIL" if failed else "SMOKE TEST: PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
