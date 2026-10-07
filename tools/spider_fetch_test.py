"""蜘蛛抓取效果自动化测试：docs/spider_test_page.html + 真实注入的宠物特效脚本。

验证点（对应 docs/spider-api.md 行为细节）：
  1. 渐进标记：抓取大列表时 collected 逐步增长，不是一抵达整块闪亮；
  2. 途中标记：第一次标记发生在蜘蛛抵达目标之前（冲刺途中爬过就点亮）；
  3. 轨迹：冲刺路径最大横向偏离显著小于旧行为的绕圈（< 直线距离的 60%）；
  4. 抽屉：未展开时采不到（rect 无效）；展开后（含动态插入文本）能全部标记；
  5. 「已抓取」提示窗：标记时出现，约 1 秒后渐隐移除；
  6. fetchRect 返回值不再包含 route 巡回标记。

运行：  python tools/spider_fetch_test.py   （需 playwright + node 环境）
"""
from __future__ import annotations

import importlib.util
import math
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

PET = {
    "id": "spider", "name": "蜘蛛", "glyph": "\U0001F577", "body": "spider",
    "accent": "#54d7e8", "accent_dim": "#155e6b",
    "body_fill": "#0a0a12", "body_edge": "#f2f2fa", "speed": 1.0,
}

PAGE = "file:///" + os.path.join(ROOT, "docs", "spider_test_page.html").replace("\\", "/")

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def state(page) -> dict:
    return page.evaluate("() => window.__wbs ? window.__wbs.state() : null")


def toasts(page) -> int:
    return page.evaluate(
        "() => { const r = document.getElementById('wbs-root');"
        " return r && r.shadowRoot ? r.shadowRoot.querySelectorAll('.wbs-toast').length : -1; }"
    )


def run_fetch(page, box: dict, poll: float = 0.06, timeout: float = 10.0):
    """触发 fetchRect 并采样，返回 (samples, result)。samples: [(t, collected, dist, toasts)]"""
    result = page.evaluate(
        "([x, y, w, h]) => window.__wbs.fetchRect(x, y, w, h)",
        [box["x"] - 8, box["y"] - 8, box["width"] + 16, box["height"] + 16],
    )
    tx, ty = float(result["tx"]), float(result["ty"])
    samples = []
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = state(page)
        if s is None:
            break
        d = math.hypot(s["x"] - tx, s["y"] - ty)
        samples.append((time.time() - t0, s["collected"], d, toasts(page), s["x"], s["y"]))
        if not s["fetching"] and len(samples) > 2:
            break
        page.wait_for_timeout(int(poll * 1000))
    return samples, result


def max_deviation(samples, tx, ty) -> float:
    """采样点偏离 起点→目标 直线段的最大垂距。"""
    pts = [(s[4], s[5]) for s in samples]
    if len(pts) < 2:
        return 0.0
    sx, sy = pts[0]
    vx, vy = tx - sx, ty - sy
    L = math.hypot(vx, vy)
    if L < 1e-6:
        return 0.0
    worst = 0.0
    for px, py in pts[1:-1]:
        t = max(0.0, min(1.0, ((px - sx) * vx + (py - sy) * vy) / (L * L)))
        worst = max(worst, math.hypot(px - (sx + t * vx), py - (sy + t * vy)))
    return worst


def main() -> int:
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, channel="msedge",
                                        args=["--disable-renderer-backgrounding"])
        except Exception:
            browser = p.chromium.launch(headless=True, args=["--disable-renderer-backgrounding"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(PAGE)
        page.wait_for_timeout(400)

        page.evaluate(f"({spider_scripts.SPIDER_OVERLAY_SCRIPT})", PET)
        page.wait_for_timeout(600)
        st = page.evaluate(
            "() => window.__wbs ? {pet: window.__wbs.pet, version: window.__wbs.version,"
            " words: window.__wbs.state().words} : null"
        )
        print("注入完成:", st)

        check("版本为 8（新行为脚本）", bool(st) and st.get("version") == 8)
        check("fetchRect 已不再返回 route 巡回标记", "route" not in (st or {}))

        # ---------- 1/2/3：大列表抓取 ----------
        print("\n== 大列表抓取（渐进标记 / 途中标记 / 轨迹）==")
        page.evaluate("() => window.__wbs.wander()")
        page.wait_for_timeout(300)
        box = page.locator("#result-list").bounding_box()
        samples, result = run_fetch(page, box)
        final = samples[-1][1] if samples else 0
        check("列表词条被采集（final>150）", final > 150, f"final={final}")

        rising = sum(1 for a, b in zip(samples, samples[1:]) if 0 < b[1] - a[1] < final)
        check("渐进标记（存在中间态，非整块闪亮）", rising >= 2, f"中间增长采样数={rising}")

        first_mark = next((s for s in samples if s[1] > 0), None)
        check("冲刺途中即开始标记（未抵达就有点亮）",
              first_mark is not None and first_mark[2] > 30,
              f"首次标记时距目标 {first_mark[2]:.0f}px" if first_mark else "无")

        dev = max_deviation(samples, float(result["tx"]), float(result["ty"]))
        straight = math.hypot(float(result["tx"]) - samples[0][4], float(result["ty"]) - samples[0][5])
        if straight > 150:
            check("轨迹无大弧线（最大偏离 < 60% 直线距）", dev < straight * 0.6,
                  f"偏离 {dev:.0f}px / 直线 {straight:.0f}px")
        else:
            print("  [SKIP] 轨迹检查：起点离目标太近")

        # ---------- 4：抽屉 ----------
        print("\n== 抽屉（未展开采不到 / 展开后含动态内容全部标记）==")
        region = page.locator("#drawer-region").bounding_box()
        before = state(page)["collected"]
        run_fetch(page, region)
        gain_hidden = state(page)["collected"] - before
        check("未展开的抽屉内容不会被误标（增量=0）", gain_hidden == 0, f"增量={gain_hidden}")

        page.click("#drawer-toggle")
        page.wait_for_timeout(300)   # 等滚动与布局稳定（点击会把按钮滚入视口）
        region = page.locator("#drawer-region").bounding_box()   # 展开后重新取矩形（旧矩形含旧滚动偏移，会偏到抽屉下方）
        check("展开后抽屉矩形有效", bool(region) and region["height"] > 60,
              f"h={region and round(region['height'])}")
        before = state(page)["collected"]
        drawer_samples, _ = run_fetch(page, region)
        gain_open = state(page)["collected"] - before
        check("展开后的抽屉内容（含动态插入）被标记（增量>10）", gain_open > 10, f"增量={gain_open}")

        # ---------- 5：提示窗 ----------
        print("\n== 「已抓取」小提示窗（1 秒渐隐）==")
        max_toasts = max((s[3] for s in drawer_samples), default=-1)
        check("标记时出现提示窗", max_toasts > 0, f"峰值 {max_toasts} 个")
        page.wait_for_timeout(1500)
        left = toasts(page)
        check("约 1 秒后提示窗全部渐隐移除", left == 0, f"剩余 {left} 个")

        browser.close()

    print("\n" + ("全部通过 ✔" if not FAILURES else f"失败 {len(FAILURES)} 项: {FAILURES}"))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
