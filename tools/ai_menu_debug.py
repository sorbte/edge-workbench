"""AI 批改菜单排查脚本：用真实课程实测「AI 批改助手」菜单注入与题目提取。

只读调试：只做导航与 DOM 读取，不回填答案、不点击任何提交按钮，不影响课程数据。
用法（需在仓库根目录、用 .venv 的 Python 运行）：

    .venv/Scripts/python.exe tools/ai_menu_debug.py [课程名关键字，默认「马克思主义基本原理」]

步骤：
1. 读取 runtime/plugins/chaoxing_assistant/courses.json，按关键字选课；
2. 用插件独立资料目录启动 Edge（无头），进入课程页；
3. 注入 ai_homework.AI_MENU_SCRIPT，探测菜单节点是否出现、是否可见、按钮是否齐全；
4. 从章节目录打开一个「章节测验」章节，再次注入菜单并探测；
5. 在学习页所有帧跑 QUESTION_EXTRACTION_SCRIPT，输出提取到的题目与题型/只读标记，
   并把第一个 div.TiMu 的 outerHTML 存到 runtime/plugins/chaoxing_assistant/debug_shots/。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROFILE_DIR = ROOT / "edge_profile" / "plugins" / "chaoxing_assistant"
COURSES_FILE = ROOT / "runtime" / "plugins" / "chaoxing_assistant" / "courses.json"
SHOT_DIR = ROOT / "runtime" / "plugins" / "chaoxing_assistant" / "debug_shots"

PLUGIN_DIR = ROOT / "plugins" / "chaoxing_assistant"
sys.path.insert(0, str(PLUGIN_DIR))
from ai_homework import AI_MENU_SCRIPT, QUESTION_EXTRACTION_SCRIPT  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402


CHAPTER_SCRIPT = r"""
els => els.map((el) => {
    const onclick = el.getAttribute("onclick") || "";
    const match = onclick.match(/toOld\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'/);
    const title = String(el.getAttribute("title") || el.innerText || "").replace(/\s+/g, " ").trim();
    return { chapter_id: match ? match[2] : "", title };
})
"""

MENU_PROBE = r"""
() => {
    const root = document.getElementById("cx-ai-menu-root");
    if (!root) return { present: false };
    const rect = root.getBoundingClientRect();
    const style = getComputedStyle(root);
    const head = root.querySelector("#cx-ai-head");
    const headRect = head ? head.getBoundingClientRect() : null;
    return {
        present: true,
        visible: rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden",
        rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
        headRect: headRect ? { x: Math.round(headRect.x), y: Math.round(headRect.y) } : null,
        zIndex: style.zIndex,
        position: style.position,
        buttons: Array.from(root.querySelectorAll("button")).map((node) => ({
            text: node.textContent.trim(),
            disabled: node.disabled,
        })),
        status: (root.querySelector("#cx-ai-status") || {}).textContent || "",
    };
}
"""


def pick_course(keyword: str) -> dict[str, Any] | None:
    payload = json.loads(COURSES_FILE.read_text(encoding="utf-8"))
    courses = [c for c in (payload.get("courses") or []) if isinstance(c, dict) and c.get("course_url")]
    matched = [c for c in courses if keyword in str(c.get("title") or "")]
    return (matched or courses or [None])[0]


# 兜底方案使用：全新临时资料目录 + 从 auth_state.json 读取登录 Cookie
# （工作台正在运行时原资料目录被独占锁定，无法直接启动或整目录复制）
AUTH_FILE = ROOT / "runtime" / "plugins" / "chaoxing_assistant" / "auth_state.json"
PROFILE_COPY_DIR = ROOT / "runtime" / "plugins" / "chaoxing_assistant" / "debug_profile_copy"


def _temp_profile_with_auth() -> tuple[Path, list[dict[str, Any]]]:
    """建一个空的临时资料目录，并读出本机缓存的登录 Cookie 供回灌。"""
    if PROFILE_COPY_DIR.exists():
        shutil.rmtree(PROFILE_COPY_DIR, ignore_errors=True)
    PROFILE_COPY_DIR.mkdir(parents=True, exist_ok=True)
    cookies: list[dict[str, Any]] = []
    if AUTH_FILE.exists():
        payload = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        raw = payload.get("cookies") if isinstance(payload, dict) else None
        for item in raw or []:
            if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                cookies.append(item)
    return PROFILE_COPY_DIR, cookies


def probe(page: Any, label: str) -> None:
    try:
        result = page.evaluate(MENU_PROBE)
    except Exception as exc:
        print(f"[{label}] 菜单探测失败：{exc}")
        return
    print(f"[{label}] 菜单探测：{json.dumps(result, ensure_ascii=False)}")


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else "马克思主义基本原理"
    direct_url = arg if arg.startswith("http") else ""
    course: dict[str, Any] | None = None
    if direct_url:
        print(f"直接打开指定章节：{direct_url[:120]}")
    else:
        course = pick_course(arg)
        if not course:
            print("courses.json 中没有可用课程；请先在工作台「刷新全部数据」。")
            return 1
        print(f"目标课程：{course.get('title')} -> {course.get('course_url')}")
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        auth_cookies: list[dict[str, Any]] = []
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="msedge",
                headless=True,
                viewport={"width": 1680, "height": 950},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                args=["--no-first-run", "--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
        except Exception:
            print("资料目录被占用（工作台/独立 Edge 正在运行），改用临时资料目录 + auth_state.json 登录 Cookie 调试…")
            temp_dir, auth_cookies = _temp_profile_with_auth()
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(temp_dir),
                channel="msedge",
                headless=True,
                viewport={"width": 1680, "height": 950},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                args=["--no-first-run", "--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
        if auth_cookies:
            try:
                context.add_cookies(auth_cookies)
                print(f"已从 auth_state.json 回灌 {len(auth_cookies)} 条登录 Cookie。")
            except Exception as exc:
                print(f"注入登录 Cookie 失败（可能无法访问需登录页面）：{exc}")

        try:
            page = context.pages[0] if context.pages else context.new_page()
            target_url = direct_url or str(course.get("course_url"))
            page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
            if not direct_url:
                page.wait_for_selector("#frame_content-zj", timeout=30_000)
                page.wait_for_timeout(1_500)

                created = page.evaluate(AI_MENU_SCRIPT)
                print(f"[课程页] AI_MENU_SCRIPT 返回：{created}")
                probe(page, "课程页")
                page.screenshot(path=str(SHOT_DIR / "01_course.png"))

                items = page.frame_locator("#frame_content-zj").locator(".chapter_item").evaluate_all(CHAPTER_SCRIPT)
                items = [i for i in (items or []) if i.get("chapter_id")]
                quiz = next((i for i in items if "章节测验" in i.get("title", "")), None) or (items[0] if items else None)
                print(f"目录共 {len(items)} 项；选中章节：{quiz.get('title') if quiz else '无'}")
                if not quiz:
                    return 1

                before_urls = {p.url for p in context.pages}
                page.frame_locator("#frame_content-zj").locator(f"#cur{quiz['chapter_id']}").click(timeout=10_000)
                study_page = page
                deadline = 45.0
                while deadline > 0:
                    for candidate in context.pages:
                        if "studentstudy" in candidate.url and candidate.url not in before_urls or (
                            "studentstudy" in page.url and candidate is page
                        ):
                            study_page = candidate
                            deadline = 0
                            break
                    if deadline > 0:
                        page.wait_for_timeout(1_000)
                        deadline -= 1
            else:
                page.wait_for_selector("#iframe", timeout=30_000)
                study_page = page

            study_page.wait_for_timeout(5_000)
            print(f"[学习页] URL：{study_page.url[:120]}")

            created2 = study_page.evaluate(AI_MENU_SCRIPT)
            print(f"[学习页] AI_MENU_SCRIPT 返回：{created2}")
            probe(study_page, "学习页")
            study_page.screenshot(path=str(SHOT_DIR / "02_study.png"))

            total = 0
            for frame in study_page.frames:
                try:
                    raw_count = frame.evaluate("() => document.querySelectorAll('div.TiMu').length")
                    questions = frame.evaluate(QUESTION_EXTRACTION_SCRIPT)
                except Exception:
                    continue
                print(f"帧 {frame.url[:110]} -> TiMu 原始 {raw_count} 个，提取 {len(questions or [])} 题")
                for question in questions or []:
                    total += 1
                    summary = {
                        "type": question.get("type"),
                        "readonly": question.get("readonly"),
                        "材料": len(str(question.get("material") or "")),
                        "我的答案": question.get("user_answer") or "(空)",
                        "已选": question.get("selected_keys") or "-",
                        "正确答案": question.get("correct_answer") or "-",
                        "选项数": len(question.get("options") or []),
                        "空数": question.get("blank_count") or 0,
                    }
                    stem = str(question.get("stem") or "")[:40]
                    print(f"  题{question.get('mu_index')} {json.dumps(summary, ensure_ascii=False)} | {stem}")
            if not total:
                print("所有帧均未提取到 div.TiMu 题目！")

            saved = 0
            for frame in study_page.frames:
                try:
                    samples = frame.evaluate(
                        "() => Array.from(document.querySelectorAll('div.TiMu')).slice(0, 3)"
                        ".map((m) => m.outerHTML.slice(0, 3500)).join('\\n\\n=====\\n\\n')"
                    )
                except Exception:
                    continue
                if samples:
                    saved += 1
                    (SHOT_DIR / f"timu_sample_{saved}.html").write_text(samples, encoding="utf-8")
                    print(f"已保存 TiMu 结构样例（帧：{frame.url[:80]}）")
        finally:
            context.close()
    print("调试完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
