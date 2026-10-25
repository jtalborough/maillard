"""Screenshot and overlap check for the roast dashboard at 1024x800."""

from pathlib import Path

import pytest

pytest.importorskip("playwright")

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "src" / "maillard" / "web" / "static" / "index.html"
APP_JS = ROOT / "src" / "maillard" / "web" / "static" / "js" / "app.js"


def _page_html() -> str:
    html = INDEX.read_text()
    html = html.replace('<script src="https://cdn.tailwindcss.com"></script>', "")
    html = html.replace('<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>', "")
    html = html.replace(
        '<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3"></script>',
        "",
    )
    start = html.find("<!-- JS Modules -->")
    if start != -1:
        html = html[:start] + "</body>\n</html>\n"
    app = APP_JS.read_text().replace(
        "document.addEventListener('DOMContentLoaded', () => App.init());",
        "",
    )
    return html.replace("</body>", f"<script>\n{app}\n</script>\n</body>")


@pytest.mark.asyncio
async def test_reasoning_applied_and_banner_do_not_overlap(tmp_path):
    from playwright.async_api import async_playwright

    page_file = tmp_path / "index.html"
    page_file.write_text(_page_html())
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        page = await browser.new_page(viewport={"width": 1024, "height": 800})
        await page.goto(page_file.as_uri())
        await page.evaluate(
            """() => {
              document.body.classList.add('roasting');
              const reason = document.getElementById('ai-reasoning');
              reason.textContent = 'RoR is still climbing after first crack. '.repeat(40);
              const line = document.createElement('div');
              line.id = 'ai-applied-line';
              line.textContent = 'Applied: Heat 65% · Fan 40% · Drum 55%';
              document.getElementById('ai-suggestions').appendChild(line);
              App.showAlert(
                'AI auto control stopped — fell back to manual',
                'critical',
                'ai_auto_fallback',
              );
              App.showAlert('Dry end looks late', 'warning', 'pace-note');
              const pace = App.safetyAlerts.get('pace-note');
              pace.at = Date.now() - 9000;
              App.renderAlerts();
            }"""
        )
        shot = Path("/tmp/roast-layout-1024.png")
        await page.screenshot(path=str(shot))
        metrics = await page.evaluate(
            """() => {
              const reason = document.getElementById('ai-reasoning');
              const line = document.getElementById('ai-applied-line');
              const stack = document.getElementById('alert-stack');
              const header = document.querySelector('header');
              const bt = document.getElementById('val-bt');
              const log = document.getElementById('event-log');
              const row = document.getElementById('control-row');
              const box = (el) => el.getBoundingClientRect();
              const rs = box(reason);
              const ls = box(line);
              const ss = box(stack);
              const hs = box(header);
              const bs = box(bt);
              const hits = (a, b) => !(
                a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top
              );
              const reasonStyle = getComputedStyle(reason);
              const texts = [...stack.querySelectorAll('span')].map((el) => el.textContent);
              return {
                overflowY: reasonStyle.overflowY,
                clamped: reason.scrollHeight > reason.clientHeight + 1,
                lineHeight: ls.height,
                overlap: hits(rs, ls),
                coversHeader: hits(ss, hs),
                coversBt: hits(ss, bs),
                stackPosition: getComputedStyle(stack).position,
                stackTop: ss.top,
                headerBottom: hs.bottom,
                headerHeight: hs.height,
                btTop: bs.top,
                btHeight: bs.height,
                viewport: window.innerHeight,
                closeButtons: stack.querySelectorAll('button').length,
                texts,
                logMin: getComputedStyle(log).minHeight,
                rowMin: parseFloat(getComputedStyle(row).minHeight),
              };
            }"""
        )
        await page.click("#alert-stack button")
        gone = await page.evaluate(
            """() => document.getElementById('alert-stack').classList.contains('hidden')"""
        )
        await browser.close()

    assert metrics["overflowY"] in {"auto", "scroll", "hidden"}
    assert metrics["clamped"] is True
    assert metrics["overlap"] is False
    assert 0 < metrics["lineHeight"] < 32
    assert metrics["coversHeader"] is False
    assert metrics["coversBt"] is False
    assert metrics["stackPosition"] != "fixed"
    assert metrics["headerHeight"] > 40
    assert metrics["btHeight"] > 10
    assert 0 <= metrics["btTop"] < metrics["headerBottom"]
    assert metrics["headerBottom"] <= metrics["viewport"]
    assert metrics["stackTop"] >= metrics["headerBottom"] - 1
    assert metrics["closeButtons"] == 1
    assert metrics["texts"] == ["AI auto control stopped — fell back to manual"]
    assert metrics["rowMin"] >= 300
    assert metrics["logMin"] not in {"0px", "auto"}
    assert gone is True
    assert shot.is_file() and shot.stat().st_size > 1000
