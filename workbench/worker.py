"""浏览器工作线程：Playwright 驱动 Edge 完成导航、搜索、Rewards 与宠物联动。"""
from __future__ import annotations

import json
import math
import queue
import random
import re
import shutil
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlsplit

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .config import (
    ALLOWED_POINTS_SYNC_HOSTS,
    DOWNLOAD_DIR,
    HOME_URL,
    OPERATIONS_LOG,
    PETS,
    PROFILE_DIR,
    PageSnapshot,
    REWARDS_ABOUT_URL,
    REWARDS_DASHBOARD_URL,
    REWARDS_EARN_URL,
    REWARDS_USERINFO_URL,
    RUNTIME_DIR,
    SAFE_CACHE_RELATIVE_PATHS,
    SEARCH_CALIBRATION_EVERY,
    SEARCH_COOLDOWN_SECONDS,
    SEARCH_INTERVAL_MAX_SECONDS,
    SEARCH_INTERVAL_MIN_SECONDS,
    SEARCH_MAX_COOLDOWNS,
    SEARCH_NEWS_EXTRA,
    SEARCH_NO_INCREASE_LIMIT,
    SEARCH_POINTS_PER_ACTION,
    SEARCH_TASK_MAX_ACTIONS,
    TOUTIAO_URL,
    append_operation,
    count_cookies_database,
    count_history_entries,
    find_edge_executable,
    format_bytes,
    load_active_pet,
    load_workbench_settings,
    normalize_url,
    now_text,
    profile_breakdown,
    save_workbench_settings,
)
from .page_scripts import (
    REWARDS_ACCOUNT_EXTRACTION_SCRIPT,
    REWARDS_EXTRACTION_SCRIPT,
    REWARDS_TASK_CLICK_SCRIPT,
)
from .spider_scripts import (
    SPIDER_COLLECTOR_DESTROY,
    SPIDER_COLLECTOR_HARVEST,
    SPIDER_OVERLAY_SCRIPT,
)


class TaskCancelled(Exception):
    pass


