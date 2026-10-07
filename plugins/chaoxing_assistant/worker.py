"""学习通助手 worker：同步个人空间用户信息与在读课程（历史课程不采集）。"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from edge_workbench import TaskCancelled, now_text

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
from study_automation import StudyAutomation
from ai_homework import AIHomeworkAutomation


PERSONAL_SPACE_URL = "http://i.mooc.jjxy.zufe.edu.cn/space/index?ws=1"
LOGIN_WAIT_SECONDS = 300
# 学习通登录 Cookie 的归属域；UID/_uid/vc3/cx_p_token/p_auth_token 等关键凭证
# 是会话级（expires=-1），Chromium 关闭浏览器即删除，必须转为持久化才能跨重启保登录。
AUTH_COOKIE_DOMAIN_SUFFIXES = ("zufe.edu.cn", "chaoxing.com")
AUTH_COOKIE_PERSIST_DAYS = 180
AUTH_COOKIE_FIELDS = {"name", "value", "url", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}


def course_is_done(course: dict[str, Any]) -> bool:
    """课程是否已学完：优先看整体进度，缺进度时看任务点/测验是否全部完成。"""
    progress = course.get("overall_progress")
    if isinstance(progress, (int, float)):
        return float(progress) >= 99.9
    checks: list[bool] = []
    for current_key, total_key in (("task_current", "task_total"), ("quiz_current", "quiz_total")):
        current = course.get(current_key)
        total = course.get(total_key)
        if isinstance(current, (int, float)) and isinstance(total, (int, float)):
            checks.append(float(current) >= float(total))
    return bool(checks) and all(checks)


PROFILE_EXTRACTION_SCRIPT = r"""
() => {
    const clean = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
    const nameElement = document.querySelector("#space_nickname p.personalName");
    const name = clean(nameElement?.getAttribute("title") || nameElement?.textContent);
    const accountElement = document.querySelector("#space_nickname a.manageBtn[href]");
    const images = Array.from(document.images || []);
    const avatarElement = images.find((image) => /photo\./i.test(image.src || ""));
    const avatarUrl = avatarElement?.src || "";
    const avatarMatch = avatarUrl.match(/\/(\d{5,})_\d+/);
    const frame = document.querySelector("#frame_content");
    let accountUrl = "";
    let frameUrl = "";
    try {
        accountUrl = accountElement ? new URL(accountElement.getAttribute("href"), location.href).href : "";
    } catch (_error) {
        accountUrl = accountElement?.getAttribute("href") || "";
    }
    try {
        frameUrl = frame ? new URL(frame.getAttribute("src") || "", location.href).href : "";
    } catch (_error) {
        frameUrl = frame?.getAttribute("src") || "";
    }
    return {
        display_name: name,
        user_id: avatarMatch ? avatarMatch[1] : "",
        avatar_url: avatarUrl,
        account_url: accountUrl,
        platform_title: document.title || "",
        frame_url: frameUrl,
    };
}
"""


COURSE_EXTRACTION_SCRIPT = r"""
els => els.map((el) => {
    const clean = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
    const heading = el.querySelector("h3.w_cour_txtH");
    const badge = heading?.querySelector(".w_coulogo");
    const badgeText = clean(badge?.textContent);
    const titleText = clean(heading?.textContent);
    const title = badgeText && titleText.endsWith(badgeText)
        ? titleText.slice(0, -badgeText.length).trim()
        : titleText;
    const paragraphs = Array.from(el.querySelectorAll("p.w_cour_txtP"));
    const assessmentElement = paragraphs.find((node) => clean(node.textContent).includes("考核"));
    const assessment = clean(assessmentElement?.textContent);
    const lineText = paragraphs.map((node) => clean(node.textContent)).join(" ");
    const taskMatch = lineText.match(/章节任务点\s*[：:]\s*(\d+)\s*\/\s*(\d+)/);
    const quizMatch = lineText.match(/章节测验\s*[：:]\s*(\d+)\s*\/\s*(\d+)/);
    const overallText = clean(el.querySelector(".percent")?.textContent);
    const overallMatch = overallText.match(/(\d+(?:\.\d+)?)\s*%/);
    const scoreMatch = lineText.match(/总评成绩\s*[：:]\s*(\d+(?:\.\d+)?)/);
    const termElement = el.closest(".w_main")?.querySelector("h3.w_tabname");
    const term = clean(termElement?.textContent);
    const link = el.querySelector("a.study[onclick]")
        || el.querySelector("h3 a[href]")
        || el.querySelector("a[href]:not([href='javascript:;'])");
    let href = clean(link?.getAttribute("href"));
    if (!href || href.startsWith("javascript")) {
        const onclick = link?.getAttribute("onclick") || "";
        const match = onclick.match(/window\.open\(['"]([^'"]+)/i);
        href = match ? match[1] : "";
    }
    let courseUrl = "";
    let courseId = "";
    try {
        courseUrl = href ? new URL(href, "http://jjxy.zufe.edu.cn").href : "";
        courseId = courseUrl ? (new URL(courseUrl).searchParams.get("xkid") || "") : "";
    } catch (_error) {
        courseUrl = "";
    }
    const cover = el.querySelector("dt img, .courseImg img, img");
    const text = clean(el.textContent);
    return {
        title,
        type: badgeText || "课程",
        term,
        isCurrent: el.classList.contains("onexk"),
        statusText: clean(link?.textContent) || (el.classList.contains("onexk") ? "进入学习" : "回顾课程"),
        courseUrl,
        courseId,
        coverUrl: cover?.src || "",
        assessment,
        taskText: taskMatch ? `${taskMatch[1]}/${taskMatch[2]}` : "",
        taskCurrent: taskMatch ? Number(taskMatch[1]) : null,
        taskTotal: taskMatch ? Number(taskMatch[2]) : null,
        quizText: quizMatch ? `${quizMatch[1]}/${quizMatch[2]}` : "",
        quizCurrent: quizMatch ? Number(quizMatch[1]) : null,
        quizTotal: quizMatch ? Number(quizMatch[2]) : null,
        overallText,
        overallProgress: overallMatch ? Number(overallMatch[1]) : null,
        score: scoreMatch ? Number(scoreMatch[1]) : null,
        rawText: text.slice(0, 2400),
    };
})
"""


class ChaoxingAssistantWorker:
    """把学习通抓取命令挂到宿主的 BrowserWorker 命令分发器上。"""

    def __init__(self, worker: Any) -> None:
        self.worker = worker
        self.runtime_dir = Path(worker.runtime_dir)
        self.profile_file = self.runtime_dir / "profile.json"
        self.courses_file = self.runtime_dir / "courses.json"
        self.auth_file = self.runtime_dir / "auth_state.json"
        self.ai_settings_file = self.runtime_dir / "ai_settings.json"
        self.ai_menu_enabled = self._read_menu_enabled()
        self.study = StudyAutomation(self)
        self.ai_homework = AIHomeworkAutomation(self)

    def install(self) -> None:
        self.worker.command_handlers.update({
            "chaoxing_start": self.start_edge,
            "chaoxing_refresh": self.refresh_all,
            "chaoxing_open_space": self.open_space,
            "chaoxing_open_course": self.open_course,
            "chaoxing_study": self.study.study_incomplete,
            "chaoxing_ai_menu": self.set_ai_menu,
        })
        # 批改菜单注入与页面命令轮询挂在宿主空闲钩子上：不占用任务闸门，
        # 与「开始学习」「刷新全部数据」等命令互不阻塞（见 docs/commands-and-events.md）
        self.worker.idle_pollers.append(self.ai_homework.service_menu)

    def _read_menu_enabled(self) -> bool:
        try:
            payload = json.loads(self.ai_settings_file.read_text(encoding="utf-8"))
            return bool(payload.get("menu_enabled")) if isinstance(payload, dict) else False
        except (OSError, ValueError):
            return False

    def ai_settings_agent_id(self) -> str:
        """批改用的 Agent id（「学习通设置」页选择并保存）。"""
        try:
            payload = json.loads(self.ai_settings_file.read_text(encoding="utf-8"))
            return str(payload.get("agent_id") or "") if isinstance(payload, dict) else ""
        except (OSError, ValueError):
            return ""

    def set_ai_menu(self, enabled: bool = False, **_payload: Any) -> None:
        """工作台工具栏「批改作业」开关：开启/关闭页面批改菜单。"""
        self.ai_homework.set_menu_enabled(bool(enabled))

    def _emit(self, event_type: str, **payload: Any) -> None:
        self.worker.emit(event_type, **payload)

    def _check_cancelled(self) -> None:
        if self.worker._cancel_event.is_set():
            raise TaskCancelled()

    @staticmethod
    def _clean(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _number(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _capture_auth_state(self, page: Any) -> int:
        """把当前独立 Edge 的登录 Cookie 与页面存储快照写入本机缓存。"""
        cookies: list[dict[str, Any]] = []
        try:
            cookies = [
                item for item in self.worker.context.cookies()
                if self._is_auth_cookie(item)
            ]
        except Exception as exc:
            self.worker.log(f"读取登录 Cookie 失败：{exc}", "warning")

        local_storage: dict[str, dict[str, str]] = {}
        session_storage: dict[str, dict[str, str]] = {}
        for frame in getattr(page, "frames", []):
            try:
                parsed = urlsplit(frame.url or "")
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    continue
                origin = f"{parsed.scheme}://{parsed.netloc}"
                local = frame.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
                session = frame.evaluate("() => Object.fromEntries(Object.entries(sessionStorage))")
                if isinstance(local, dict) and local:
                    local_storage[origin] = {str(key): str(value) for key, value in local.items()}
                if isinstance(session, dict) and session:
                    session_storage[origin] = {str(key): str(value) for key, value in session.items()}
            except Exception:
                continue

        payload = {
            "saved_at": now_text(),
            "source_url": page.url,
            "cookies": cookies,
            "local_storage": local_storage,
            "session_storage": session_storage,
            "note": "本文件包含学习通登录 Cookie 快照，仅保存在本机插件 runtime 目录，请勿分享。",
        }
        self._write_json(self.auth_file, payload)
        self._persist_auth_cookies(cookies)
        self.worker.log(f"已缓存学习通登录 Cookie {len(cookies)} 条及页面存储快照。", "success")
        return len(cookies)

    def _is_auth_cookie(self, item: Any) -> bool:
        """是否属于学习通登录体系的 Cookie（学校平台与超星主站）。"""
        return (
            isinstance(item, dict)
            and bool(item.get("name"))
            and str(item.get("domain") or "").lstrip(".").endswith(AUTH_COOKIE_DOMAIN_SUFFIXES)
        )

    def _persistent_auth_cookie(self, item: dict[str, Any]) -> dict[str, Any]:
        """复制 Cookie 并把会话级/已过期的有效期改为长期，使其能写入资料目录。"""
        cookie = {key: value for key, value in item.items() if key in AUTH_COOKIE_FIELDS}
        try:
            expires = float(cookie.get("expires") or -1)
        except (TypeError, ValueError):
            expires = -1.0
        if expires <= 0 or expires < time.time():
            cookie["expires"] = time.time() + AUTH_COOKIE_PERSIST_DAYS * 86400
        return cookie

    def _persist_auth_cookies(self, cookies: list[dict[str, Any]]) -> int:
        """把独立 Edge 中的会话级登录 Cookie 就地转为持久化。

        学习通登录的关键凭证（UID/_uid/vc3/cx_p_token/p_auth_token 等）是会话级
        Cookie（expires=-1），Chromium 关闭浏览器即从资料目录删除；改写有效期后
        Cookie 落盘，重启浏览器仍保持登录。
        """
        if self.worker.context is None or not cookies:
            return 0
        rewrites: list[dict[str, Any]] = []
        for item in cookies:
            try:
                expires = float(item.get("expires") or -1)
            except (TypeError, ValueError):
                expires = -1.0
            if expires > 0 and expires >= time.time():
                continue
            rewrites.append(self._persistent_auth_cookie(item))
        if not rewrites:
            return 0
        try:
            self.worker.context.add_cookies(rewrites)
        except Exception as exc:
            self.worker.log(f"持久化学习通登录 Cookie 失败：{exc}", "warning")
            return 0
        self.worker.log(f"已将 {len(rewrites)} 条会话级登录 Cookie 转为持久化，关闭浏览器后仍可保持登录。", "success")
        return len(rewrites)

    def _restore_auth_state(self) -> None:
        """用本机 Cookie 快照补回资料目录里缺失的登录状态。

        学习通登录的关键 Cookie 是会话级，浏览器关闭即被删除，资料目录里往往
        只剩 fid/xxtenc 等少数长期 Cookie；因此不能「已有 Cookie 就跳过恢复」，
        而要按 名称+域名+路径 逐条比对，缺哪条补哪条，并顺带转为持久化。
        """
        if not self.auth_file.exists() or self.worker.context is None:
            return
        try:
            payload = json.loads(self.auth_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        snapshot = payload.get("cookies") if isinstance(payload, dict) else None
        if not isinstance(snapshot, list) or not snapshot:
            return
        try:
            existing_keys = {
                (item.get("name"), item.get("domain"), item.get("path"))
                for item in self.worker.context.cookies()
                if self._is_auth_cookie(item)
            }
        except Exception:
            existing_keys = set()
        missing = [
            self._persistent_auth_cookie(item)
            for item in snapshot
            if self._is_auth_cookie(item)
            and item.get("value") is not None
            and (item.get("name"), item.get("domain"), item.get("path")) not in existing_keys
        ]
        if not missing:
            self.worker.log("独立 Edge 登录 Cookie 完整，无需从本机快照恢复。", "info")
            return
        try:
            self.worker.context.add_cookies(missing)
            self.worker.log(
                f"已从本机快照补回学习通登录 Cookie {len(missing)} 条（会话 Cookie 已持久化，重启浏览器不掉线）。",
                "success",
            )
        except Exception as exc:
            self.worker.log(f"恢复学习通登录 Cookie 失败，将继续使用持久资料目录：{exc}", "warning")

    def _has_profile(self, page: Any) -> bool:
        try:
            return page.locator("#space_nickname p.personalName").count() > 0
        except Exception:
            return False

    def _open_personal_space(self) -> Any:
        page = self.worker._current_page()
        self.worker._goto_page(page, PERSONAL_SPACE_URL, wait_network_idle=True)
        return page

    def _wait_for_login(self, page: Any) -> bool:
        if self._has_profile(page):
            return True

        if bool(getattr(self.worker, "headless", False)):
            message = "独立 Edge 当前处于后台隐藏模式，无法扫描二维码登录；请在显示模式下重新同步。"
            self.worker.log(message, "warning")
            self._emit("chaoxing_run_status", state="error", message=message, current=0, total=4)
            return False

        self._emit(
            "chaoxing_run_status",
            state="login_required",
            message="请在弹出的独立 Edge 窗口中使用“学习通”App 扫描二维码登录；登录成功后会自动缓存 Cookies 并继续同步。",
            login_url=page.url,
            current=0,
            total=4,
        )
        self.worker.log("学习通个人空间需要登录，请使用学习通 App 扫描二维码登录…", "warning")
        deadline = time.monotonic() + LOGIN_WAIT_SECONDS
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                page.wait_for_timeout(1500)
            except Exception:
                self._emit("chaoxing_run_status", state="error", message="独立 Edge 已关闭，无法继续同步。", current=0, total=4)
                return False
            if self._has_profile(page):
                self.worker.log("已检测到学习通登录状态，继续同步。", "success")
                self.worker.spider_celebrate(page)
                return True

        self._emit(
            "chaoxing_run_status",
            state="error",
            message="等待登录超时。请在独立 Edge 中登录后重新点击「刷新全部数据」。",
            current=0,
            total=4,
        )
        self.worker.log("等待学习通登录超时。", "warning")
        return False

    def _extract_profile(self, page: Any) -> dict[str, Any]:
        # 蜘蛛特效联动：读取资料前先爬到昵称上接触采集（特效关闭时静默跳过）
        self.worker.spider_fetch_element(page, selector="#space_nickname p.personalName", padding=10)
        payload = page.evaluate(PROFILE_EXTRACTION_SCRIPT)
        if not isinstance(payload, dict):
            payload = {}
        payload["fetched_at"] = now_text()
        payload["source_url"] = page.url
        return payload

    def _extract_courses(self, page: Any) -> list[dict[str, Any]]:
        frame = page.frame_locator("#frame_content")
        try:
            frame.locator("dl.w_cour_row").first.wait_for(state="visible", timeout=20_000)
        except Exception as exc:
            raise RuntimeError("未找到课程列表 iframe，请确认已进入学习通个人空间首页。") from exc

        # 蜘蛛特效联动：读取课程前先爬到课程列表区域接触采集
        self.worker.spider_fetch_element(page, selector="#frame_content", padding=4)

        items = frame.locator("dl.w_cour_row").evaluate_all(COURSE_EXTRACTION_SCRIPT)
        courses: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            title = self._clean(item.get("title"))
            if not title:
                continue
            # 只采集在读课程（onexk）；历史课程（已结课）一律丢弃
            if not item.get("isCurrent"):
                continue
            courses.append({
                "title": title,
                "type": self._clean(item.get("type")) or "课程",
                "term": self._clean(item.get("term")) or "未标注学期",
                "is_current": True,
                "status": "进行中",
                "course_url": self._clean(item.get("courseUrl")),
                "course_id": self._clean(item.get("courseId")),
                "cover_url": self._clean(item.get("coverUrl")),
                "assessment": self._clean(item.get("assessment")),
                "task_text": self._clean(item.get("taskText")),
                "task_current": self._number(item.get("taskCurrent")),
                "task_total": self._number(item.get("taskTotal")),
                "quiz_text": self._clean(item.get("quizText")),
                "quiz_current": self._number(item.get("quizCurrent")),
                "quiz_total": self._number(item.get("quizTotal")),
                "overall_text": self._clean(item.get("overallText")),
                "overall_progress": self._number(item.get("overallProgress")),
                "score": self._number(item.get("score")),
                "action_text": self._clean(item.get("statusText")) or "进入学习",
                "raw_text": self._clean(item.get("rawText")),
            })
        return courses

    @staticmethod
    def _summarize(courses: list[dict[str, Any]]) -> dict[str, Any]:
        progress_values = [
            float(course["overall_progress"])
            for course in courses
            if course.get("overall_progress") is not None
        ]
        completed = sum(1 for course in courses if course_is_done(course))
        return {
            "total": len(courses),
            "current": len(courses),
            "history": 0,
            "completed": completed,
            "unfinished": len(courses) - completed,
            "average_progress": round(sum(progress_values) / len(progress_values), 1) if progress_values else None,
        }

    def refresh_all(self, **_payload: Any) -> None:
        """启动浏览器、等待登录、抓取个人资料与学历课程，并写入缓存。"""
        try:
            self._check_cancelled()
            self._emit("chaoxing_run_status", state="running", message="正在启动独立 Edge…", current=0, total=4)
            self.worker.start_browser()
            self._restore_auth_state()

            self._check_cancelled()
            self._emit("chaoxing_run_status", state="running", message="正在打开学习通个人空间…", current=1, total=4)
            page = self._open_personal_space()
            if not self._wait_for_login(page):
                return

            self._check_cancelled()
            cookie_count = self._capture_auth_state(page)
            self._emit("chaoxing_profile_status", state="loading", message=f"登录成功，已缓存 {cookie_count} 条 Cookie；正在读取个人空间基本信息…")
            profile = self._extract_profile(page)
            self._write_json(self.profile_file, profile)
            self._emit("chaoxing_profile_data", data=profile)
            self._emit("chaoxing_profile_status", state="done", message="基本信息已缓存。")

            self._check_cancelled()
            self._emit("chaoxing_run_status", state="running", message="正在读取在读课程…", current=2, total=4)
            courses = self._extract_courses(page)
            self._capture_auth_state(page)
            stats = self._summarize(courses)
            payload = {
                "fetched_at": now_text(),
                "source_url": page.url,
                "profile": profile,
                "courses": courses,
                "stats": stats,
            }
            self._write_json(self.courses_file, payload)
            self._emit("chaoxing_courses_data", data=payload)
            self.worker.spider_celebrate(page)
            self._emit(
                "chaoxing_run_status",
                state="done",
                message=f"同步完成：用户 {profile.get('display_name') or '未知'}，在读课程 {stats['total']} 门；登录 Cookies 已缓存。",
                current=4,
                total=4,
            )
            self.worker.log(
                f"学习通同步完成：基本信息已缓存，在读课程 {stats['total']} 门"
                f"（已完成 {stats['completed']}，未完成 {stats['unfinished']}）；历史课程不采集。",
                "success",
            )
        except TaskCancelled:
            self._emit("chaoxing_run_status", state="cancelled", message="同步已取消。", current=0, total=4)
            self.worker.log("学习通同步已取消。", "warning")
        except Exception as exc:
            message = f"学习通同步失败：{exc}"
            self.worker.log(message, "error")
            self._emit("chaoxing_run_status", state="error", message=message, current=0, total=4)

    def start_edge(self, **_payload: Any) -> None:
        """仅启动本插件的独立 Edge（不执行同步/学习任务），供页头「▶ 启动」使用。"""
        try:
            self.worker.start_browser()
            self._restore_auth_state()
            self._emit(
                "chaoxing_run_status",
                state="opened",
                message="独立 Edge 已启动；可点击右上角「刷新全部数据」图标读取课程，或在课程卡片中点击「开始学习」。",
                current=0,
                total=0,
            )
        except Exception as exc:
            message = f"启动独立 Edge 失败：{exc}"
            self.worker.log(message, "error")
            self._emit("chaoxing_run_status", state="error", message=message, current=0, total=0)

    def open_space(self, **_payload: Any) -> None:
        """打开独立 Edge 中的学习通个人空间，便于用户登录或查看。"""
        try:
            self.worker.start_browser()
            self._restore_auth_state()
            page = self._open_personal_space()
            if self._has_profile(page):
                # 已登录则顺手刷新 Cookie 快照，保证本地缓存与最新会话一致
                self._capture_auth_state(page)
                message = f"已打开个人空间：{page.url}"
            else:
                message = "已打开学习通登录页，请使用“学习通”App 扫描二维码登录。"
            self._emit("chaoxing_run_status", state="opened", message=message, current=0, total=0)
        except Exception as exc:
            message = f"打开学习通个人空间失败：{exc}"
            self.worker.log(message, "error")
            self._emit("chaoxing_run_status", state="error", message=message, current=0, total=0)

    def open_course(self, url: str = "", **_payload: Any) -> None:
        """在独立 Edge 中新标签页打开课程。"""
        target = self._clean(url)
        if not target:
            self._emit("chaoxing_run_status", state="error", message="该课程没有可用的进入地址。", current=0, total=0)
            return
        try:
            self.worker.start_browser()
            self._restore_auth_state()
            self.worker.new_tab(target)
            self._emit("chaoxing_run_status", state="opened", message="已在独立 Edge 中打开课程。", current=0, total=0)
        except Exception as exc:
            message = f"打开课程失败：{exc}"
            self.worker.log(message, "error")
            self._emit("chaoxing_run_status", state="error", message=message, current=0, total=0)


def register(worker: Any) -> None:
    """宿主在插件子进程中调用此函数安装命令。"""
    ChaoxingAssistantWorker(worker).install()
