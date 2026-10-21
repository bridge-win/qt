"""Browser acceptance for the private self-hosted Langfuse trace view."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    if len(sys.argv) != 2 or not re.fullmatch(r"[0-9a-f]{32}", sys.argv[1]):
        raise ValueError("trace_id_required")
    identity = sys.argv[1]
    base = os.environ["QT_LANGFUSE_UI_URL"]
    url = base + "/project/qt-agent-hub/traces/" + identity
    reports = Path(os.environ.get("QT_LANGFUSE_UI_REPORTS", "/state/langfuse-ui-acceptance"))
    reports.mkdir(parents=True, exist_ok=True)
    checks: list[str] = []
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        try:
            page.goto(base + "/auth/sign-in")
            page.locator('input[name="email"]').fill(os.environ["QT_LANGFUSE_USER_EMAIL"])
            page.locator('input[name="password"]').fill(os.environ["QT_LANGFUSE_USER_PASSWORD"])
            page.get_by_role("button", name="Sign in", exact=True).click()
            page.wait_for_url(lambda target: "/auth/" not in target, timeout=30000)
            checks.append("private_owner_login")

            response = page.goto(url)
            assert response and response.ok, "trace_view_response_failed"
            expect(page.get_by_text(identity, exact=True).first).to_be_visible(timeout=30000)
            expect(page.get_by_text("invoke_agent pipeline", exact=True).first).to_be_visible()
            expect(page.get_by_text("invoke_agent research", exact=True).first).to_be_visible()
            expect(page.get_by_text("chat codex.invoke", exact=True).first).to_be_visible()
            expect(page.get_by_text("opportunities.scan", exact=True).first).to_be_visible()
            expect(page.get_by_text("sandbox.backtest", exact=True).first).to_be_visible()
            checks.extend(["exact_trace_id", "agent_model_tool_call_tree"])
            expect(page.locator("body")).to_contain_text(re.compile(r"\d[\d,.]*k? tokens"))
            checks.append("model_token_usage_displayed")
            assert not errors, "browser_javascript_errors"
            checks.append("no_browser_javascript_errors")
            screenshot = reports / "langfuse-trace.png"
            page.screenshot(path=str(screenshot), full_page=True)
        finally:
            browser.close()
    report = {
        "passed": True,
        "created_at": time.time(),
        "trace_id": identity,
        "url": url,
        "checks": checks,
        "javascript_errors": errors,
        "screenshot": str(screenshot),
        "real_money_orders_sent": False,
    }
    (reports / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"passed": False, "error_type": type(exc).__name__}))
        sys.exit(1)