class BrowserWorker(threading.Thread):
    """Owns Playwright inside one thread so Tkinter remains responsive."""

    def __init__(
        self,
        event_queue: Any,
        *,
        commands: Any | None = None,
        name: str = "edge-browser-worker",
        profile_dir: Path | None = None,
        download_dir: Path | None = None,
        runtime_dir: Path | None = None,
        operations_log: Path | None = None,
        cancel_event: Any | None = None,
        namespace: str | None = None,
    ) -> None:
        super().__init__(name=name, daemon=True)
        self.events = event_queue
        self.commands = commands if commands is not None else queue.Queue()
        self.context = None
        self.playwright = None
        self.bound_pages: set[int] = set()
        self._shutting_down = False
        self._context_closing = False
        self._cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self._search_running = False
        self._auto_running = False
        self._active_flow: str | None = None
        self.headless = False
        self._account_rules: dict[str, Any] = {}
        self._header_dump_done = False
        self.current_command: str | None = None
        self.namespace = namespace
        self.command_handlers: dict[str, Any] = {}
        # 插件可注册的空闲轮询钩子：命令队列空闲时执行（如学习通批改菜单的注入与命令轮询）。
        # 轮询实现必须自带节流与异常兜底；长耗时轮询会阻塞命令处理（与命令串行的语义一致）
        self.idle_pollers: list[Any] = []
        self.profile_dir = Path(profile_dir) if profile_dir is not None else PROFILE_DIR
        self.download_dir = Path(download_dir) if download_dir is not None else DOWNLOAD_DIR
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else RUNTIME_DIR
        self.state_dir = self.runtime_dir / "state_snapshots"
        self.operations_log = Path(operations_log) if operations_log is not None else OPERATIONS_LOG
        self.rewards_data_file = self.runtime_dir / "rewards_data.json"
        self.rewards_account_file = self.runtime_dir / "rewards_account.json"
        self.search_task_log = self.runtime_dir / "search_task.jsonl"
        self.news_cache_file = self.runtime_dir / "news_cache.json"
        self.test_click_screenshot = self.runtime_dir / "test_click.png"
        # 操作可视化（蜘蛛联动特效）对所有工作进程生效：纯视觉、无 UI 挂件、不滚动页面，
        # 插件自动化可在动作时刻调用 spider_* 公共方法触发联动
        self.spider_overlay_enabled = bool(load_workbench_settings().get("spider_overlay"))
        self._spider_pending: Any | None = None
        self._spider_pending_at = 0.0

    def _append_operation(self, entry: dict[str, Any]) -> None:
        append_operation(entry, self.operations_log)

    def submit(self, command: str, **payload: Any) -> None:
        self.commands.put((command, payload))

    def emit(self, event_type: str, **payload: Any) -> None:
        if event_type == "search_task_status":
            payload["flow"] = self._active_flow or "search"
        if self.namespace:
            payload.setdefault("plugin_id", self.namespace)
        self.events.put({"type": event_type, **payload})

    def log(self, message: str, level: str = "info") -> None:
        self.emit("log", level=level, message=message)
        self._append_operation({"event": "log", "level": level, "message": message})

    def run(self) -> None:
        while not self._shutting_down:
            try:
                command, payload = self.commands.get(timeout=0.2)
            except queue.Empty:
                self._service_spider_injection()
                self._service_idle_pollers()
                continue

            try:
                self.current_command = command
                if "headless" in payload:
                    self.headless = bool(payload.get("headless"))
                if command == "start":
                    self.start_browser()
                elif command == "navigate":
                    self.navigate(payload.get("url", HOME_URL))
                elif command == "bing_search":
                    self.bing_search(payload.get("query", ""))
                elif command == "new_tab":
                    self.new_tab(payload.get("url", HOME_URL))
                elif command == "simulate":
                    self.simulate_real_browsing()
                elif command == "rewards_data":
                    self.fetch_rewards_all()
                elif command == "test_click":
                    self.test_click()
                elif command == "search_task":
                    self.run_search_task()
                elif command == "auto_run":
                    self.auto_run()
                elif command == "trigger_tasks":
                    self.trigger_incomplete_tasks()
                elif command == "account_info":
                    self.fetch_rewards_all()
                elif command == "stats":
                    self.publish_stats()
                elif command == "snapshot":
                    self.save_state_snapshot()
                elif command == "stop":
                    self.stop_browser()
                elif command == "restart":
                    self.restart_browser()
                elif command == "clean_cache":
                    self.clean_browser_cache()
                elif command == "spider_overlay":
                    self._set_spider_overlay(bool(payload.get("enabled")))
                elif command == "spider_pet":
                    self._set_active_pet(str(payload.get("pet") or ""))
                elif command == "pet_play":
                    self._pet_play(str(payload.get("action") or ""))
                elif command == "shutdown":
                    self._shutting_down = True
                    self.stop_browser()
                else:
                    handler = self.command_handlers.get(command)
                    if handler is None:
                        self.log(f"Unknown command: {command}", "warning")
                    else:
                        handler(**payload)
            except Exception as exc:
                self.log(f"操作失败: {exc}", "error")
                self.emit("log", level="error", message=traceback.format_exc(limit=5))
            finally:
                self.current_command = None
            self.emit("command_done", command=command)

        self.stop_browser()

    def start_browser(self) -> None:
        if self.context is not None:
            self.emit("status", state="running", detail="Microsoft Edge 已运行")
            self.log("Microsoft Edge 已经在运行，无需重复启动。", "info")
            return

        edge = find_edge_executable()
        if edge is None:
            raise RuntimeError("没有找到本机 Microsoft Edge，请先安装 Edge。")

        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

        self.emit("status", state="starting", detail="正在启动 Microsoft Edge…")
        self.log(f"使用 Edge: {edge}{'（隐藏模式，浏览器在后台运行）' if self.headless else ''}")
        self.log(f"持久化资料目录: {self.profile_dir}")
        self._append_operation({"event": "browser_start", "edge": str(edge), "profile": str(self.profile_dir), "headless": self.headless})

        self.playwright = sync_playwright().start()
        args = [
            "--start-maximized",
            "--profile-directory=Default",
            "--disk-cache-size=1073741824",
            "--media-cache-size=268435456",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--disable-blink-features=AutomationControlled",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
            "--autoplay-policy=no-user-gesture-required",
        ]
        if self.headless:
            args.append("--window-size=1440,900")
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel="msedge",
            headless=self.headless,
            no_viewport=True,
            accept_downloads=True,
            downloads_path=str(self.download_dir),
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=args,
            ignore_default_args=["--enable-automation"],
        )
        self.context.set_default_timeout(30_000)
        self.context.set_default_navigation_timeout(45_000)
        self.context.on("page", self._bind_page)
        self.context.on("close", self._on_context_closed)

        page = next((item for item in self.context.pages if not item.is_closed()), None)
        if page is None:
            page = self.context.new_page()
        self._bind_page(page)
        page.bring_to_front()
        self.emit("status", state="running", detail="Microsoft Edge 已运行")
        self.log("Microsoft Edge 已启动；Cookies、站点数据与磁盘缓存会持续写入独立资料目录。", "success")
        self._goto_page(page, HOME_URL)
        self.publish_stats()
        self.emit("page_count", count=self._page_count())
        self.emit("focus_ui")

    def _on_context_closed(self) -> None:
        self.context = None
        self.bound_pages.clear()
        if self._context_closing:
            return
        self.emit("status", state="stopped", detail="Microsoft Edge 窗口已关闭")
        self.log("Microsoft Edge 已关闭，资料与缓存已保留。", "info")
        self._append_operation({"event": "browser_closed_manually"})

    def _bind_page(self, page: Page) -> None:
        identity = id(page)
        if identity in self.bound_pages:
            return
        self.bound_pages.add(identity)

        def frame_navigated(frame: Any) -> None:
            try:
                if frame == page.main_frame:
                    self.emit("url", url=page.url)
                    self._append_operation({"event": "navigation", "url": page.url})
                    # 主框架每次导航后就地延时注入：长任务里直接 page.goto 的页面也能跟上蜘蛛特效
                    if self.spider_overlay_enabled and not page.is_closed():
                        try:
                            page.wait_for_timeout(900)
                            self._apply_spider_overlay(page)
                        except Exception:
                            pass
            except Exception:
                pass

        def page_closed(_page: Page) -> None:
            self.emit("page_count", count=self._page_count())
            self.log("已关闭一个标签页。", "info")

        page.on("framenavigated", frame_navigated)
        page.on("close", page_closed)
        self.emit("url", url=page.url)
        self.emit("page_count", count=self._page_count())
        self._schedule_spider_injection(page, delay=1.5)

    # ---------------------------------------------------------------- 操作可视化（蜘蛛爬虫特效）

    def _set_spider_overlay(self, enabled: bool) -> None:
        """开关操作可视化：写回设置并对当前打开的页面立即生效。"""
        enabled = bool(enabled)
        self.spider_overlay_enabled = enabled
        settings = load_workbench_settings()
        settings["spider_overlay"] = enabled
        save_workbench_settings(settings)
        label = "开启" if enabled else "关闭"
        if self.context is None:
            self.log(f"操作可视化（蜘蛛爬虫特效）已{label}；浏览器启动后自动注入到打开的页面。", "info")
            return
        pages = [page for page in self.context.pages if not page.is_closed()]
        for page in pages:
            if enabled:
                self._apply_spider_overlay(page)
            else:
                self._remove_spider_overlay(page)
        if enabled:
            self.log(f"操作可视化（蜘蛛爬虫特效）已{label}：已注入当前 {len(pages)} 个标签页。", "success")
        else:
            self.log(f"操作可视化（蜘蛛爬虫特效）已{label}。", "info")

    def _set_active_pet(self, pet_id: str) -> None:
        """切换宠物：写回设置并对当前打开的页面立即按新宠物重新注入。"""
        pet = next((item for item in PETS if item["id"] == str(pet_id)), None)
        if pet is None:
            return
        settings = load_workbench_settings()
        settings["spider_pet"] = pet["id"]
        save_workbench_settings(settings)
        label = f"{pet['name']}（{pet['id']}）"
        if self.context is None:
            self.log(f"宠物已切换为 {label}；浏览器启动后自动应用。", "info")
            return
        pages = [page for page in self.context.pages if not page.is_closed()]
        for page in pages:
            self._apply_spider_overlay(page)
        self.log(f"宠物已切换为 {label}：已应用到 {len(pages)} 个标签页。", "success")

    def _visible_page(self) -> Page | None:
        """挑出用户当前正在浏览的标签页：可见的优先，持有焦点的更优先。

        宠物互动这类「对着人」的操作应作用于用户眼前的页面，而不是内部页面
        列表的最后一个；全部检测失败（如窗口最小化）时回退最后一个标签页。"""
        if self.context is None:
            return None
        pages = [page for page in self.context.pages if not page.is_closed()]
        if not pages:
            return None
        visible: list[Page] = []
        focused: list[Page] = []
        for page in pages:
            try:
                state = page.evaluate(
                    "() => ({v: document.visibilityState, f: document.hasFocus()})"
                )
            except Exception:
                continue
            if not isinstance(state, dict) or state.get("v") != "visible":
                continue
            visible.append(page)
            if state.get("f"):
                focused.append(page)
        return (focused or visible or pages)[-1]

    def _pet_play(self, action: str) -> None:
        """宠物页互动按钮：在当前正在浏览的标签页上触发一次即时联动（召唤/庆祝/撒粒子）。"""
        if self.context is None:
            self.log("浏览器尚未运行，无法与宠物互动。", "warning")
            return
        page = self._visible_page()
        if page is None:
            self.log("没有已打开的标签页，无法与宠物互动。", "warning")
            return
        self._refresh_spider_setting()
        if not self.spider_overlay_enabled:
            self.log("操作可视化当前是关闭状态；开启后宠物才会出现在页面上。", "warning")
            return
        try:
            if action == "call":
                state = self._spider_call(page, "state") or {}
                width = float(state.get("vw") or 0) or 800
                height = float(state.get("vh") or 0) or 600
                self.spider_dash_to(page, x=width / 2, y=height / 2, wait=False)
                self.log(f"已召唤{load_active_pet()['name']}到页面中央。", "info")
            elif action == "celebrate":
                self.spider_celebrate(page)
                self.log("宠物开心地转了个圈。", "info")
            elif action == "burst":
                self.spider_burst(page, count=26)
                self.log("撒了一把粒子，宠物跑去接了。", "info")
            self._append_operation({"event": "pet_play", "action": action})
        except Exception:
            pass

    def _schedule_spider_injection(self, page: Page, delay: float = 1.2) -> None:
        """登记待注入页面；由命令循环空闲时执行，避免打断 Playwright 事件回调。"""
        self._refresh_spider_setting()
        if not self.spider_overlay_enabled:
            return
        self._spider_pending = page
        self._spider_pending_at = time.time() + max(0.0, delay)

    def _refresh_spider_setting(self) -> None:
        """每次注入前重读开关设置，让运行中的插件子进程也能响应主开关。"""
        self.spider_overlay_enabled = bool(load_workbench_settings().get("spider_overlay"))

    def _service_spider_injection(self) -> None:
        if not self.spider_overlay_enabled or self._spider_pending is None:
            return
        if time.time() < self._spider_pending_at:
            return
        page = self._spider_pending
        self._spider_pending = None
        if self.context is None or page.is_closed():
            return
        self._apply_spider_overlay(page)

    def _service_idle_pollers(self) -> None:
        """执行插件注册的空闲轮询钩子（学习通批改菜单等）；轮询实现自带节流与异常兜底。"""
        for poller in list(self.idle_pollers):
            try:
                poller()
            except Exception:
                continue

    def _apply_spider_overlay(self, page: Page) -> None:
        """把宠物联动特效注入页面（顶层文档）；失败（chrome:// 等）静默跳过，不影响自动化。"""
        self._refresh_spider_setting()
        try:
            if self.spider_overlay_enabled:
                page.evaluate(SPIDER_OVERLAY_SCRIPT, load_active_pet())
            else:
                page.evaluate("() => { if (window.__wbs) window.__wbs.destroy(); }")
        except Exception:
            pass

    def _remove_spider_overlay(self, page: Page) -> None:
        try:
            page.evaluate("() => { if (window.__wbs) window.__wbs.destroy(); }")
        except Exception:
            pass
        for frame, _offset in self._spider_frames(page):
            try:
                frame.evaluate(SPIDER_COLLECTOR_DESTROY)
            except Exception:
                continue

    def _spider_frames(self, page: Page) -> list[tuple[Any, tuple[float, float]]]:
        """枚举页面的全部子帧，返回 (frame, 帧视口在主视口中的偏移)。

        偏移取 iframe 元素的 bounding_box 原点（Playwright 返回主帧视口坐标，
        嵌套帧也已折算），用于「主视口矩形 ↔ 帧内视口矩形」的坐标换算。
        不可见或过小（<24px）的帧跳过。"""
        results: list[tuple[Any, tuple[float, float]]] = []
        try:
            frames = list(page.frames)
        except Exception:
            return results
        for frame in frames:
            if frame is page.main_frame:
                continue
            try:
                box = frame.frame_element().bounding_box()
            except Exception:
                continue
            if not box or float(box.get("width") or 0) < 24 or float(box.get("height") or 0) < 24:
                continue
            results.append((frame, (float(box["x"]), float(box["y"]))))
        return results

    def _spider_harvest_frames(self, page: Page, box: Any, max_words: int = 80) -> int:
        """让各帧词采器原地采集主视口矩形 box {x, y, width, height} 内的词条，返回总数。

        词采器按需懒注入（帧导航后自动重建），跨域帧同样生效；特效关闭时调用方
        不会走到这里。"""
        accent = str(load_active_pet().get("accent") or "#54d7e8")
        total = 0
        for frame, (dx, dy) in self._spider_frames(page):
            try:
                got = frame.evaluate(
                    SPIDER_COLLECTOR_HARVEST,
                    [
                        accent,
                        float(box["x"]) - dx,
                        float(box["y"]) - dy,
                        float(box.get("width") or 0),
                        float(box.get("height") or 0),
                        int(max_words),
                    ],
                )
            except Exception:
                continue
            if isinstance(got, int) and got > 0:
                total += got
        return total

    # ---------------------------------------------------------------- 蜘蛛特效联动公共方法（插件/自动化可直接调用）

    def _spider_call(self, page: Page, api: str, *args: Any) -> Any:
        """调用页面内蜘蛛特效的公开方法；特效未开启或未注入时静默返回 None。"""
        self._refresh_spider_setting()
        if not self.spider_overlay_enabled:
            return None
        try:
            return page.evaluate(
                "([name, argv]) => { const w = window.__wbs; return (w && typeof w[name] === 'function') ? w[name](...argv) : null; }",
                [api, list(args)],
            )
        except Exception:
            return None

    @staticmethod
    def _resolve_spider_box(page: Page, selector: str | None = None, element: Any = None, box: Any = None) -> Any:
        """把选择器 / Locator / bounding_box 统一解析成元素矩形。"""
        if box is not None:
            return box
        if element is not None:
            try:
                return element.bounding_box()
            except Exception:
                return None
        if selector:
            try:
                return page.locator(selector).first.bounding_box()
            except Exception:
                return None
        return None

    @staticmethod
    def _spider_box_center(box: Any) -> tuple[float, float] | None:
        try:
            if not box or not float(box.get("width") or 0):
                return None
            return float(box["x"]) + float(box["width"]) / 2, float(box["y"]) + float(box["height"]) / 2
        except Exception:
            return None

    @staticmethod
    def _spider_wait(page: Page, wait: Any, result: Any) -> None:
        """触发冲刺后按需等待蜘蛛爬行。

        wait=True/"auto"：先按冲刺距离估算（约 450ms 起步 + 每像素 1ms，500~2600ms），
        再轮询 state().fetching 直到指向性抓取结算完毕（最多补 4.2s：冲刺途中逐词
        接触标记，抵达后剩余词条以波纹节奏补齐，整体约 1~2.5 秒，含收尾特效），
        超时后不等剩余特效（补齐继续在后台播完），保证常规情况返回时采集与特效
        都已播完；wait 为数字时按指定毫秒等待；False/0 不等待。
        """
        if not wait or not isinstance(result, dict) or "tx" not in result:
            return
        if wait is True or wait == "auto":
            try:
                dist = ((float(result["tx"]) - float(result["x"])) ** 2
                        + (float(result["ty"]) - float(result["y"])) ** 2) ** 0.5
                ms = int(min(2600.0, max(500.0, 450.0 + dist)))
            except Exception:
                ms = 1200
            try:
                page.wait_for_timeout(ms)
                deadline = time.time() + 4.2
                while time.time() < deadline:
                    fetching = page.evaluate(
                        "() => window.__wbs ? window.__wbs.state().fetching : null"
                    )
                    if not fetching:
                        break
                    page.wait_for_timeout(100)
            except Exception:
                pass
        elif isinstance(wait, (int, float)):
            try:
                page.wait_for_timeout(int(wait))
            except Exception:
                pass

    def spider_dash_to(
        self,
        page: Page,
        x: float | None = None,
        y: float | None = None,
        *,
        selector: str | None = None,
        element: Any = None,
        box: Any = None,
        wait: bool | int | float = False,
    ) -> Any:
        """蜘蛛冲刺到目标（视口坐标 / 选择器 / Locator / bounding_box）。纯移动互动，不抓取。

        wait：False（默认）不等待；True 等待蜘蛛抵达；数字 = 等待指定毫秒。
        想要"蜘蛛抢先跑过去、再执行点击"的效果，用 wait=400 左右即可。
        """
        if x is None or y is None:
            center = self._spider_box_center(self._resolve_spider_box(page, selector, element, box))
            if center is None:
                return None
            x, y = center
        result = self._spider_call(page, "dashTo", float(x), float(y))
        self._spider_wait(page, wait, result)
        return result

    def spider_fetch_element(
        self,
        page: Page,
        *,
        selector: str | None = None,
        element: Any = None,
        box: Any = None,
        padding: float = 8,
        wait: bool | str | int | float = "auto",
    ) -> Any:
        """指向性接触采集：蜘蛛径直冲刺到目标元素，途中接触到的词条逐个标记，
        抵达后把元素矩形内剩余词条从落点向外波纹补齐。

        在插件「要获取信息」的时刻调用（如读取积分、读取搜索结果），蜘蛛会爬过去、
        接触到目标后再采集；若目标过远或被视口裁剪，超时后会在可达位置结算采集。

        wait："auto"（默认）按冲刺距离自动等待，蜘蛛抵达后再返回；True 等待抵达；
        数字 = 等待指定毫秒；False = 触发后立即返回（不等待）。
        """
        b = self._resolve_spider_box(page, selector, element, box)
        if not b:
            return None
        try:
            result = self._spider_call(
                page, "fetchRect",
                float(b["x"]) - padding, float(b["y"]) - padding,
                float(b["width"]) + padding * 2, float(b["height"]) + padding * 2,
            )
        except Exception:
            return None
        self._spider_wait(page, wait, result)
        # 多帧词采：抵达后把同矩形内的 iframe 词条（含跨域帧）一并高亮采集
        if result:
            frame_rect = {
                "x": float(b["x"]) - padding,
                "y": float(b["y"]) - padding,
                "width": float(b["width"]) + padding * 2,
                "height": float(b["height"]) + padding * 2,
            }
            frame_count = self._spider_harvest_frames(page, frame_rect)
            if frame_count > 0:
                self._spider_call(page, "addCollected", frame_count)
        return result

    _SPIDER_TEXT_FINDER = r"""
    ([pattern]) => {
      try {
        const re = new RegExp(pattern, "i");
        let best = null;
        for (const el of document.querySelectorAll("body *")) {
          if (el.children.length > 0 || el.shadowRoot) continue;
          const text = (el.textContent || "").trim();
          if (!text || text.length > 60 || !re.test(text)) continue;
          const r = el.getBoundingClientRect();
          if (r.width < 8 || r.height < 8) continue;
          if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
          const style = getComputedStyle(el);
          if (style.visibility === "hidden" || style.display === "none" || style.opacity === "0") continue;
          if (!best || text.length < best.text.length) best = { text, rect: { x: r.left, y: r.top, width: r.width, height: r.height } };
        }
        return best;
      } catch (e) { return null; }
    }
    """

    def spider_fetch_text(self, page: Page, pattern: str, padding: float = 10, wait: bool | str | int | float = "auto") -> Any:
        """指向性接触采集（按文本定位）：宠物爬到包含匹配文本的最内层可见元素处，抵达后采集其文字。

        例：微软积分获取全部数据时 `spider_fetch_text(page, r"积分|points")`，
        宠物会自动爬到积分数字上接触采集，与信息读取脚本形成配合。
        顶层文档找不到时会继续到全部 iframe（含跨域帧）里查找。

        wait 语义同 spider_fetch_element："auto" 默认按距离自动等待抵达。
        """
        self._refresh_spider_setting()
        if not self.spider_overlay_enabled:
            return None
        rect: dict[str, float] | None = None
        try:
            found = page.evaluate(self._SPIDER_TEXT_FINDER, [pattern])
            rect = (found or {}).get("rect")
        except Exception:
            rect = None
        if rect is None:
            # 顶层没有匹配 → 逐帧查找（帧内坐标 + 帧偏移 = 主视口坐标）
            for frame, (dx, dy) in self._spider_frames(page):
                try:
                    found = frame.evaluate(self._SPIDER_TEXT_FINDER, [pattern])
                except Exception:
                    continue
                frame_rect = (found or {}).get("rect")
                if frame_rect:
                    rect = {
                        "x": float(frame_rect["x"]) + dx,
                        "y": float(frame_rect["y"]) + dy,
                        "width": float(frame_rect["width"]),
                        "height": float(frame_rect["height"]),
                    }
                    break
        if not rect:
            return None
        return self.spider_fetch_element(
            page,
            box={
                "x": float(rect["x"]) - padding,
                "y": float(rect["y"]) - padding,
                "width": float(rect["width"]) + padding * 2,
                "height": float(rect["height"]) + padding * 2,
            },
            wait=wait,
        )

    def spider_harvest_element(
        self,
        page: Page,
        *,
        selector: str | None = None,
        element: Any = None,
        box: Any = None,
        padding: float = 6,
    ) -> Any:
        """原地立即采集目标元素矩形内的词条（不移动）；指向性采集请用 spider_fetch_element。

        顶层文档与全部 iframe（含跨域帧）一起结算，返回合计采集数量。"""
        b = self._resolve_spider_box(page, selector, element, box)
        if not b:
            return None
        rect = {
            "x": float(b["x"]) - padding,
            "y": float(b["y"]) - padding,
            "width": float(b["width"]) + padding * 2,
            "height": float(b["height"]) + padding * 2,
        }
        try:
            top_count = self._spider_call(
                page, "harvestRect", rect["x"], rect["y"], rect["width"], rect["height"]
            )
        except Exception:
            top_count = None
        frame_count = self._spider_harvest_frames(page, rect)
        total = int(top_count or 0) + frame_count
        if frame_count > 0:
            self._spider_call(page, "addCollected", frame_count)
        return total

    def spider_harvest_point(self, page: Page, x: float, y: float, radius: float = 150) -> Any:
        """宠物「采集」视口坐标 (x, y) 半径 r 内的词条（含全部 iframe），返回采集数量。"""
        rect = {"x": float(x) - radius, "y": float(y) - radius, "width": radius * 2, "height": radius * 2}
        try:
            top_count = self._spider_call(page, "harvestAt", float(x), float(y), float(radius))
        except Exception:
            top_count = None
        frame_count = self._spider_harvest_frames(page, rect)
        total = int(top_count or 0) + frame_count
        if frame_count > 0:
            self._spider_call(page, "addCollected", frame_count)
        return total

    def spider_burst(self, page: Page, x: float | None = None, y: float | None = None, count: int = 14) -> Any:
        """在指定位置（缺省蜘蛛当前位置）迸发一簇粒子。"""
        return self._spider_call(page, "burst", x, y, int(count))

    def spider_ring(self, page: Page, x: float | None = None, y: float | None = None) -> Any:
        """在指定位置（缺省蜘蛛当前位置）泛起一圈涟漪。"""
        return self._spider_call(page, "ring", x, y)

    def spider_celebrate(self, page: Page) -> Any:
        """蜘蛛原地转圈庆祝：适合数据获取完成、任务收尾等时刻。"""
        return self._spider_call(page, "celebrate")

    def spider_wander(self, page: Page) -> Any:
        """让蜘蛛立刻随机换一个游走目标。"""
        return self._spider_call(page, "wander")

    def _page_count(self) -> int:
        if self.context is None:
            return 0
        try:
            return len([page for page in self.context.pages if not page.is_closed()])
        except Exception:
            return 0

    def _current_page(self) -> Page:
        if self.context is None:
            self.start_browser()
        if self.context is None:
            raise RuntimeError("Microsoft Edge 未能启动。")
        pages = [page for page in self.context.pages if not page.is_closed()]
        if not pages:
            page = self.context.new_page()
            self._bind_page(page)
            return page
        page = pages[-1]
        page.bring_to_front()
        return page

    def _goto_page(self, page: Page, url: str, wait_network_idle: bool = True) -> None:
        self.log(f"正在打开: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        if wait_network_idle:
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeoutError:
                pass
        try:
            title = page.title()
        except Exception:
            title = ""
        self.emit("url", url=page.url)
        self.emit("page_count", count=self._page_count())
        self._append_operation({"event": "page_view", "url": page.url, "title": title})
        self.log(f"页面已打开: {title or page.url}", "success")
        self._apply_spider_overlay(page)

    def navigate(self, raw_url: str) -> None:
        url = normalize_url(raw_url)
        page = self._current_page()
        self._goto_page(page, url)

    def bing_search(self, query: str) -> None:
        """打开 cn.bing.com，把输入框内容逐字输入搜索框并回车搜索。"""
        page = self._current_page()
        query = str(query or "").strip()
        self._append_operation({"event": "bing_search_start", "query": query[:80]})
        if query:
            self.log(f"必应搜索：正在打开 {HOME_URL}，输入关键词「{query[:60]}」…")
        else:
            self.log(f"必应搜索：输入框为空，仅打开 {HOME_URL}。")
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45_000)
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(600)
        self.emit("url", url=page.url)
        self._apply_spider_overlay(page)
        if not query:
            self.emit("status", state="running", detail="必应已打开")
            self.publish_stats()
            return

        box = None
        for selector in ("textarea[name='q']", "input[name='q']", "#sb_form_q"):
            try:
                page.wait_for_selector(selector, state="visible", timeout=3_000)
                box = page.locator(selector).first
                break
            except Exception:
                continue
        if box is None:
            self.log("未找到必应搜索框，已仅打开首页。", "warning")
            self.emit("status", state="running", detail="未找到必应搜索框")
            self.publish_stats()
            return

        self.spider_dash_to(page, element=box, wait=400)
        try:
            box.click(timeout=5_000)
        except Exception:
            pass
        try:
            box.fill("")
        except Exception:
            pass
        page.keyboard.type(query, delay=random.randint(35, 110))
        page.keyboard.press("Enter")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(800)
        self._apply_spider_overlay(page)
        # 搜索结果就是本次“获取到的信息”：蜘蛛爬过去接触采集
        self.spider_fetch_element(page, selector="#b_results")
        self.log(f"必应搜索完成：{query[:60]}", "success")
        self._append_operation({"event": "bing_search_complete", "query": query[:80], "url": page.url})
        self.emit("url", url=page.url)
        self.emit("status", state="running", detail="必应搜索完成")
        self.publish_stats()

    def new_tab(self, raw_url: str = HOME_URL) -> None:
        if self.context is None:
            self.start_browser()
        page = self.context.new_page()
        self._bind_page(page)
        page.bring_to_front()
        self._goto_page(page, normalize_url(raw_url), wait_network_idle=False)
        self.emit("page_count", count=self._page_count())

    @staticmethod
    def _human_move(page: Page, x: int, y: int, steps: int = 24) -> None:
        start = page.evaluate("() => ({x: window.innerWidth / 2, y: window.innerHeight / 2})")
        start_x = float(start["x"])
        start_y = float(start["y"])
        for index in range(1, steps + 1):
            t = index / steps
            eased = t * t * (3 - 2 * t)
            current_x = start_x + (x - start_x) * eased + random.uniform(-2.5, 2.5)
            current_y = start_y + (y - start_y) * eased + random.uniform(-2.5, 2.5)
            page.mouse.move(current_x, current_y)
            page.wait_for_timeout(random.randint(8, 28))

    def simulate_real_browsing(self) -> None:
        page = self._current_page()
        if "bing.com" not in page.url:
            self._goto_page(page, HOME_URL)
        page.wait_for_timeout(random.randint(700, 1200))

        query = "Microsoft Edge 浏览器缓存"
        self.log("模拟真实用户操作：寻找搜索框 → 点击 → 逐字输入 → 回车 → 阅读滚动。")
        self._append_operation({"event": "simulation_start", "query": query})

        search_box = page.locator("#sb_form_q").first
        try:
            search_box.wait_for(state="visible", timeout=15_000)
            box = search_box.bounding_box()
            if box:
                target_x = int(box["x"] + box["width"] * random.uniform(0.15, 0.85))
                target_y = int(box["y"] + box["height"] * random.uniform(0.25, 0.75))
                self.spider_dash_to(page, box=box)
                self._human_move(page, target_x, target_y, steps=random.randint(18, 30))
                page.mouse.click(target_x, target_y)
            else:
                search_box.click()
        except PlaywrightTimeoutError:
            page.mouse.click(500, 300)

        if search_box.count():
            search_box.fill("")
        for character in query:
            page.keyboard.type(character, delay=random.randint(45, 135))
        page.wait_for_timeout(random.randint(250, 700))
        page.keyboard.press("Enter")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(random.randint(700, 1400))
        for _ in range(random.randint(3, 5)):
            page.mouse.wheel(0, random.randint(320, 780))
            page.wait_for_timeout(random.randint(450, 1050))
            self._human_move(page, random.randint(180, 1100), random.randint(140, 650), steps=random.randint(8, 16))
        page.mouse.wheel(0, -random.randint(1000, 1900))
        page.wait_for_timeout(random.randint(700, 1200))

        try:
            title = page.title()
        except Exception:
            title = ""
        self.spider_celebrate(page)
        self.emit("url", url=page.url)
        self._append_operation({"event": "simulation_complete", "url": page.url, "title": title})
        self.log(f"模拟浏览完成，当前页面: {title or page.url}", "success")
        self.emit("status", state="running", detail="模拟浏览完成，新增缓存已写入磁盘")
        self.publish_stats()

    def _wait_for_rewards_ready(self, page: Page) -> None:
        # rewards 流程用直接 page.goto 导航，这里补一个确定性注入点
        self._apply_spider_overlay(page)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(900)
        for _ in range(5):
            page.mouse.wheel(0, 1100)
            page.wait_for_timeout(300)
        try:
            page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:
            pass
        page.wait_for_timeout(500)

    def _click_today_points_drawer(self, page: Page) -> dict[str, Any]:
        for pattern in (re.compile("今日积分"), re.compile("积分明细")):
            try:
                locator = page.get_by_role("button", name=pattern)
                count = min(locator.count(), 6)
                for index in range(count):
                    candidate = locator.nth(index)
                    try:
                        if not candidate.is_visible():
                            continue
                        label = (candidate.inner_text(timeout=2000) or "").strip().replace("\n", " ")
                        self.spider_dash_to(page, element=candidate, wait=450)
                        candidate.click(timeout=5_000)
                        page.wait_for_timeout(800)
                        return {"clicked": True, "target": label[:90] or pattern.pattern}
                    except Exception:
                        continue
            except Exception:
                continue

        try:
            result = page.evaluate(
                """() => {
                    const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const button = buttons.find((item) => /今日积分|积分明细/.test(clean(item.innerText)));
                    if (!button) return { clicked: false, target: '' };
                    const target = clean(button.innerText).slice(0, 90);
                    button.click();
                    return { clicked: true, target };
                }"""
            )
            if result.get("clicked"):
                page.wait_for_timeout(800)
                return result
        except Exception:
            pass
        return {"clicked": False, "target": ""}

    def _extract_rewards_page(self, page: Page) -> dict[str, Any]:
        data = page.evaluate(REWARDS_EXTRACTION_SCRIPT)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _summarize_rewards(earn: dict[str, Any], dashboard: dict[str, Any]) -> dict[str, Any]:
        raw_tasks = earn.get("daily_tasks") or dashboard.get("daily_activities") or []
        # 页面原生计数（“日常任务 25/25”）包含无积分项；摘要只统计带积分的任务。
        daily_tasks = [task for task in raw_tasks if task.get("points")]
        completed = sum(1 for task in daily_tasks if task.get("completed"))
        pending = sum(1 for task in daily_tasks if not task.get("completed"))
        return {
            "fetched_at": now_text(),
            "login_required": bool(earn.get("login_required") or dashboard.get("login_required")),
            "today_points": earn.get("today_points") if earn.get("today_points") is not None else dashboard.get("today_points"),
            "account_points": earn.get("account_points") if earn.get("account_points") is not None else dashboard.get("account_points"),
            "daily_task_progress": earn.get("daily_task_progress") or dashboard.get("daily_task_progress"),
            "today_points_progress": earn.get("today_points_progress") or dashboard.get("today_points_progress"),
            "edge_browsing": dashboard.get("edge_browsing") or earn.get("edge_browsing"),
            "search_points": earn.get("search_row_points"),
            "search_progress": earn.get("search_progress") or earn.get("today_points_progress"),
            "offer_points": earn.get("offer_row_points"),
            "daily_tasks": daily_tasks,
            "daily_tasks_completed": completed,
            "daily_tasks_pending": pending,
            "earn_url": earn.get("url"),
            "dashboard_url": dashboard.get("url"),
        }

    def fetch_rewards_all(self) -> None:
        """统一获取：earn → dashboard → about 单次导航流程，基本数据与账户规则同时更新。"""
        page = self._current_page()
        original_url = page.url
        self.emit("account_info_status", state="loading", message="正在获取 Rewards 全部数据…")
        self.log("开始获取 Rewards 全部数据（基本数据 + 账户与积分规则），单次读取。")
        self._append_operation({"event": "rewards_fetch_start", "original_url": original_url})
        try:
            earn, dashboard = self._extract_earn_and_dashboard(page)
            account = self._extract_rewards_account_info(page)

            summary = self._summarize_rewards(earn, dashboard)
            payload = {
                "fetched_at": now_text(),
                "summary": summary,
                "earn": earn,
                "dashboard": dashboard,
                "note": "仅提取页面上的公开可见状态文本；不会自动领取或提交任务。",
            }
            self.rewards_data_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._append_operation({
                "event": "rewards_fetch_complete",
                "today_points": summary.get("today_points"),
                "daily_tasks_completed": summary.get("daily_tasks_completed"),
                "daily_tasks_pending": summary.get("daily_tasks_pending"),
                "path": str(self.rewards_data_file),
            })
            tasks = summary.get("daily_tasks", [])
            task_preview = "；".join(
                f"{'已完成' if task.get('completed') else '未完成'} {task.get('title')} +{task.get('points')}"
                for task in tasks[:8]
            )
            self.log(
                "Rewards 数据: "
                f"今日积分={summary.get('today_points')}，"
                f"账户积分={summary.get('account_points')}，"
                f"每日任务={summary.get('daily_tasks_completed')}/{len(summary.get('daily_tasks') or [])}，"
                f"Edge={summary.get('edge_browsing')}",
                "success",
            )
            if task_preview:
                self.log(f"带积分日常任务: {task_preview}")
            if summary.get("login_required"):
                self.log("Rewards 页面要求登录；请先在此 Edge 窗口登录 Microsoft 账号后重试。", "warning")
            self.emit("rewards_data", data=summary)
            self.emit("status", state="running", detail="Rewards 基本数据已获取")
            self.spider_celebrate(page)

            page.goto(REWARDS_ABOUT_URL, wait_until="domcontentloaded", timeout=45_000)
            self._wait_for_rewards_ready(page)
            rules = self._extract_rewards_rules(page)

            level = str(account.get("membership_level") or "").strip()
            if "金牌" in level:
                tier_key = "金牌"
            elif "银牌" in level:
                tier_key = "银牌"
            else:
                tier_key = "会员"
            tier_rules = (rules.get("tiers") or {}).get(tier_key, {})
            search_rules = {
                "points_per_search": int(rules.get("points_per_search") or SEARCH_POINTS_PER_ACTION),
                "daily_search_limit": tier_rules.get("daily_search_limit"),
                "current_tier": level or tier_key,
                "current_tier_key": tier_key,
                "monthly_points_requirement": tier_rules.get("monthly_points_requirement"),
                "monthly_level_reward": tier_rules.get("monthly_level_reward"),
                "default_search_reward": tier_rules.get("default_search_reward"),
                "bing_star_reward_max": tier_rules.get("bing_star_reward_max"),
                "store_multiplier": tier_rules.get("store_multiplier"),
                "xbox_multiplier": tier_rules.get("xbox_multiplier"),
                "redemption_discount_points": tier_rules.get("redemption_discount_points"),
            }
            self._account_rules = search_rules
            account.pop("raw_text_preview", None)
            rules.pop("raw_text", None)
            account_payload = {
                "fetched_at": now_text(),
                "account": account,
                "rules": rules,
                "search_rules": search_rules,
                "note": "账户信息与积分规则仅用于本地搜索计划计算。",
            }
            self.rewards_account_file.write_text(json.dumps(account_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._append_operation({
                "event": "account_info_fetch_complete",
                "username": account.get("username"),
                "membership_level": account.get("membership_level"),
                "available_points": account.get("available_points"),
                "points_per_search": search_rules.get("points_per_search"),
                "daily_search_limit": search_rules.get("daily_search_limit"),
                "path": str(self.rewards_account_file),
            })
            self.log(
                f"账户: {account.get('username') or '--'}，等级 {search_rules.get('current_tier')}，"
                f"可用积分 {account.get('available_points') if account.get('available_points') is not None else '--'}，"
                f"可领取 {account.get('claimable_points') if account.get('claimable_points') is not None else '--'}，"
                f"连续打卡 {account.get('streak_days') if account.get('streak_days') is not None else '--'} 天。",
                "success",
            )
            self.log(
                f"积分规则: 每次搜索 {search_rules.get('points_per_search')} 分，"
                f"{search_rules.get('current_tier')}每日搜索上限 {search_rules.get('daily_search_limit')} 分，"
                f"每月等级奖励 {search_rules.get('monthly_level_reward')} 分，"
                f"默认搜索奖励 {search_rules.get('default_search_reward')} 分。",
                "success",
            )
            self.emit("rewards_account", data=account_payload)
            self.emit("account_info_status", state="done", message="账户信息和积分规则已获取")
            self.emit("status", state="running", detail="Rewards 全部数据已获取")
        except Exception as exc:
            self.emit("account_info_status", state="error", message=f"获取失败：{exc}")
            raise
        finally:
            if isinstance(original_url, str) and original_url.startswith("http") and page.url != original_url:
                try:
                    page.goto(original_url, wait_until="domcontentloaded", timeout=45_000)
                except Exception:
                    pass

    def fetch_rewards_data(self) -> None:
        page = self._current_page()
        original_url = page.url
        self.log("开始获取 Microsoft Rewards 基本数据……")
        self._append_operation({"event": "rewards_fetch_start", "original_url": original_url})
        try:
            earn, dashboard = self._extract_earn_and_dashboard(page)
            summary = self._summarize_rewards(earn, dashboard)
            payload = {
                "fetched_at": now_text(),
                "summary": summary,
                "earn": earn,
                "dashboard": dashboard,
                "note": "仅提取页面上的公开可见状态文本；不会自动领取或提交任务。",
            }
            self.rewards_data_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._append_operation({
                "event": "rewards_fetch_complete",
                "today_points": summary.get("today_points"),
                "daily_tasks_completed": summary.get("daily_tasks_completed"),
                "daily_tasks_pending": summary.get("daily_tasks_pending"),
                "path": str(self.rewards_data_file),
            })

            tasks = summary.get("daily_tasks", [])
            task_preview = "；".join(
                f"{'已完成' if task.get('completed') else '未完成'} {task.get('title')} +{task.get('points')}"
                for task in tasks[:8]
            )
            self.log(
                "Rewards 数据: "
                f"今日积分={summary.get('today_points')}，"
                f"账户积分={summary.get('account_points')}，"
                f"每日任务={summary.get('daily_tasks_completed')}/{len(summary.get('daily_tasks') or [])}，"
                f"Edge={summary.get('edge_browsing')}",
                "success",
            )
            if task_preview:
                self.log(f"带积分日常任务: {task_preview}")
            if summary.get("login_required"):
                self.log("Rewards 页面要求登录；请先在此 Edge 窗口登录 Microsoft 账号后重试。", "warning")
            self.emit("rewards_data", data=summary)
            self.emit("status", state="running", detail="Rewards 基本数据已获取")
            self.spider_celebrate(page)
        finally:
            if isinstance(original_url, str) and original_url.startswith("http") and page.url != original_url:
                try:
                    page.goto(original_url, wait_until="domcontentloaded", timeout=45_000)
                except Exception:
                    pass

    def test_click(self) -> None:
        page = self._current_page()
        self.log("执行测试点击：Rewards 今日积分抽屉 / Bing 搜索框。")
        if "rewards.bing.com" not in page.url:
            page.goto(REWARDS_EARN_URL, wait_until="domcontentloaded", timeout=45_000)
            self._wait_for_rewards_ready(page)

        result = self._click_today_points_drawer(page)
        clicked = bool(result.get("clicked"))
        target = str(result.get("target") or "")
        if clicked:
            self.log(f"测试点击成功，已点击: {target}", "success")
        else:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(600)
            search_box = page.locator("#sb_form_q").first
            try:
                search_box.wait_for(state="visible", timeout=10_000)
                search_box.click(timeout=5000)
                clicked = True
                target = "Bing 搜索框"
                self.log("测试点击成功，已点击 Bing 搜索框。", "success")
            except Exception as exc:
                raise RuntimeError(f"测试点击失败，没有找到可安全点击的目标: {exc}") from exc

        try:
            page.screenshot(path=str(self.test_click_screenshot), full_page=False)
        except Exception:
            pass
        self._append_operation({"event": "test_click", "url": page.url, "target": target, "success": clicked})
        self.emit("status", state="running", detail=f"测试点击成功：{target[:36]}")

    def cancel_search_task(self) -> None:
        self._cancel_event.set()
        self.emit("search_task_status", state="cancelling", message="正在取消搜索任务…", current=0, total=0)

    def _check_cancel(self) -> None:
        if self._cancel_event.is_set():
            raise TaskCancelled()

    def _search_task_log(self, entry: dict[str, Any]) -> None:
        payload = {"timestamp": now_text(), **entry}
        try:
            self.search_task_log.parent.mkdir(parents=True, exist_ok=True)
            with self.search_task_log.open("a", encoding="utf-8") as file:
                file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _wait_with_cancel(self, seconds: float, message: str = "") -> None:
        self._check_cancel()
        if message:
            self.emit("search_task_status", state="waiting", message=message, wait_seconds=round(seconds))
        if self._cancel_event.wait(max(0.0, seconds)):
            raise TaskCancelled()

    def _extract_rewards_account_info(self, page: Page) -> dict[str, Any]:
        try:
            data = page.evaluate(REWARDS_ACCOUNT_EXTRACTION_SCRIPT)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            self.log(f"账户信息提取脚本执行失败：{exc}", "warning")
            return {}

    def _extract_rewards_rules(self, page: Page) -> dict[str, Any]:
        try:
            raw_text = page.evaluate(
                r"""() => {
                    const section = document.querySelector('#benefits');
                    return section ? section.innerText.replace(/\s+/g, ' ').trim() : '';
                }"""
            )
        except Exception:
            raw_text = ""
        text = str(raw_text or "")
        if not text:
            return {"parse_ok": False, "raw_text": ""}

        def numbers(pattern: str) -> list[int]:
            match = re.search(pattern, text, re.S)
            if not match:
                return []
            return [int(value) for value in match.groups()]

        def at(values: list[int], index: int, default: int | None = None) -> int | None:
            if not values:
                return default
            if len(values) == 1:
                return values[0] if index == 2 else default
            if len(values) == 2:
                return values[index - 1] if index > 0 else default
            if index < len(values):
                return values[index]
            return default

        points_match = re.search(r"每次搜索\s*(\d{1,2})\s*积分", text)
        points_per_search = int(points_match.group(1)) if points_match else SEARCH_POINTS_PER_ACTION
        search_limits = numbers(r"必应搜索：每日积分限制（每次搜索\s*\d{1,2}\s*积分）\s+(\d+)\s+(\d+)\s+(\d+)")
        monthly_requirements = numbers(r"赚取的积分\(每月\)\s+—\s+(\d+)\s+(\d+)")
        upgrade_activities = numbers(r"升级活动（每月）\s+—\s+—\s+(\d+)")
        monthly_level_rewards = numbers(r"每月级别奖励[^0-9]{0,20}(\d+)\s+(\d+)\s+(\d+)")
        default_search_rewards = numbers(r"默认搜索奖励（每月）[^0-9]{0,20}(\d+)\s+(\d+)\s+(\d+)")
        star_rewards = numbers(r"必应\s*Star\s*奖励\(每月\)[^0-9]{0,20}(\d+)\s+(\d+)\s+(\d+)")
        store_multipliers = numbers(r"Microsoft Store:.*?(\d+)x\s+(\d+)x\s+(\d+)x")
        xbox_multipliers = numbers(r"XBOX:.*?(\d+)x\s+(\d+)x\s+(\d+)x")
        redemption_discounts = numbers(r"兑换折扣优惠券\(点数\)\s+—\s+(\d+)\s+(\d+)")

        tiers: dict[str, dict[str, Any]] = {}
        for index, tier_name in enumerate(("会员", "银牌", "金牌")):
            tiers[tier_name] = {
                "monthly_points_requirement": at(monthly_requirements, index, 0),
                "upgrade_activities_per_month": at(upgrade_activities, index),
                "daily_search_limit": at(search_limits, index),
                "monthly_level_reward": at(monthly_level_rewards, index),
                "default_search_reward": at(default_search_rewards, index),
                "bing_star_reward_max": at(star_rewards, index),
                "store_multiplier": at(store_multipliers, index),
                "xbox_multiplier": at(xbox_multipliers, index),
                "redemption_discount_points": at(redemption_discounts, index),
            }
        return {
            "parse_ok": True,
            "points_per_search": points_per_search,
            "tiers": tiers,
            "raw_text": text,
        }

    def _load_cached_account_rules(self) -> dict[str, Any]:
        if self._account_rules:
            return self._account_rules
        if self.rewards_account_file.exists():
            try:
                payload = json.loads(self.rewards_account_file.read_text(encoding="utf-8"))
                rules = payload.get("search_rules") or (payload.get("rules") or {}).get("search_rules") or {}
                if isinstance(rules, dict):
                    self._account_rules = rules
            except Exception:
                pass
        return self._account_rules

    def _extract_earn_and_dashboard(self, page: Page) -> tuple[dict[str, Any], dict[str, Any]]:
        """依次打开 earn 与 dashboard 页，提取公开可见状态文本。"""
        page.goto(REWARDS_EARN_URL, wait_until="domcontentloaded", timeout=45_000)
        self._wait_for_rewards_ready(page)
        drawer_click = self._click_today_points_drawer(page)
        if drawer_click.get("clicked"):
            self.log(f"已点开今日积分抽屉: {drawer_click.get('target')}", "success")
        else:
            self.log("未找到今日积分抽屉按钮，将直接读取页面基础数据。", "warning")
        # 蜘蛛联动：信息读取脚本运行的同时，蜘蛛爬到积分信息处接触采集（wait=auto 自动等它爬到）
        self.spider_fetch_text(page, r"积分|points")
        earn = self._extract_rewards_page(page)
        earn.pop("raw_text_preview", None)
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass
        page.goto(REWARDS_DASHBOARD_URL, wait_until="domcontentloaded", timeout=45_000)
        self._wait_for_rewards_ready(page)
        self.spider_fetch_text(page, r"积分|points")
        dashboard = self._extract_rewards_page(page)
        dashboard.pop("raw_text_preview", None)
        return earn, dashboard

    def _build_search_plan(self, page: Page) -> dict[str, Any]:
        page.goto(REWARDS_EARN_URL, wait_until="domcontentloaded", timeout=45_000)
        self._wait_for_rewards_ready(page)
        self._click_today_points_drawer(page)
        data = self._extract_rewards_page(page)
        account_rules = self._load_cached_account_rules()
        points_per_action = int(account_rules.get("points_per_search") or SEARCH_POINTS_PER_ACTION)
        rule_daily_limit = int(account_rules.get("daily_search_limit") or 0)
        progress = data.get("search_progress") or data.get("today_points_progress") or {}
        current = int(progress.get("current") or 0)
        target = int(progress.get("target") or rule_daily_limit or 60)
        remaining_points = max(0, target - current)
        required_searches = math.ceil(remaining_points / points_per_action) if remaining_points > 0 else 0
        news_needed = min(SEARCH_TASK_MAX_ACTIONS, required_searches + SEARCH_NEWS_EXTRA) if required_searches > 0 else 0
        return {
            "current_search_points": current,
            "target_search_points": target,
            "remaining_search_points": remaining_points,
            "points_per_action": points_per_action,
            "rule_daily_search_limit": rule_daily_limit,
            "current_tier": account_rules.get("current_tier"),
            "required_searches": required_searches,
            "news_needed": news_needed,
            "account_points": data.get("account_points"),
            "search_progress_text": progress.get("text") or f"{current}/{target}",
        }

    def _fetch_toutiao_news(self, count: int) -> list[dict[str, str]]:
        if self.context is None:
            self.start_browser()
        if self.context is None:
            raise RuntimeError("Microsoft Edge 未启动，无法获取今日头条新闻。")
        news_page = self.context.new_page()
        try:
            self.emit("search_task_status", state="news", message="正在从今日头条随机获取新闻…", current=0, total=count)
            news_page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=45_000)
            try:
                news_page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeoutError:
                pass
            news_page.wait_for_timeout(1200)
            for _ in range(6):
                news_page.mouse.wheel(0, random.randint(700, 1200))
                news_page.wait_for_timeout(random.randint(250, 550))
            items = news_page.evaluate(
                r"""() => {
                    const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    const links = anchors.map((anchor) => ({
                        title: clean(anchor.innerText || anchor.getAttribute('aria-label') || anchor.title || ''),
                        href: anchor.href || '',
                    })).filter((item) => item.title.length >= 8 && item.title.length <= 90 && /toutiao\.com\/(?:article|video|item|group|w)\//i.test(item.href));
                    const seen = new Set();
                    return links.filter((item) => {
                        if (seen.has(item.title)) return false;
                        seen.add(item.title);
                        return true;
                    }).slice(0, 200);
                }"""
            )
            if not isinstance(items, list):
                items = []
            random.shuffle(items)
            selected = items[:count]
            cache_payload = {
                "fetched_at": now_text(),
                "source": TOUTIAO_URL,
                "requested": count,
                "available": len(items),
                "items": selected,
            }
            self.news_cache_file.write_text(json.dumps(cache_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._search_task_log({"event": "news_fetch", "requested": count, "available": len(items), "selected": len(selected)})
            return selected
        finally:
            try:
                news_page.close()
            except Exception:
                pass

    def _search_news_item(self, page: Page, title: str) -> str:
        query = re.sub(r"\s+", " ", title).strip()
        query = query[:30]
        url = f"https://cn.bing.com/search?q={quote_plus(query)}&form=QBLH"
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(random.randint(1800, 3500))
        # 蜘蛛联动：新闻搜索结果就是“获取到的信息”，蜘蛛爬过去接触采集
        self._apply_spider_overlay(page)
        self.spider_fetch_element(page, selector="#b_results")
        for _ in range(random.randint(2, 4)):
            page.mouse.wheel(0, random.randint(350, 900))
            page.wait_for_timeout(random.randint(500, 1200))
            self._human_move(page, random.randint(180, 1050), random.randint(160, 650), steps=random.randint(6, 12))
        return query

    def _read_bing_total_points(self, page: Page) -> int | None:
        try:
            raw = page.evaluate(
                r"""() => {
                    const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
                    const extractNumber = (text) => {
                        const match = clean(text).match(/\d{1,3}(?:,\d{3})+|\d{3,7}/);
                        return match ? match[0] : '';
                    };
                    // 旧版页头 Rewards 组件的已知节点，优先尝试；提取不到数字时继续走结构定位。
                    const known = [
                        '#rh_rwm .points-container',
                        '#rh_rwm span.points-container',
                        '#rh_rwm .b_clickarea',
                        '#rh_rwm',
                        '#rch',
                        '#rchsp',
                        '.points-container',
                        '.rwrl_js'
                    ];
                    for (const selector of known) {
                        const element = document.querySelector(selector);
                        if (element && element.innerText) {
                            const number = extractNumber(element.innerText);
                            if (number) return number;
                        }
                    }
                    // cn.bing.com 搜索页右上角的总积分（紧挨奖章图标，如 “15397”）：
                    // 节点 id/class 不稳定，按「右上角位置 + 纯数字文本 + Rewards 上下文」的结构特征定位。
                    const parseValue = (text) => {
                        const match = text.match(/^(?:\d{1,3}(?:,\d{3})+|\d{2,7})$/);
                        return match ? parseInt(match[0].replace(/,/g, ''), 10) : null;
                    };
                    const candidates = [];
                    for (const element of document.querySelectorAll('body *')) {
                        const value = parseValue(clean(element.innerText));
                        if (value === null) continue;
                        if (element.children.length > 2) continue;
                        const rect = element.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        if (rect.top > 120 || rect.left < window.innerWidth * 0.55) continue;
                        candidates.push({ element: element, value: value });
                    }
                    const hasRewardsContext = (item) => {
                        let node = item.element;
                        for (let depth = 0; depth < 5 && node; depth += 1) {
                            const label = clean(
                                (node.getAttribute && (node.getAttribute('aria-label') || node.getAttribute('title'))) || ''
                            );
                            const href = clean((node.getAttribute && node.getAttribute('href')) || '');
                            if (/rewards|积分/i.test(label) || /rewards/i.test(href)) return true;
                            node = node.parentElement;
                        }
                        return false;
                    };
                    const matched = candidates.filter(hasRewardsContext);
                    if (matched.length) return matched[0].element.innerText;
                    // 没有 Rewards 标记时：仅当右上角所有候选解析出同一个 ≥3 位数值才采信
                    // （父容器与内层节点会重复报同一个数，按数值去重；1-2 位可能是通知角标，不采信）。
                    const distinct = Array.from(new Set(candidates.map((item) => item.value)));
                    if (distinct.length === 1 && distinct[0] >= 100) return candidates[0].element.innerText;
                    return '';
                }"""
            )
        except Exception:
            return None
        match = re.search(r"\d{1,3}(?:,\d{3})+|\d{3,7}", str(raw or ""))
        if not match:
            return None
        try:
            return int(match.group(0).replace(",", ""))
        except ValueError:
            return None

    def _read_bing_points_with_retry(self, page: Page, previous: int | None) -> int | None:
        """读取搜索页右上角的总积分计数器。

        搜索完成后计数器加载/刷新需要几秒，先等约 5 秒再读（每轮共尝试 3 次）；
        完全读不到时返回 None，由调用方决定是否改用服务端积分。
        """
        latest: int | None = None
        for attempt in range(3):
            self._check_cancel()
            page.wait_for_timeout(random.randint(4500, 6000) if attempt == 0 else random.randint(3500, 5500))
            current = self._read_bing_total_points(page)
            if current is not None:
                latest = current
                if previous is None or current > previous:
                    return current
                # 读到但未上涨：计数器可能还没把本轮积分刷出来，稍等后再读一次。
        if latest is None and not self._header_dump_done:
            self._header_dump_done = True
            snippet = self._dump_header_points_region(page)
            if snippet:
                self.log(f"右上角未识别到积分计数器，页头区域片段：{snippet}", "warning")
        return latest

    def _dump_header_points_region(self, page: Page) -> str:
        """诊断用：抓取搜索页右上角区域的 HTML 片段，排查积分计数器未被识别的原因。"""
        try:
            return str(page.evaluate(
                r"""() => {
                    const snippets = [];
                    for (const element of document.elementsFromPoint(window.innerWidth - 40, 45)) {
                        let node = element;
                        for (let depth = 0; depth < 3 && node; depth += 1) {
                            node = node.parentElement;
                        }
                        if (node && node.outerHTML) snippets.push(node.outerHTML.slice(0, 300));
                    }
                    return Array.from(new Set(snippets)).join('\n---\n').slice(0, 1200);
                }"""
            ) or "")
        except Exception:
            return ""

    @staticmethod
    def _gained_points(baseline: int | None, current: int | None) -> int | None:
        """上涨积分 = 当前积分 - 账户积分基线；任一未知返回 None，不同步导致的负数按 0 展示。"""
        if current is None or baseline is None:
            return None
        return max(0, current - baseline)

    def _cached_account_points(self) -> int | None:
        """读取本地缓存中展示的「账户积分」，作为上涨积分的兜底基线。"""
        if not self.rewards_data_file.exists():
            return None
        try:
            payload = json.loads(self.rewards_data_file.read_text(encoding="utf-8"))
            value = (payload.get("summary") or {}).get("account_points")
            return int(value) if value is not None else None
        except Exception:
            return None

    @staticmethod
    def _extract_available_points(raw_text: str) -> int | None:
        """从 Rewards 服务端返回内容中解析 availablePoints（优先 JSON 结构，退回全文正则）。"""
        try:
            raw = ((json.loads(raw_text).get("dashboard") or {}).get("userStatus") or {}).get("availablePoints")
            if raw is not None:
                return int(raw)
        except Exception:
            pass
        match = re.search(r'"availablePoints"\s*:\s*(\d+)', raw_text or "")
        return int(match.group(1)) if match else None

    def _sync_account_points(self) -> int | None:
        """搜索页计数器不可读时的兜底：向 Rewards 服务端（rewards.bing.com 用户信息接口）查询当前积分。

        复用浏览器登录态（共享 Cookie），不会打断当前搜索页面，也不改动本地缓存文件。
        """
        if self.context is None:
            return None
        # 仅允许向固定的 Rewards 官方域名发起 https 查询，避免地址被改动后把登录态发到别处。
        parts = urlsplit(REWARDS_USERINFO_URL)
        if parts.scheme != "https" or (parts.hostname or "") not in ALLOWED_POINTS_SYNC_HOSTS:
            self.log(f"积分查询地址不受支持，已跳过：{REWARDS_USERINFO_URL}", "warning")
            return None
        for attempt in range(2):
            try:
                response = self.context.request.get(REWARDS_USERINFO_URL, timeout=15_000)
            except Exception as exc:
                self.log(f"向 Rewards 服务端查询积分失败：{exc}", "warning")
                return None
            if response.ok:
                raw_text = ""
                try:
                    raw_text = response.text()
                except Exception:
                    pass
                points = self._extract_available_points(raw_text)
                if points is not None:
                    return points
                preview = re.sub(r"\s+", " ", str(raw_text or "")).strip()[:200]
                self.log(
                    f"Rewards 服务端返回中没有识别到可用积分字段（第 {attempt + 1} 次），返回片段：{preview or '<空>'}",
                    "warning",
                )
            else:
                self.log(f"Rewards 服务端返回状态码 {response.status}，无法查询积分（第 {attempt + 1} 次）。", "warning")
            if attempt == 0:
                # 偶发异常返回（风控页/临时错误）时稍等重试一次。
                if self._cancel_event.wait(3):
                    raise TaskCancelled()
        return None

    def _calibrate_rewards_points(self, page: Page, return_url: str | None = None) -> dict[str, Any]:
        original_url = return_url or page.url
        page.goto(REWARDS_EARN_URL, wait_until="domcontentloaded", timeout=45_000)
        self._wait_for_rewards_ready(page)
        self._click_today_points_drawer(page)
        earn = self._extract_rewards_page(page)
        earn.pop("raw_text_preview", None)
        payload: dict[str, Any] = {}
        if self.rewards_data_file.exists():
            try:
                payload = json.loads(self.rewards_data_file.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
        dashboard = payload.get("dashboard", {})
        summary = self._summarize_rewards(earn, dashboard)
        payload.update({
            "fetched_at": now_text(),
            "summary": summary,
            "earn": earn,
            "dashboard": dashboard,
            "note": "搜索任务校准结果；不会自动提交或领取任务。",
        })
        self.rewards_data_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._search_task_log({
            "event": "calibration",
            "account_points": summary.get("account_points"),
            "search_progress": summary.get("search_progress"),
        })
        self.emit("rewards_data", data=summary)
        if isinstance(original_url, str) and original_url.startswith("http") and page.url != original_url:
            try:
                page.goto(original_url, wait_until="domcontentloaded", timeout=45_000)
            except Exception:
                pass
        return summary

    def _cooldown_for_no_increase(self, page: Page) -> dict[str, Any]:
        remaining = SEARCH_COOLDOWN_SECONDS
        self.log("连续 3 次搜索积分未上涨，进入 15 分钟冷却期。", "warning")
        while remaining > 0:
            self._check_cancel()
            minutes, seconds = divmod(remaining, 60)
            self.emit(
                "search_task_status",
                state="cooldown",
                message=f"积分未上涨，冷却中：剩余 {minutes:02d}:{seconds:02d}",
                cooldown_remaining=remaining,
                current=0,
                total=0,
            )
            chunk = min(30, remaining)
            if self._cancel_event.wait(chunk):
                raise TaskCancelled()
            remaining -= chunk
        self.log("冷却期结束，重新校准 Rewards 积分并继续。", "success")
        return self._calibrate_rewards_points(page)

    def auto_run(self) -> None:
        """一键化流程：启动浏览器 → 刷新全部数据 → 搜索任务 → 每日任务 → 最终同步。"""
        if self._auto_running:
            self.log("一键自动化已经在运行。", "warning")
            return
        if self._search_running:
            self.log("已有搜索 / 任务流程正在运行，请等待其结束或先取消。", "warning")
            return
        self._auto_running = True
        self._cancel_event.clear()  # 清掉上次停止/取消残留的标志，避免新流程被误判取消
        try:
            self.emit("auto_status", state="started", message="一键自动化开始")
            self.log("一键自动化开始：刷新全部数据 → 搜索任务 → 每日任务 → 最终数据同步。")
            if self.context is None:
                self.emit("auto_status", state="running", message="一键自动化：正在启动浏览器…")
                self.log("浏览器未启动，正在自动启动 Edge。")
                self.start_browser()
            steps = (
                ("刷新全部数据", self.fetch_rewards_data),
                ("搜索任务", self.run_search_task),
                ("每日任务", self.trigger_incomplete_tasks),
                ("最终数据同步", self.fetch_rewards_data),
            )
            for name, step in steps:
                if self._cancel_event.is_set():
                    self.log("一键自动化：检测到取消指令，停止后续步骤。", "warning")
                    self.emit("auto_status", state="cancelled", message="一键自动化已取消")
                    return
                self.emit("auto_status", state="running", message=f"一键自动化：{name}…")
                self.log(f"一键自动化 · {name}：开始。")
                step()
                self.log(f"一键自动化 · {name}：完成。", "success")
            self.emit("auto_status", state="done", message="一键自动化全部完成")
            self.log("一键自动化：完整流程执行完毕。", "success")
        except Exception as exc:
            self.emit("auto_status", state="error", message=f"一键自动化失败: {exc}")
            self.log(f"一键自动化失败: {exc}", "error")
            self.emit("log", level="error", message=traceback.format_exc(limit=5))
        finally:
            self._auto_running = False

    def run_search_task(self) -> None:
        if self._search_running:
            self.log("搜索任务已经在运行。", "warning")
            return
        self._search_running = True
        self._cancel_event.clear()
        self._active_flow = "search"
        self._header_dump_done = False
        try:
            if self.context is None:
                self.emit("search_task_status", state="planning", message="浏览器未运行，正在自动启动 Edge…", current=0, total=0)
            page = self._current_page()
            self.emit("search_task_status", state="planning", message="正在计算今日搜索积分进度…", current=0, total=0)
            plan = self._build_search_plan(page)
            # 上涨积分基线 = 任务开始时的账户积分；计划页没读到时退回缓存里展示的「账户积分」。
            baseline_points = plan.get("account_points")
            if baseline_points is None:
                baseline_points = self._cached_account_points()
            self.emit(
                "search_task_status",
                state="planned",
                message="搜索计划已生成",
                current=0,
                total=plan["news_needed"],
                plan=plan,
                gained=self._gained_points(baseline_points, baseline_points),
            )
            self._search_task_log({"event": "plan", **plan})
            self.log(
                f"搜索计划: 今日搜索积分 {plan['search_progress_text']}，"
                f"剩余 {plan['remaining_search_points']} 分，按 {plan['points_per_action']} 分/次计算，"
                f"需要 {plan['required_searches']} 次搜索，另加 {SEARCH_NEWS_EXTRA} 条新闻缓冲，共准备 {plan['news_needed']} 条。",
            )
            if plan["news_needed"] <= 0:
                self.log("今日搜索积分已完成，无需执行新闻搜索任务。", "success")
                self._calibrate_rewards_points(page)
                self.emit("search_task_status", state="done", message="今日搜索积分已完成，无需搜索", current=0, total=0)
                return

            news = self._fetch_toutiao_news(plan["news_needed"])
            if not news:
                raise RuntimeError("今日头条没有返回可用于搜索的新闻标题。")
            self.log(f"已随机选择 {len(news)} 条今日头条新闻用于搜索。", "success")
            self.emit("search_task_status", state="searching", message="新闻准备完成，开始搜索…", current=0, total=len(news))

            last_points = plan.get("account_points")
            no_increase_count = 0
            cooldown_count = 0
            stopped_due_to_no_increase = False
            total = len(news)
            for index, item in enumerate(news, start=1):
                self._check_cancel()
                title = str(item.get("title") or "").strip()
                if len(title) < 8:
                    continue
                self.emit(
                    "search_task_status",
                    state="searching",
                    message=f"[{index}/{total}] 搜索：{title[:34]}",
                    current=index - 1,
                    total=total,
                    points=last_points,
                    gained=self._gained_points(baseline_points, last_points),
                )
                query = self._search_news_item(page, title)
                points_before = last_points
                current_points = self._read_bing_points_with_retry(page, last_points)
                if current_points is not None:
                    points_source = "bing_header"
                else:
                    # 搜索页计数器读不到不等于积分没涨：改从 Rewards 服务端取当前积分。
                    self.log(f"[{index}/{total}] 搜索页右上角未读到积分计数器，改用 Rewards 服务端查询。", "warning")
                    current_points = self._sync_account_points()
                    points_source = "rewards_server" if current_points is not None else "unavailable"
                increased = False
                if current_points is not None:
                    if last_points is None or current_points > last_points:
                        increased = True
                        no_increase_count = 0
                    elif current_points < last_points:
                        # 读数低于上一轮（来源间不同步）：采纳新值继续比较，不计入未上涨。
                        self._search_task_log({
                            "event": "points_baseline_resync",
                            "index": index,
                            "source": points_source,
                            "previous": last_points,
                            "current": current_points,
                        })
                    else:
                        no_increase_count += 1
                    last_points = current_points
                else:
                    # 计数器与服务端都拿不到积分：本轮不做判定，保持基线与计数不变。
                    self.log(f"[{index}/{total}] 无法获取当前积分，本轮跳过上涨判定。", "warning")
                gained = self._gained_points(baseline_points, current_points)
                self._search_task_log({
                    "event": "search",
                    "index": index,
                    "total": total,
                    "title": title,
                    "query": query,
                    "source": points_source,
                    "points_before": points_before,
                    "points_after": current_points,
                    "increased": increased,
                    "no_increase_count": no_increase_count,
                    "gained": gained,
                })
                gained_text = f"（+{gained}）" if gained is not None else ""
                self.emit(
                    "search_task_status",
                    state="searching",
                    message=f"[{index}/{total}] 已完成：积分 {current_points if current_points is not None else '--'}{gained_text}",
                    current=index,
                    total=total,
                    points=current_points,
                    gained=gained,
                )

                if index % SEARCH_CALIBRATION_EVERY == 0:
                    self.log(f"已完成 {index} 次搜索，进入 Rewards 详细页面同步校准。")
                    summary = self._calibrate_rewards_points(page, return_url=page.url)
                    last_points = summary.get("account_points") or last_points
                    no_increase_count = 0

                if no_increase_count >= SEARCH_NO_INCREASE_LIMIT:
                    if cooldown_count >= SEARCH_MAX_COOLDOWNS:
                        self.log("冷却后仍连续 3 次搜索积分未上涨，停止搜索并进入最终校准。", "warning")
                        stopped_due_to_no_increase = True
                        break
                    summary = self._cooldown_for_no_increase(page)
                    cooldown_count += 1
                    last_points = summary.get("account_points") or last_points
                    no_increase_count = 0

                if index < total:
                    wait_seconds = random.uniform(SEARCH_INTERVAL_MIN_SECONDS, SEARCH_INTERVAL_MAX_SECONDS)
                    self._wait_with_cancel(wait_seconds, message=f"随机等待 {wait_seconds:.0f} 秒后继续…")

            if stopped_due_to_no_increase:
                self.log("搜索任务提前停止，进入 Rewards 详细页面进行最终积分同步校准。", "warning")
            else:
                self.log("搜索任务结束，进入 Rewards 详细页面进行最终积分同步校准。")
            final_gained = self._gained_points(baseline_points, last_points)
            self.emit(
                "search_task_status",
                state="syncing",
                message="正在同步 Rewards 数据…",
                current=total,
                total=total,
                gained=final_gained,
            )
            self.fetch_rewards_data()
            final_message = "搜索任务完成，积分已同步校准" if not stopped_due_to_no_increase else "积分未继续增长，已停止并完成校准"
            if final_gained is not None:
                final_message = f"{final_message}，本次上涨积分 +{final_gained}"
            self.emit(
                "search_task_status",
                state="done",
                message=final_message,
                current=total,
                total=total,
                gained=final_gained,
            )
        except TaskCancelled:
            self.log("搜索任务已取消。", "warning")
            self._search_task_log({"event": "cancelled"})
            self.emit("search_task_status", state="cancelled", message="搜索任务已取消", current=0, total=0)
            self.emit("status", state="running", detail="搜索任务已取消")
        except Exception as exc:
            self.log(f"搜索任务失败: {exc}", "error")
            self._search_task_log({"event": "error", "message": str(exc)})
            self.emit("search_task_status", state="error", message=f"搜索任务失败: {exc}", current=0, total=0)
            self.emit("status", state="running", detail="搜索任务失败，请查看日志")
        finally:
            self._search_running = False
            self._active_flow = None

    def _find_incomplete_task(self, page: Page, section_name: str, processed: set[str], sequence: int) -> dict[str, Any]:
        try:
            result = page.evaluate(
                REWARDS_TASK_CLICK_SCRIPT,
                {"section": section_name, "processed": sorted(processed), "sequence": sequence},
            )
            return result if isinstance(result, dict) else {}
        except Exception as exc:
            self._search_task_log({"event": "task_scan_error", "section": section_name, "message": str(exc)})
            return {}

    def trigger_incomplete_tasks(self) -> None:
        if self._search_running:
            self.log("已有搜索或任务触发流程正在运行，请先等待或点击“取消任务”。", "warning")
            return
        self._search_running = True
        self._cancel_event.clear()
        self._active_flow = "daily"
        try:
            if self.context is None:
                self.emit("search_task_status", state="scanning", message="浏览器未运行，正在自动启动 Edge…", current=0, total=0)
            clicked_total = 0
            page = self._current_page()
            # 上涨积分基线 = 本轮任务开始时的账户积分；优先取 Rewards 页面实时读数，读不到再退回缓存。
            baseline_points: int | None = None
            current_points: int | None = None
            self.log("开始扫描“每日活动”和“日常任务”：未完成项将逐个点击，已完成项跳过。")
            sections = [("每日活动", REWARDS_DASHBOARD_URL), ("日常任务", REWARDS_EARN_URL)]
            for section_name, section_url in sections:
                self._check_cancel()
                self.emit("search_task_status", state="scanning", message=f"正在扫描{section_name}…", current=clicked_total, total=0)
                page.goto(section_url, wait_until="domcontentloaded", timeout=45_000)
                self._wait_for_rewards_ready(page)
                if baseline_points is None:
                    page_data = self._extract_rewards_page(page)
                    baseline_points = page_data.get("account_points") or None
                    if baseline_points is None:
                        baseline_points = self._cached_account_points()
                    if baseline_points is not None:
                        self.log(f"上涨积分基线（账户积分）：{baseline_points}。")
                processed: set[str] = set()
                sequence = 0
                first_summary = True
                while True:
                    self._check_cancel()
                    result = self._find_incomplete_task(page, section_name, processed, sequence)
                    if result.get("missing_section"):
                        self.log(f"{section_name}: 页面未找到该区块，跳过。", "warning")
                        break
                    if first_summary:
                        total = int(result.get("total") or 0)
                        completed = int(result.get("completed") or 0)
                        pending = int(result.get("pending") or 0)
                        self.log(f"{section_name}: 共 {total} 项，已完成 {completed} 项（跳过），未完成 {pending} 项（触发）。")
                        self.emit(
                            "search_task_status",
                            state="scanning",
                            message=f"{section_name}: 已完成 {completed} 项跳过，准备点击 {pending} 项",
                            current=clicked_total,
                            total=total,
                        )
                        first_summary = False
                    if not result.get("found"):
                        break

                    title = str(result.get("title") or "未命名任务").strip()
                    task_id = str(result.get("id") or "")
                    points = result.get("points")
                    self.emit(
                        "search_task_status",
                        state="clicking",
                        message=f"{section_name}: 点击未完成项 {title[:34]}",
                        current=clicked_total,
                        total=max(1, int(result.get("total") or 0)),
                    )
                    locator = page.locator(f'[data-codex-reward-click="{task_id}"]')
                    pages_before = {id(item) for item in (self.context.pages if self.context is not None else [])}
                    old_url = page.url
                    clicked = False
                    try:
                        locator.scroll_into_view_if_needed(timeout=3000)
                        locator.click(timeout=6000)
                        clicked = True
                    except Exception as exc:
                        self.log(f"{section_name}: 常规点击失败，尝试强制点击：{title} ({exc})", "warning")
                        try:
                            locator.click(timeout=6000, force=True)
                            clicked = True
                        except Exception:
                            clicked = False
                        if not clicked:
                            try:
                                clicked = bool(page.evaluate(
                                """(id) => {
                                    const element = document.querySelector('[data-codex-reward-click="' + id + '"]');
                                    if (!element) return false;
                                    element.click();
                                    return true;
                                }""",
                                    task_id,
                                ))
                            except Exception:
                                clicked = False

                    if not clicked:
                        self.log(f"{section_name}: 未完成项点击失败，停止该区块：{title}", "error")
                        self._search_task_log({"event": "task_click_failed", "section": section_name, "title": title})
                        break
                    processed.add(title)
                    page.wait_for_timeout(random.randint(2500, 4500))
                    new_pages = []
                    if self.context is not None:
                        for item in self.context.pages:
                            if id(item) not in pages_before and not item.is_closed():
                                try:
                                    item.wait_for_load_state("domcontentloaded", timeout=5000)
                                except Exception:
                                    pass
                                new_pages.append(item)
                    if new_pages:
                        for new_page in new_pages:
                            try:
                                new_url = new_page.url
                            except Exception:
                                new_url = ""
                            self.log(f"已触发未完成项：{title}；打开新标签页 {new_url}", "success")
                            try:
                                new_page.close()
                            except Exception:
                                pass
                    elif page.url != old_url:
                        self.log(f"已触发未完成项：{title}；页面跳转到 {page.url}", "success")
                    else:
                        self.log(f"已触发未完成项：{title}", "success")

                    clicked_total += 1
                    sequence += 1
                    # 每次触发后向 Rewards 服务端查询当前积分：上涨积分 = 当前积分 - 任务开始基线。
                    current_points = self._sync_account_points()
                    if baseline_points is None and current_points is not None:
                        baseline_points = current_points
                    gained = self._gained_points(baseline_points, current_points)
                    self._search_task_log({
                        "event": "task_trigger",
                        "section": section_name,
                        "title": title,
                        "points": points,
                        "clicked": clicked,
                        "sequence": sequence,
                        "gained": gained,
                    })
                    gained_text = f"（+{gained}）" if gained is not None else ""
                    self.emit(
                        "search_task_status",
                        state="clicking",
                        message=f"{section_name}: 已触发 {clicked_total} 项（已完成自动跳过）{gained_text}",
                        current=clicked_total,
                        total=max(clicked_total, int(result.get("total") or 0)),
                        gained=gained,
                    )

                    if clicked_total % 5 == 0:
                        self.log(f"已触发 {clicked_total} 项，进入 Rewards 详细页同步校准。")
                        summary = self._calibrate_rewards_points(page, return_url=section_url)
                        if baseline_points is None:
                            baseline_points = summary.get("account_points") or None

                    try:
                        page.goto(section_url, wait_until="domcontentloaded", timeout=45_000)
                        self._wait_for_rewards_ready(page)
                    except Exception as exc:
                        self.log(f"刷新 {section_name} 时出现问题，将重新打开页面：{exc}", "warning")
                        page.goto(section_url, wait_until="domcontentloaded", timeout=45_000)
                        self._wait_for_rewards_ready(page)

                    if clicked_total >= 30:
                        self.log("达到单轮最多 30 次触发限制，停止继续点击。", "warning")
                        break
                    if self._cancel_event.wait(random.uniform(6, 12)):
                        raise TaskCancelled()

            self.emit(
                "search_task_status",
                state="syncing",
                message="未完成任务触发结束，正在同步 Rewards 数据…",
                current=clicked_total,
                total=clicked_total,
                gained=self._gained_points(baseline_points, current_points),
            )
            if clicked_total > 0:
                self.fetch_rewards_data()
                self.log(f"未完成任务触发结束：共点击 {clicked_total} 项，已完成项均已跳过。", "success")
            else:
                self.log("没有发现需要点击的未完成任务。", "success")
                summary = self._calibrate_rewards_points(page)
                if baseline_points is None:
                    baseline_points = summary.get("account_points") or None
                current_points = summary.get("account_points") or current_points
            # 最终上涨积分以同步后的最新账户积分为准（fetch/calibrate 都会写回缓存）。
            final_points = self._cached_account_points()
            if final_points is None:
                final_points = current_points
            if baseline_points is None and final_points is not None:
                baseline_points = final_points
            final_gained = self._gained_points(baseline_points, final_points)
            done_message = f"未完成任务处理完成：点击 {clicked_total} 项"
            if final_gained is not None:
                done_message = f"{done_message}，本次上涨积分 +{final_gained}"
            self.emit(
                "search_task_status",
                state="done",
                message=done_message,
                current=clicked_total,
                total=clicked_total,
                gained=final_gained,
            )
        except TaskCancelled:
            self.log("未完成任务触发流程已取消。", "warning")
            self._search_task_log({"event": "task_trigger_cancelled", "clicked": clicked_total})
            self.emit("search_task_status", state="cancelled", message="未完成任务触发已取消", current=clicked_total, total=clicked_total)
            self.emit("status", state="running", detail="未完成任务触发已取消")
        except Exception as exc:
            self.log(f"未完成任务触发失败: {exc}", "error")
            self._search_task_log({"event": "task_trigger_error", "clicked": clicked_total, "message": str(exc)})
            self.emit("search_task_status", state="error", message=f"未完成任务触发失败: {exc}", current=clicked_total, total=clicked_total)
            self.emit("status", state="running", detail="未完成任务触发失败，请查看日志")
        finally:
            self._search_running = False
            self._active_flow = None

    def _collect_page_snapshot(self, page: Page) -> PageSnapshot:
        try:
            local_keys = int(page.evaluate("() => Object.keys(window.localStorage).length"))
        except Exception:
            local_keys = 0
        try:
            session_keys = int(page.evaluate("() => Object.keys(window.sessionStorage).length"))
        except Exception:
            session_keys = 0
        try:
            cookie_names = page.evaluate(
                "() => document.cookie.split(';').map(v => v.trim().split('=')[0]).filter(Boolean)"
            )
            cookie_names = [str(item) for item in cookie_names]
        except Exception:
            cookie_names = []
        try:
            title = page.title()
        except Exception:
            title = ""
        return PageSnapshot(
            url=page.url,
            title=title,
            localStorage_keys=local_keys,
            sessionStorage_keys=session_keys,
            document_cookie_names=cookie_names,
        )

    def save_state_snapshot(self) -> None:
        if self.context is None:
            self.start_browser()
        cookies = self.context.cookies()
        cookie_summary = [
            {
                "name": cookie.get("name"),
                "domain": cookie.get("domain"),
                "path": cookie.get("path"),
                "expires": cookie.get("expires"),
                "httpOnly": cookie.get("httpOnly"),
                "secure": cookie.get("secure"),
                "sameSite": cookie.get("sameSite"),
            }
            for cookie in cookies
        ]
        pages = [self._collect_page_snapshot(page) for page in self.context.pages if not page.is_closed()]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = self.state_dir / f"state_{timestamp}.json"
        payload = {
            "created_at": now_text(),
            "browser": "Microsoft Edge",
            "profile_dir": str(self.profile_dir),
            "cache_breakdown": profile_breakdown(self.profile_dir, self.download_dir),
            "cookie_count": len(cookies),
            "cookie_metadata": cookie_summary,
            "pages": [snapshot.__dict__ for snapshot in pages],
            "note": "此文件只保存状态元数据，Cookies 的值仍保存在 Edge 资料目录内。",
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._append_operation({"event": "state_snapshot", "path": str(target), "cookie_count": len(cookies), "page_count": len(pages)})
        self.log(f"状态快照已保存: {target}", "success")
        self.emit("status", state="running", detail="状态快照已保存")
        self.emit("snapshot_saved", path=str(target))

    def publish_stats(self) -> None:
        breakdown = profile_breakdown(self.profile_dir, self.download_dir)
        cookie_count = count_cookies_database(self.profile_dir, self.runtime_dir)
        if self.context is not None:
            try:
                cookie_count = len(self.context.cookies())
            except Exception:
                pass
        data = {
            "profile_size": breakdown.get("全部用户数据", 0),
            "cache_size": breakdown.get("HTTP 缓存", 0),
            "site_data_size": breakdown.get("站点数据", 0),
            "cookie_size": breakdown.get("Cookies", 0),
            "history_size": breakdown.get("历史记录", 0),
            "removable_cache": breakdown.get("可回收缓存", 0),
            "cookie_count": cookie_count,
            "history_count": count_history_entries(self.profile_dir, self.runtime_dir),
            "page_count": self._page_count(),
            "profile_dir": str(self.profile_dir),
            "breakdown": breakdown,
        }
        self.emit("stats", data=data)

    def restart_browser(self) -> None:
        """重启浏览器：先取消任务并停止，再按当前窗口模式启动，使隐藏/显示切换立即生效。"""
        self.log("正在重启 Microsoft Edge：先停止当前浏览器与任务，再重新启动。", "info")
        self.cancel_search_task()
        self.stop_browser()
        self.start_browser()

    def stop_browser(self) -> None:
        context = self.context
        self.context = None
        if context is not None:
            self._context_closing = True
            try:
                context.close()
            except Exception:
                pass
            finally:
                self._context_closing = False
            self.log("Microsoft Edge 已停止，所有缓存和浏览数据已写入磁盘。如需继续浏览，可点击「启动浏览器」。", "success")
            self._append_operation({"event": "browser_stop"})
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        self.bound_pages.clear()
        self.emit("status", state="stopped", detail="Microsoft Edge 未运行")
        self.emit("page_count", count=0)
    def _safe_remove_inside_profile(self, path: Path) -> bool:
        try:
            base = self.profile_dir.resolve()
            target = path.resolve()
            if target.is_symlink():
                return False
            if target != base and base not in target.parents:
                return False
            if not target.exists():
                return False
            if target.is_dir():
                shutil.rmtree(target)
            elif target.is_file():
                target.unlink(missing_ok=True)
            else:
                return False
            return True
        except OSError:
            return False

    def clean_browser_cache(self) -> None:
        if self.context is not None:
            self.log("清理缓存前先停止 Microsoft Edge。", "warning")
            self.stop_browser()
        breakdown_before = profile_breakdown(self.profile_dir, self.download_dir)
        before = breakdown_before.get("全部用户数据", 0)
        removable_before = breakdown_before.get("可回收缓存", 0)
        removed: list[str] = []
        for relative in SAFE_CACHE_RELATIVE_PATHS:
            path = self.profile_dir / relative
            if self._safe_remove_inside_profile(path):
                removed.append(relative)
        breakdown_after = profile_breakdown(self.profile_dir, self.download_dir)
        after = breakdown_after.get("全部用户数据", 0)
        freed = max(0, before - after)
        self.log(
            f"安全缓存清理完成：释放 {format_bytes(freed)}，"
            f"用户数据 {format_bytes(before)} → {format_bytes(after)}。"
            f"登录、Cookies、历史、LocalStorage、IndexedDB 和 Service Worker 均保留。",
            "success",
        )
        self._append_operation({
            "event": "cache_cleanup",
            "before": before,
            "after": after,
            "freed": freed,
            "removable_before": removable_before,
            "removed": removed,
        })
        self.emit(
            "cache_cleaned",
            data={"before": before, "after": after, "freed": freed, "removed": removed},
        )
        self.publish_stats()
        self.emit("status", state="stopped", detail="缓存已清理，Microsoft Edge 未运行；可点击「启动浏览器」继续")


