"""抽屉抓取失败的探针：打开抽屉 → fetchRect → 转储高亮范围与几何信息。"""
from __future__ import annotations

import importlib.util
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from playwright.sync_api import sync_playwright  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "spider_scripts", os.path.join(ROOT, "workbench", "spider_scripts.py")
)
spider_scripts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(spider_scripts)

PET = {"id": "spider", "body": "spider", "accent": "#54d7e8", "accent_dim": "#155e6b",
       "body_fill": "#0a0a12", "body_edge": "#f2f2fa", "speed": 1.0}
PAGE = "file:///" + os.path.join(ROOT, "docs", "spider_test_page.html").replace("\\", "/")

PROBE = """
() => {
  const hl = CSS.highlights.get('wbs-crawl');
  const region = document.getElementById('drawer-region').getBoundingClientRect();
  let inRegion = 0, samples = [];
  if (hl) for (const r of hl) {
    const b = r.getBoundingClientRect();
    if (!b.width || !b.height) continue;
    if (b.bottom >= region.top - 4 && b.top <= region.bottom + 4) {
      inRegion++;
      if (samples.length < 4) samples.push([Math.round(b.left), Math.round(b.top + scrollY)]);
    }
  }
  const w = window.__wbs ? window.__wbs.state() : null;
  return { hlSize: hl ? hl.size : -1, inRegion, samples,
           regionTopPage: Math.round(region.top + scrollY), scrollY: Math.round(scrollY),
           collected: w ? w.collected : null, fetching: w ? w.fetching : null,
           px: w ? Math.round(w.px) : null, py: w ? Math.round(w.py) : null };
}
"""


def main() -> int:
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, channel="msedge")
        except Exception:
            browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(PAGE)
        page.wait_for_timeout(400)
        page.evaluate(f"({spider_scripts.SPIDER_OVERLAY_SCRIPT})", PET)
        page.wait_for_timeout(500)

        print("打开抽屉前:", page.evaluate(PROBE))
        page.click("#drawer-toggle")
        page.wait_for_timeout(200)
        box = page.locator("#drawer-region").bounding_box()
        print("region box:", {k: round(v) for k, v in box.items()})
        r = page.evaluate(
            "([x, y, wd, h]) => window.__wbs.fetchRect(x, y, wd, h)",
            [box["x"] - 8, box["y"] - 8, box["width"] + 16, box["height"] + 16],
        )
        print("fetchRect 返回:", {k: (round(v) if isinstance(v, (int, float)) else v) for k, v in r.items() if k in ("tx", "ty", "collected", "fetching")})
        for i in range(30):
            time.sleep(0.15)
            s = page.evaluate(PROBE)
            print(f"t={i * 0.15:.2f}s inRegion={s['inRegion']} hlSize={s['hlSize']} "
                  f"collected={s['collected']} fetching={s['fetching']} "
                  f"pet=({s['px']},{s['py']}) scrollY={s['scrollY']} regionTop={s['regionTopPage']}")
            if not s["fetching"] and i > 2:
                break
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
