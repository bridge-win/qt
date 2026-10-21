"""Real Chromium acceptance against the local paper deployment, without token logs."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    url = os.environ.get("QT_AGENT_HUB_UI_URL", "http://127.0.0.1:8878")
    token = os.environ["QT_AGENT_HUB_SERVICE_TOKEN"]
    reports = Path(os.environ.get("QT_AGENT_HUB_UI_REPORTS", "/state/ui-acceptance"))
    reports.mkdir(parents=True, exist_ok=True)
    checks, errors = [], []
    fixture = "ui-check-" + uuid.uuid4().hex[:12]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        api = playwright.request.new_context(
            base_url=url, extra_http_headers={"Authorization": "Bearer " + token}
        )
        status = api.get("/api/status").json()
        assert status["mode"] == "paper", "UI acceptance requires paper mode"
        original_mode = status["trading"]["risk_mode"]
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        try:
            page.goto(url)
            expect(page.locator("#login")).to_be_visible()
            page.locator("#token").fill(token)
            page.locator("#loginForm button").click()
            expect(page.locator("#workspace")).to_be_visible(timeout=30000)
            expect(page.locator("#token")).to_have_value("")
            expect(page.locator("#skills")).to_contain_text("qt-forecast")
            assert page.evaluate("localStorage.length + sessionStorage.length") == 0
            checks.append("authenticated_login_no_persisted_token")

            page.locator("#reduce").click()
            expect(page.locator("#riskMode")).to_contain_text("reduce_only")
            page.locator("#resume").click()
            expect(page.locator("#riskMode")).to_contain_text("active")
            checks.append("risk_mode_roundtrip")

            page.locator("#source").fill("verification:" + fixture)
            page.locator("#content").fill(
                fixture + " deterministic UI evidence; not market information."
            )
            page.locator("#documentForm button").click()
            expect(page.locator("#notice")).to_contain_text("证据已保存")
            page.locator("#query").fill(fixture)
            page.locator("#searchForm button").click()
            expect(page.locator("#searchResult")).to_contain_text(fixture)
            checks.append("document_ingest_and_rag_search")

            page.locator("#memoryKey").fill(fixture)
            page.locator("#memoryValue").fill("temporary acceptance preference")
            page.locator("#memoryForm button").click()
            expect(page.locator("#notice")).to_contain_text("记忆版本已保存")
            page.locator("#memories").click()
            expect(page.locator("#memoryResult")).to_contain_text(fixture)
            assert api.delete("/api/memories/" + fixture).ok
            checks.append("versioned_memory_create_read_delete")

            page.locator("#kind").select_option("monitor")
            page.locator("#question").fill(fixture)
            with page.expect_response(
                lambda response: (
                    response.url.endswith("/api/jobs") and response.request.method == "POST"
                )
            ) as queued:
                page.locator("#jobForm button").click()
            job = queued.value.json()
            for _ in range(30):
                saved = api.get("/api/jobs/" + job["id"]).json()
                if saved["status"] in {"succeeded", "deadletter"}:
                    break
                time.sleep(1)
            assert saved["status"] == "succeeded", "monitor did not complete"
            page.locator("#refresh").click()
            page.locator("#jobs button").filter(has_text="详情").first.click()
            expect(page.locator("#runEvidence")).to_be_visible()
            page.locator("#allTraces").click()
            expect(page.locator("#traces")).to_contain_text("trace_id")
            checks.append("browser_submission_worker_completion_and_trace")

            page.locator("#readiness").click()
            expect(page.locator("#releaseResult")).to_contain_text('"live_ready": false')
            page.locator("#portfolio").click()
            expect(page.locator("#portfolioResult")).to_contain_text("positions")
            checks.append("readiness_and_portfolio")
            page.screenshot(path=str(reports / "hub-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(reports / "hub-mobile.png"), full_page=True)
            overflow = page.evaluate(
                "Array.from(document.querySelectorAll('body *')).filter(e=>e.getBoundingClientRect().right > innerWidth + 2).map(e=>({tag:e.tagName,id:e.id,width:Math.round(e.getBoundingClientRect().width)})).slice(0,15)"
            )
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2"), (
                json.dumps(overflow)
            )
            checks.append("desktop_mobile_layout")
            page.locator("#logout").click()
            expect(page.locator("#login")).to_be_visible()
            checks.append("logout")
            assert not errors, "browser JavaScript errors"
        finally:
            restored = api.post("/api/risk-mode", data={"mode": original_mode})
            assert restored.ok, "risk mode restoration failed"
            api.dispose()
            browser.close()
    report = {
        "passed": True,
        "created_at": time.time(),
        "checks": checks,
        "javascript_errors": errors,
        "real_money_orders_sent": False,
    }
    (reports / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
