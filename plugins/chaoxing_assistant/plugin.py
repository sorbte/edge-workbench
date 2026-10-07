"""学习通助手插件页：独立进程运行的学习通个人空间刷新、在读课程、单课程自动学习与页面批改菜单。

页头排版与命名对齐微软积分助手：
- 标题行右侧为整页唯一一组图标按钮（刷新全部数据 / 清理缓存 / 批改作业开关 / 打开缓存 JSON / 切换窗口显示）；
- 操作行左侧仅保留「打开个人空间」；「开始学习 / 取消任务」下沉到在读课程卡片内，
  按课程发起，同一账号同一时间只允许一个任务（一门课程学习），避免同时播放触发验证码；
- 「批改作业」是工具栏上的开关（不占任务闸门）：开启后独立 Edge 每个页面右上角出现
  「AI 批改助手」菜单，用户打开作业/章节测验页面点「开始批改」，AI 逐题作答并在题目
  下方批注参考答案、解释与「你的答案」状态；只批注，不自动选择或填写，绝不提交；
- 工具栏齿轮按钮打开页内「学习通设置」视图（顶部「← 返回」）：批改 Agent 选择与批改菜单行为说明；
- 运行状态统一词汇：未启动 / 启动中 / 运行中 / 就绪 / 等待登录 / 已完成 / 取消中 / 已取消 / 异常。
"""

from __future__ import annotations

import json
import os
import tkinter as tk
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

from edge_workbench import (
    COLORS,
    ICON_CLEAN,
    ICON_GRADE,
    ICON_OPEN_FILE,
    ICON_REFRESH,
    ICON_SETTINGS,
    ICON_WINDOW,
    WorkbenchPlugin,
    ai_manager,
    mono_font,
    now_text,
    sans_font,
)


class ChaoxingAssistantPlugin(WorkbenchPlugin):
    """学习通个人空间数据同步与在读课程总览。"""

    FILTERS = (("all", "全部"), ("todo", "未完成"), ("done", "已完成"))

    def __init__(self, manifest: dict[str, Any], app: Any) -> None:
        super().__init__(manifest, app)
        self.profile_file = self.runtime_dir / "profile.json"
        self.courses_file = self.runtime_dir / "courses.json"
        self.auth_file = self.runtime_dir / "auth_state.json"
        self.study_report_file = self.runtime_dir / "study_report.json"
        self.ai_settings_file = self.runtime_dir / "ai_settings.json"
        self._ai_settings = self._load_ai_settings()
        self._ai_agents: list[dict[str, Any]] = []
        self._ai_agent_options: dict[str, str] = {}
        self._profile: dict[str, Any] = {}
        self._courses: list[dict[str, Any]] = []
        self._course_filter = "all"
        self._sync_running = False

    def build_pages(self, parent: Any) -> list[tuple[str, str, Any]]:
        """构建学习通助手页面，并在启动时自动读取缓存、触发同步。"""
        self._profile_name_var = tk.StringVar(value="未获取用户信息")
        self._profile_id_var = tk.StringVar(value="用户 ID：--")
        self._profile_meta_var = tk.StringVar(value="登录后同步个人空间数据")
        self._last_sync_var = tk.StringVar(value="尚未同步")
        self._stat_total_var = tk.StringVar(value="--")
        self._stat_done_var = tk.StringVar(value="--")
        self._stat_todo_var = tk.StringVar(value="--")
        self._stat_progress_var = tk.StringVar(value="--")
        self._course_count_var = tk.StringVar(value="暂无课程")
        self._empty_reason_var = tk.StringVar(value="尚未同步学习通数据。首次使用会打开独立 Edge，请使用“学习通”App 扫描二维码登录；同步后展示全部在读课程（历史课程不采集）。")
        self._runtime_state_var = tk.StringVar(value="未启动")
        self._runtime_detail_var = tk.StringVar(value="独立进程尚未启动；启动后使用专用 Edge 资料目录。")
        self._runtime_process_var = tk.StringVar(value="进程: 未启动")
        self._profile_label_var = tk.StringVar(value=f"资料: {self.profile_dir.name}")
        self._browser_window_visible = not self.runtime.headless
        self._browser_running = False
        # 在读课程卡片内的任务按钮登记表：key = 课程标识，随课程列表重建而刷新
        self.course_study_buttons: dict[str, ctk.CTkButton] = {}
        self.course_cancel_buttons: dict[str, ctk.CTkButton] = {}
        self.course_open_buttons: dict[str, ctk.CTkButton] = {}
        # 页面批改菜单开关状态（与 worker 侧/设置文件三方同步）
        self._ai_menu_enabled = bool(self._ai_settings.get("menu_enabled"))

        page = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        # 主视图 / 设置视图在同一页内切换（设置入口为工具栏齿轮按钮，不占侧栏导航）
        self._main_view = ctk.CTkFrame(page, fg_color="transparent", corner_radius=0)
        self._main_view.pack(fill="both", expand=True)
        self._build_header(self._main_view)
        body = ctk.CTkFrame(self._main_view, fg_color="transparent", corner_radius=0)
        body.pack(fill="both", expand=True)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self._build_empty_state(body)
        self.data_frame = ctk.CTkScrollableFrame(
            body,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color="#e7e7e4",
            scrollbar_button_hover_color="#d6d6d2",
        )
        self.data_frame.grid(row=0, column=0, sticky="nsew")
        scrollbar = getattr(self.data_frame, "_scrollbar", None)
        if scrollbar is not None:
            scrollbar.grid_remove()

        self._build_summary_card()
        self._build_course_section()
        self._show_state("empty")
        self._load_cache()
        # 设置视图：学习通助手专属（批改 Agent、批改菜单说明），构建后默认隐藏
        self._build_settings_view(page)
        return [("chaoxing", str(self.manifest.get("name") or "学习通助手"), page)]

    # ------------------------------------------------------------------ 页面构建

    def _build_header(self, page: Any) -> None:
        header = ctk.CTkFrame(page, fg_color="transparent", corner_radius=0)
        header.pack(fill="x", padx=24, pady=(18, 10))

        # 标题行：页面标题 + 独立进程运行状态（与微软积分助手同款词汇）
        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="x")
        title_cluster = ctk.CTkFrame(title_row, fg_color="transparent")
        title_cluster.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_cluster,
            text="学习通助手",
            text_color=COLORS["text"],
            font=sans_font(20, "bold"),
        ).pack(side="left")
        status_inline = ctk.CTkFrame(title_cluster, fg_color="transparent")
        status_inline.pack(side="left", padx=(10, 0), pady=(2, 0))
        self._runtime_dot = ctk.CTkLabel(status_inline, text="●", text_color=COLORS["muted"], font=("Segoe UI", 12))
        self._runtime_dot.pack(side="left", padx=(0, 5))
        self._runtime_state_label = ctk.CTkLabel(
            status_inline,
            textvariable=self._runtime_state_var,
            text_color=COLORS["muted"],
            font=sans_font(11, "bold"),
        )
        self._runtime_state_label.pack(side="left")
        ctk.CTkLabel(
            title_row,
            text="个人空间基本信息与在读课程",
            text_color=COLORS["muted"],
            font=sans_font(11),
        ).pack(side="right", padx=(16, 0), pady=(7, 0))

        # 进程行：独立进程 PID + 插件资料目录按钮（左），运行详情（右）
        meta_row = ctk.CTkFrame(header, fg_color="transparent")
        meta_row.pack(fill="x", pady=(5, 0))
        meta_row.grid_columnconfigure(0, weight=1)
        meta_left = ctk.CTkFrame(meta_row, fg_color="transparent")
        meta_left.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            meta_left,
            textvariable=self._runtime_process_var,
            text_color=COLORS["muted"],
            font=sans_font(10),
            anchor="w",
        ).pack(side="left")
        self._profile_button = ctk.CTkButton(
            meta_left,
            textvariable=self._profile_label_var,
            command=self.open_profile_directory,
            width=0,
            height=20,
            corner_radius=4,
            fg_color="transparent",
            hover_color="#ececea",
            text_color=COLORS["accent"],
            font=sans_font(10, "bold"),
            anchor="w",
        )
        self._profile_button.pack(side="left", padx=(6, 0))
        self.app._attach_tooltip(self._profile_button, "点击打开插件资料目录")
        ctk.CTkLabel(
            meta_row,
            textvariable=self._runtime_detail_var,
            text_color=COLORS["muted"],
            font=sans_font(10),
            anchor="e",
        ).grid(row=0, column=1, sticky="e", padx=(16, 0))

        # 操作行独占一行：「打开个人空间」在左，数据图标在右（刷新/清理/打开 JSON/窗口切换）
        action_card = ctk.CTkFrame(page, fg_color="transparent", corner_radius=0)
        action_card.pack(fill="x", padx=24, pady=(0, 8))
        self.browser_actions = ctk.CTkFrame(action_card, fg_color="transparent")
        self.browser_actions.pack(side="left", pady=10)

        self.open_space_button = self.app._button(self.browser_actions, "打开个人空间", self.open_space, "secondary", 115)
        self.open_space_button.pack(side="left")

        data_actions = ctk.CTkFrame(action_card, fg_color="transparent")
        data_actions.pack(side="right", pady=10)
        self.toolbar = data_actions
        self.sync_button = self.app._icon_button(
            data_actions,
            ICON_REFRESH,
            self.start_refresh,
            "刷新全部数据：打开独立 Edge 读取个人空间与在读课程",
        )
        self.sync_button.pack(side="left", padx=(0, 6))
        self.clear_button = self.app._icon_button(data_actions, ICON_CLEAN, self.clear_cache, "清理本地缓存（任务运行中会跳过）")
        self.clear_button.pack(side="left", padx=(0, 6))
        self.ai_menu_button = self.app._icon_button(
            data_actions,
            ICON_GRADE,
            self.toggle_ai_menu,
            "开启/关闭页面批改作业菜单：在网页题目下方批注 AI 参考答案（不自动填写、不提交，不占用任务）",
        )
        self.ai_menu_button.pack(side="left", padx=(0, 6))
        self.open_json_button = self.app._icon_button(data_actions, ICON_OPEN_FILE, self.open_data_file, "打开课程缓存 JSON")
        self.open_json_button.pack(side="left", padx=(0, 6))
        self.browser_window_button = self.app._icon_button(data_actions, ICON_WINDOW, self.toggle_browser_window, "切换独立 Edge 窗口显示（黑色=后台运行）")
        self.browser_window_button.pack(side="left")
        self.settings_button = self.app._icon_button(
            data_actions,
            ICON_SETTINGS,
            self.open_settings_view,
            "学习通助手设置：批改 Agent 与页面批改菜单说明",
        )
        self.settings_button.pack(side="left", padx=(6, 0))
        self._sync_browser_window_button()
        self._sync_ai_menu_button()

        self.app._bind_busy("chaoxing_refresh", self.sync_button)
        self.app._bind_busy("chaoxing_open_space", self.open_space_button)

    def _build_empty_state(self, body: Any) -> None:
        self.empty_frame = ctk.CTkFrame(body, fg_color="transparent", corner_radius=0)
        self.empty_frame.grid(row=0, column=0, sticky="nsew")
        self.empty_frame.grid_rowconfigure(0, weight=1)
        self.empty_frame.grid_columnconfigure(0, weight=1)
        center = ctk.CTkFrame(self.empty_frame, fg_color="transparent")
        center.grid(row=0, column=0)
        ctk.CTkLabel(center, text="▣", text_color=COLORS["accent"], font=mono_font(38)).pack()
        ctk.CTkLabel(center, text="暂无在读课程数据", text_color=COLORS["text"], font=sans_font(15, "bold")).pack(pady=(14, 8))
        ctk.CTkLabel(
            center,
            textvariable=self._empty_reason_var,
            justify="center",
            wraplength=580,
            text_color=COLORS["muted"],
            font=sans_font(11),
        ).pack()
        ctk.CTkLabel(
            center,
            text="① 点击「获取基本数据」    ② 在独立 Edge 中使用“学习通”App 扫码登录    ③ 自动读取在读课程与学习进度",
            text_color=COLORS["muted"],
            font=sans_font(10),
        ).pack(pady=(10, 16))
        empty_actions = ctk.CTkFrame(center, fg_color="transparent")
        empty_actions.pack()
        self.empty_sync_button = self.app._button(empty_actions, "获取基本数据", self.start_refresh, "primary", 140)
        self.empty_sync_button.pack(side="left")
        self.empty_cancel_button = self.app._button(empty_actions, "取消任务", self.cancel_refresh, "danger", 100)
        self.empty_cancel_button.configure(state="disabled")
        self.empty_cancel_button.pack(side="left", padx=(8, 0))
        self.app._bind_busy("chaoxing_refresh", self.empty_sync_button)

    def _show_state(self, state: str) -> None:
        """在空状态与数据态之间切换，布局与微软积分助手保持一致。"""
        if state == "empty":
            self.empty_frame.grid()
            self.data_frame.grid_remove()
            self._set_toolbar_visible(False)
        else:
            self.data_frame.grid()
            self.empty_frame.grid_remove()
            self._set_toolbar_visible(True)

    def _set_toolbar_visible(self, visible: bool) -> None:
        toolbar = getattr(self, "toolbar", None)
        browser_actions = getattr(self, "browser_actions", None)
        if toolbar is None or browser_actions is None:
            return
        if visible:
            browser_actions.pack(side="left", pady=10)
            toolbar.pack(side="right", pady=10)
        else:
            toolbar.pack_forget()
            browser_actions.pack_forget()

    def _set_task_buttons_state(self, state: str) -> None:
        """任务按钮联动：任务运行时禁用各任务入口、放开「取消任务」，空闲时相反。

        课程卡片内的「开始学习 / 取消任务」随课程列表重建，这里统一按当前登记表同步；
        同一账号同一时间只允许一个任务，因此所有「开始学习」一起禁用。
        """
        buttons: list[ctk.CTkButton | None] = [
            getattr(self, "sync_button", None),
            getattr(self, "open_space_button", None),
            getattr(self, "empty_sync_button", None),
        ]
        buttons.extend(getattr(self, "course_study_buttons", {}).values())
        buttons.extend(getattr(self, "course_open_buttons", {}).values())
        for button in buttons:
            if button is not None:
                try:
                    button.configure(state=state)
                except tk.TclError:
                    pass
        cancel_state = "normal" if state == "disabled" else "disabled"
        cancels: list[ctk.CTkButton | None] = [getattr(self, "empty_cancel_button", None)]
        cancels.extend(getattr(self, "course_cancel_buttons", {}).values())
        for cancel in cancels:
            if cancel is not None:
                try:
                    cancel.configure(state=cancel_state)
                except tk.TclError:
                    pass

    def _set_runtime_state(self, state: str, detail: str = "") -> None:
        """更新本插件页头自己的独立进程运行状态（词汇与微软积分助手一致）。"""
        mapping = {
            "idle": ("未启动", COLORS["muted"]),
            "stopped": ("未启动", COLORS["muted"]),
            "starting": ("启动中", COLORS["warning"]),
            "running": ("运行中", COLORS["success"]),
            "ready": ("就绪", COLORS["accent"]),
            "waiting_login": ("等待登录", COLORS["warning"]),
            "done": ("已完成", COLORS["success"]),
            "cancelling": ("取消中", COLORS["warning"]),
            "cancelled": ("已取消", COLORS["warning"]),
            "error": ("异常", COLORS["danger"]),
        }
        label, color = mapping.get(str(state), ("运行中", COLORS["success"]))
        try:
            self._runtime_state_var.set(label)
            self._runtime_dot.configure(text_color=color)
            self._runtime_state_label.configure(text_color=color)
            if detail:
                self._runtime_detail_var.set(detail)
            pid = self.runtime.process_id
            self._runtime_process_var.set(f"进程: {pid or '未启动'}")
            self._profile_label_var.set(f"资料: {self.profile_dir.name}")
        except tk.TclError:
            pass

    def _sync_browser_window_button(self) -> None:
        button = getattr(self, "browser_window_button", None)
        if button is None:
            return
        button.configure(text=ICON_WINDOW)
        if self._browser_window_visible:
            button.configure(
                fg_color="#f2f2f0",
                hover_color="#e7e7e4",
                text_color=COLORS["text"],
            )
        else:
            button.configure(
                fg_color=COLORS["accent"],
                hover_color=COLORS["accent_hover"],
                text_color="#ffffff",
            )

    def sync_browser_window_visibility(self, visible: bool) -> None:
        """与总览页的浏览器窗口显示设置保持同步。"""
        self._browser_window_visible = bool(visible)
        self._sync_browser_window_button()

    def toggle_browser_window(self) -> None:
        """独立切换启动时是否显示 Edge 窗口；浏览器运行中时自动重启立即生效。"""
        visible = not self._browser_window_visible
        self.app.set_browser_window_visible(visible)
        if not self._browser_running or self.runtime.current_command == "restart":
            return  # 浏览器未运行时下次启动生效；自动重启已在进行中则忽略连点
        self._runtime_detail_var.set("窗口显示模式已切换，正在自动重启独立浏览器…")
        self.log("窗口显示模式已切换，正在自动重启独立浏览器…", "info")
        self.runtime.cancel()
        self.submit("restart")

    def _build_summary_card(self) -> None:
        """用户信息与在读课程统计合并为一张 ACCOUNT 卡（排版对齐微软积分助手）。"""
        card = self.app._card(self.data_frame, padded=True)
        label_row = ctk.CTkFrame(card, fg_color="transparent")
        label_row.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(
            label_row,
            text="ACCOUNT / 用户与在读课程",
            text_color=COLORS["muted"],
            font=sans_font(11, "bold"),
            anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            label_row,
            textvariable=self._last_sync_var,
            text_color=COLORS["muted"],
            font=mono_font(10),
            anchor="e",
        ).pack(side="right")

        ctk.CTkLabel(
            card,
            textvariable=self._profile_name_var,
            text_color=COLORS["text"],
            font=sans_font(17, "bold"),
            anchor="w",
        ).pack(fill="x", padx=16)
        ctk.CTkLabel(
            card,
            textvariable=self._profile_id_var,
            text_color=COLORS["muted"],
            font=mono_font(11),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(3, 0))
        ctk.CTkLabel(
            card,
            textvariable=self._profile_meta_var,
            text_color=COLORS["muted"],
            font=sans_font(10),
            anchor="w",
            wraplength=900,
            justify="left",
        ).pack(fill="x", padx=16, pady=(3, 0))

        separator = ctk.CTkFrame(card, height=1, fg_color=COLORS["border"])
        separator.pack(fill="x", padx=16, pady=(12, 0))

        # 统计区必须与卡内其他行一样留出 16px 内边距：透明子框架若铺到卡片
        # 边缘，会用底色盖住卡片 1px 描边，看起来左右边框“消失”。
        stats = ctk.CTkFrame(card, fg_color="transparent")
        stats.pack(fill="x", padx=16, pady=(4, 12))
        for column in range(4):
            stats.grid_columnconfigure(column, weight=1, uniform="chaoxing-stat")
        self._stat_cell(stats, "在读课程", self._stat_total_var, 0, COLORS["text"])
        self._stat_cell(stats, "已完成", self._stat_done_var, 1, COLORS["success"])
        self._stat_cell(stats, "未完成", self._stat_todo_var, 2, COLORS["warning"])
        self._stat_cell(stats, "平均进度", self._stat_progress_var, 3, COLORS["text"])

    def _stat_cell(self, parent: Any, title: str, variable: tk.StringVar, column: int, value_color: str) -> None:
        ctk.CTkLabel(parent, text=title, text_color=COLORS["muted"], font=sans_font(11)).grid(
            row=0, column=column, sticky="w", padx=(0, 8)
        )
        ctk.CTkLabel(parent, textvariable=variable, text_color=value_color, font=mono_font(20, "bold")).grid(
            row=1, column=column, sticky="w", padx=(0, 8), pady=(2, 0)
        )

    def _build_course_section(self) -> None:
        section = ctk.CTkFrame(self.data_frame, fg_color="transparent")
        section.pack(fill="x")
        # 标题与筛选按钮与课程卡片（24px 边距）对齐，不再向左右突出
        title_row = ctk.CTkFrame(section, fg_color="transparent")
        title_row.pack(fill="x", padx=24, pady=(0, 8))
        ctk.CTkLabel(
            title_row,
            text="COURSES / 在读课程",
            text_color=COLORS["muted"],
            font=sans_font(11, "bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            title_row,
            textvariable=self._course_count_var,
            text_color=COLORS["muted"],
            font=mono_font(10),
        ).pack(side="right")

        filters = ctk.CTkFrame(section, fg_color="transparent")
        filters.pack(fill="x", padx=24, pady=(0, 12))
        self.filter_buttons: dict[str, ctk.CTkButton] = {}
        for key, label in self.FILTERS:
            button = self.app._button(filters, label, lambda value=key: self._set_filter(value), "secondary", 68)
            button.pack(side="left", padx=(0, 6))
            self.filter_buttons[key] = button
        self._sync_filter_buttons()

        self.course_container = ctk.CTkFrame(section, fg_color="transparent")
        self.course_container.pack(fill="x")

    # ------------------------------------------------------------------ 缓存与状态


    def _load_cache(self) -> None:
        if self.profile_file.exists():
            try:
                payload = json.loads(self.profile_file.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    self._profile = payload
            except Exception:
                self._profile = {}
        if self.courses_file.exists():
            try:
                payload = json.loads(self.courses_file.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    courses = payload.get("courses")
                    if isinstance(courses, list):
                        # 旧版本缓存可能包含历史课程，这里统一只保留在读课程
                        self._courses = [
                            item for item in courses
                            if isinstance(item, dict) and item.get("is_current")
                        ]
                    if not self._profile and isinstance(payload.get("profile"), dict):
                        self._profile = payload["profile"]
            except Exception:
                pass
        self._refresh_ui()
        if self._profile or self._courses:
            self._set_runtime_state("idle", "已读取本机缓存；点击右上角「刷新全部数据」图标才会启动独立 Edge 并刷新数据。")
        else:
            self._set_runtime_state("idle", "不会自动启动；点击「获取基本数据」后才会打开独立 Edge 并引导扫码登录。")

    def _refresh_ui(self) -> None:
        profile = self._profile if isinstance(self._profile, dict) else {}
        name = str(profile.get("display_name") or "").strip() or "未获取用户信息"
        user_id = str(profile.get("user_id") or "").strip()
        platform = str(profile.get("platform_title") or "浙江财经大学继续教育学院综合管理平台").strip()
        fetched_at = str(profile.get("fetched_at") or "").strip()
        self._profile_name_var.set(name)
        self._profile_id_var.set(f"用户 ID：{user_id or '--'}")
        auth_text = " · 登录 Cookie 快照已保存" if self.auth_file.exists() else ""
        self._profile_meta_var.set(f"{platform} · 数据仅缓存在本机插件资料目录{auth_text}")
        if fetched_at:
            self._last_sync_var.set(f"上次同步 {fetched_at}")

        total = len(self._courses)
        done = sum(1 for course in self._courses if self._course_done(course))
        todo = total - done
        progress_values = [
            float(course["overall_progress"])
            for course in self._courses
            if isinstance(course.get("overall_progress"), (int, float))
        ]
        average = round(sum(progress_values) / len(progress_values), 1) if progress_values else None
        self._stat_total_var.set(str(total))
        self._stat_done_var.set(str(done))
        self._stat_todo_var.set(str(todo))
        self._stat_progress_var.set(f"{average:.1f}%" if average is not None else "--")
        self._course_count_var.set(f"共 {total} 门在读 · {todo} 门未完成" if total else "暂无课程")
        self._rebuild_course_list()
        self._show_state("data" if (self._profile or self._courses) else "empty")

    @staticmethod
    def _course_done(course: dict[str, Any]) -> bool:
        """与 worker 侧判定保持一致：整体进度达标，或任务点/测验全部完成。"""
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

    def _filtered_courses(self) -> list[dict[str, Any]]:
        courses = list(self._courses)
        if self._course_filter == "done":
            courses = [course for course in courses if self._course_done(course)]
        elif self._course_filter == "todo":
            courses = [course for course in courses if not self._course_done(course)]

        def sort_key(course: dict[str, Any]) -> tuple[int, float, str]:
            progress = course.get("overall_progress")
            return (
                1 if self._course_done(course) else 0,
                float(progress) if isinstance(progress, (int, float)) else 999.0,
                str(course.get("title") or ""),
            )

        courses.sort(key=sort_key)
        return courses

    def _set_filter(self, value: str) -> None:
        self._course_filter = value if value in {key for key, _ in self.FILTERS} else "all"
        self._sync_filter_buttons()
        self._rebuild_course_list()

    def _sync_filter_buttons(self) -> None:
        for key, button in getattr(self, "filter_buttons", {}).items():
            active = key == self._course_filter
            button.configure(
                fg_color=COLORS["accent"] if active else "#f2f2f0",
                hover_color=COLORS["accent_hover"] if active else "#e7e7e4",
                text_color="#ffffff" if active else COLORS["text"],
            )

    def _rebuild_course_list(self) -> None:
        container = getattr(self, "course_container", None)
        if container is None:
            return
        for child in container.winfo_children():
            child.destroy()
        # 卡片按钮随列表整体重建，先清空登记表再由 _build_course_card 重新登记
        self.course_study_buttons.clear()
        self.course_cancel_buttons.clear()
        self.course_open_buttons.clear()
        courses = self._filtered_courses()
        if not courses:
            text = "暂未同步到在读课程，点击「刷新全部数据」开始获取。" if not self._courses else "当前筛选条件下没有课程。"
            row = ctk.CTkFrame(container, fg_color="transparent")
            row.pack(fill="x", padx=24, pady=28)
            ctk.CTkLabel(
                row,
                text=text,
                text_color=COLORS["muted"],
                font=sans_font(11),
            ).pack(side="left")
            # 该状态下没有课程卡片可承载「取消任务」，任务运行时在此补一个取消入口
            if self._sync_running:
                cancel_button = self.app._button(row, "取消任务", self.cancel_refresh, "danger", 88)
                cancel_button.pack(side="right")
                self.course_cancel_buttons["__empty__"] = cancel_button
                self.app._attach_tooltip(cancel_button, "取消当前正在运行的同步、学习或批改任务。")
        else:
            for course in courses:
                self._build_course_card(container, course)
        self._set_task_buttons_state("disabled" if self._sync_running else "normal")

    def _build_course_card(self, parent: Any, course: dict[str, Any]) -> None:
        done = self._course_done(course)
        course_key = self._course_key(course)
        card = self.app._card(parent, padded=True)
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(12, 4))
        title_box = ctk.CTkFrame(top, fg_color="transparent")
        title_box.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_box,
            text=str(course.get("title") or "未知课程"),
            text_color=COLORS["text"],
            font=sans_font(14, "bold"),
            anchor="w",
            wraplength=560,
            justify="left",
        ).pack(fill="x")
        tags = ctk.CTkFrame(title_box, fg_color="transparent")
        tags.pack(fill="x", pady=(5, 0))
        self._tag(tags, str(course.get("type") or "课程"), "#f2f2f0", COLORS["text"])
        self._tag(tags, str(course.get("term") or "未标注学期"), "#f2f2f0", COLORS["muted"])
        if done:
            self._tag(tags, "已完成", "#e8f7ee", "#15803d")
        else:
            self._tag(tags, "进行中", "#fdf3e7", "#b45309")

        # 卡片内任务按钮：针对单门课程开始学习 / 取消当前任务 / 打开课程页；
        # 已完成课程没有可学习的内容（不显示「开始学习」「取消任务」），只保留「回顾课程」；
        # 作业批改已改为工具栏的全局「批改作业」开关，不再按课程发起
        actions = ctk.CTkFrame(top, fg_color="transparent")
        actions.pack(side="right", padx=(14, 0), pady=(2, 0))
        if not done:
            study_button = self.app._button(
                actions,
                "开始学习",
                lambda item=course: self.start_course_study(item),
                "primary",
                92,
            )
            study_button.pack(side="left")
            self.course_study_buttons[course_key] = study_button
            self.app._attach_tooltip(study_button, "自动学习该课程的未完成章节；同一账号同一时间只能学习一门课程。")
            cancel_button = self.app._button(actions, "取消任务", self.cancel_refresh, "danger", 88)
            cancel_button.configure(state="disabled")
            cancel_button.pack(side="left", padx=(8, 0))
            self.course_cancel_buttons[course_key] = cancel_button
            self.app._attach_tooltip(cancel_button, "取消当前正在运行的同步、学习或批改任务。")
        open_button = self.app._button(actions, "回顾课程" if done else "打开课程", lambda item=course: self.open_course(item), "secondary", 96)
        open_button.pack(side="left", padx=(8, 0))
        if not course.get("course_url"):
            open_button.configure(state="disabled")
        self.course_open_buttons[course_key] = open_button

        progress = course.get("overall_progress")
        progress_row = ctk.CTkFrame(card, fg_color="transparent")
        progress_row.pack(fill="x", padx=16, pady=(7, 2))
        ctk.CTkLabel(
            progress_row,
            text="整体进度",
            text_color=COLORS["muted"],
            font=sans_font(10),
            width=60,
            anchor="w",
        ).pack(side="left")
        if isinstance(progress, (int, float)):
            bar = ctk.CTkProgressBar(
                progress_row,
                height=7,
                corner_radius=4,
                fg_color=COLORS["border"],
                progress_color=COLORS["success"] if done else COLORS["accent"],
            )
            bar.pack(side="left", fill="x", expand=True, padx=(4, 10))
            bar.set(max(0.0, min(1.0, float(progress) / 100.0)))
            ctk.CTkLabel(
                progress_row,
                text=f"{float(progress):.1f}%",
                text_color=COLORS["success"] if done else COLORS["text"],
                font=mono_font(10, "bold"),
                width=58,
                anchor="e",
            ).pack(side="right")
        else:
            score = course.get("score")
            score_text = f"总评成绩 {float(score):g}" if isinstance(score, (int, float)) else "无进度数据"
            ctk.CTkLabel(
                progress_row,
                text=score_text,
                text_color=COLORS["muted"],
                font=sans_font(10),
                anchor="w",
            ).pack(side="left", padx=(4, 0))

        detail_parts: list[str] = []
        if course.get("task_text"):
            detail_parts.append(f"章节任务点 {course['task_text']}")
        if course.get("quiz_text"):
            detail_parts.append(f"章节测验 {course['quiz_text']}")
        if not done:
            todo_bits: list[str] = []
            task_current, task_total = course.get("task_current"), course.get("task_total")
            if isinstance(task_current, (int, float)) and isinstance(task_total, (int, float)) and task_total > task_current:
                todo_bits.append(f"任务点 {int(task_total - task_current)}")
            quiz_current, quiz_total = course.get("quiz_current"), course.get("quiz_total")
            if isinstance(quiz_current, (int, float)) and isinstance(quiz_total, (int, float)) and quiz_total > quiz_current:
                todo_bits.append(f"测验 {int(quiz_total - quiz_current)}")
            if todo_bits:
                detail_parts.append(f"待完成：{'、'.join(todo_bits)}")
        if isinstance(course.get("score"), (int, float)):
            detail_parts.append(f"总评成绩 {float(course['score']):g}")
        if detail_parts:
            ctk.CTkLabel(
                card,
                text=" · ".join(detail_parts),
                text_color=COLORS["muted"] if done else COLORS["warning"],
                font=sans_font(10),
                anchor="w",
            ).pack(fill="x", padx=16, pady=(5, 0))

        assessment = str(course.get("assessment") or "").strip()
        if assessment:
            ctk.CTkLabel(
                card,
                text=assessment,
                text_color=COLORS["muted"],
                font=sans_font(10),
                anchor="w",
                wraplength=850,
                justify="left",
            ).pack(fill="x", padx=16, pady=(5, 12))
        else:
            ctk.CTkFrame(card, height=10, fg_color="transparent").pack()

    @staticmethod
    def _tag(parent: Any, text: str, background: str, foreground: str) -> None:
        ctk.CTkLabel(
            parent,
            text=text,
            fg_color=background,
            corner_radius=5,
            text_color=foreground,
            font=sans_font(9, "bold"),
            height=20,
        ).pack(side="left", padx=(0, 6))

    # ------------------------------------------------------------------ AI 作业批改

    def _load_ai_settings(self) -> dict[str, Any]:
        """读取批改 Agent 选择等本页设置（旧版字段忽略），损坏时回默认。"""
        try:
            payload = json.loads(self.ai_settings_file.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, ValueError):
            pass
        return {}

    def _build_settings_view(self, page: Any) -> None:
        """设置视图：学习通助手专属设置（批改 Agent、页面批改菜单说明）。

        与主视图在同一页内切换，构建后默认隐藏；顶部提供「← 返回」按钮。
        """
        view = ctk.CTkFrame(page, fg_color="transparent", corner_radius=0)
        self._settings_view = view
        header = ctk.CTkFrame(view, fg_color="transparent", corner_radius=0)
        header.pack(fill="x", padx=24, pady=(18, 10))
        back_button = self.app._button(header, "← 返回", self.back_to_main, "secondary", 88)
        back_button.pack(side="left")
        ctk.CTkLabel(
            header,
            text="学习通设置",
            text_color=COLORS["text"],
            font=sans_font(20, "bold"),
        ).pack(side="left", padx=(12, 0), pady=(2, 0))
        ctk.CTkLabel(
            header,
            text="批改 Agent 与页面批改菜单（学习通助手专属）",
            text_color=COLORS["muted"],
            font=sans_font(11),
        ).pack(side="right", padx=(16, 0), pady=(7, 0))
        content = ctk.CTkFrame(view, fg_color="transparent", corner_radius=0)
        content.pack(fill="both", expand=True)
        self._build_ai_settings_section(content)
        self._build_menu_help_section(content)

    def open_settings_view(self) -> None:
        """工具栏齿轮按钮：切换到设置视图。"""
        try:
            self._main_view.pack_forget()
            self._settings_view.pack(fill="both", expand=True)
        except tk.TclError:
            pass  # 页面可能已被热卸载

    def back_to_main(self) -> None:
        """设置视图顶部「← 返回」：回到主视图。"""
        try:
            self._settings_view.pack_forget()
            self._main_view.pack(fill="both", expand=True)
        except tk.TclError:
            pass  # 页面可能已被热卸载

    def _build_ai_settings_section(self, parent: Any) -> None:
        """「AGENT / 作业批改设置」卡：只选择批改用的 Agent（模型 / 提示词 / 工具都在「Agent 管理」页配置）。"""
        section = ctk.CTkFrame(parent, fg_color="transparent")
        section.pack(fill="x")
        title_row = ctk.CTkFrame(section, fg_color="transparent")
        title_row.pack(fill="x", padx=24, pady=(4, 8))
        ctk.CTkLabel(
            title_row, text="AGENT / 作业批改设置", text_color=COLORS["muted"], font=sans_font(11, "bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            title_row,
            text="只批注参考答案与解释 · 不自动选择或填写 · 不提交",
            text_color=COLORS["muted"], font=mono_font(10),
        ).pack(side="right")

        card = self.app._card(section, padded=True)
        agent_row = ctk.CTkFrame(card, fg_color="transparent")
        agent_row.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(
            agent_row, text="批改 Agent", text_color=COLORS["muted"], font=sans_font(11), width=64, anchor="w",
        ).pack(side="left")
        self._ai_agent_var = tk.StringVar(value="（暂无 Agent）")
        self._ai_agent_menu = ctk.CTkOptionMenu(
            agent_row,
            values=["（暂无 Agent）"],
            variable=self._ai_agent_var,
            width=300,
            height=32,
            fg_color="#f2f2f0",
            button_color="#e7e7e4",
            button_hover_color="#d6d6d2",
            text_color=COLORS["text"],
            font=sans_font(11),
            anchor="w",
            command=self._on_agent_selected,
        )
        self._ai_agent_menu.pack(side="left")
        refresh_button = self.app._icon_button(
            agent_row, ICON_REFRESH, self._refresh_agents, "刷新 Agent 列表（在「Agent 管理」页增删后点击）",
        )
        refresh_button.pack(side="left", padx=(8, 0))
        self._refresh_agents()
        ctk.CTkLabel(
            card,
            text="Agent 的模型、系统提示词与工具统一在「Agent 管理」页配置；此处选择即保存，批改请求直接使用该 Agent。",
            text_color=COLORS["muted"], font=mono_font(10), anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 12))

    def _build_menu_help_section(self, parent: Any) -> None:
        """「MENU / 页面批改菜单」说明卡：开关位置、菜单用法与批改行为。"""
        section = ctk.CTkFrame(parent, fg_color="transparent")
        section.pack(fill="x")
        title_row = ctk.CTkFrame(section, fg_color="transparent")
        title_row.pack(fill="x", padx=24, pady=(16, 8))
        ctk.CTkLabel(
            title_row, text="MENU / 页面批改菜单", text_color=COLORS["muted"], font=sans_font(11, "bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            title_row, text="开关在工作台工具栏（铅笔图标，位于「打开缓存 JSON」左侧）",
            text_color=COLORS["muted"], font=mono_font(10),
        ).pack(side="right")
        card = self.app._card(section, padded=True)
        lines = (
            "· 开启开关后，仅当页面是作业 / 章节测验（含 iframe 内嵌测验）时右上角出现「AI 批改助手」；其他页面不显示",
            "· 菜单可按住标题栏拖动到任意位置，单击标题折叠 / 展开；新开的测验页面会自动出现菜单",
            "· 点「开始批改」：AI 逐题作答，在每道题下方批注参考答案、一句话解释和你的作答状态，未作答的题用橙色标出",
            "· 只批注，不自动选择或填写、不提交；「重新批改」覆盖上一轮批注，「停止」中断当前页",
            "· 批改中关闭或跳转页面会安全中断本页，打开新的测验页面即可继续批改",
            "· 批改菜单不占用任务闸门：与「开始学习」「刷新全部数据」互不影响；任务运行期间菜单不刷新、批改不响应，任务结束后自动恢复",
        )
        for line in lines:
            ctk.CTkLabel(
                card,
                text=line,
                text_color=COLORS["muted"],
                font=sans_font(11),
                anchor="w",
                wraplength=880,
                justify="left",
            ).pack(fill="x", padx=16, pady=(3, 0))
        ctk.CTkFrame(card, height=10, fg_color="transparent").pack()

    def _refresh_agents(self) -> None:
        """把「Agent 管理」页的 Agent 同步到本页下拉框；默认 Agent 兜底。"""
        try:
            self._ai_agents = ai_manager.list_agents()
            self._ai_agent_options = {}
            values = []
            for agent in self._ai_agents:
                provider = ai_manager.get_provider(str(agent.get("provider_id") or ""))
                model_text = str(provider.get("model") or "--") if provider else "接入缺失"
                label = f"{agent.get('name') or agent.get('id')}（{model_text}）"
                values.append(label)
                self._ai_agent_options[label] = str(agent.get("id") or "")
            if not values:
                values = ["（暂无 Agent，请到「Agent 管理」页创建）"]
                self._ai_agent_options[values[0]] = ""
            self._ai_agent_menu.configure(values=values)
            selected_id = str(self._ai_settings.get("agent_id") or "")
            if selected_id and not ai_manager.get_agent(selected_id):
                selected_id = ""  # 已删除的 Agent 回落到默认
            if not selected_id:
                fallback = ai_manager.default_agent()
                selected_id = str(fallback.get("id") or "") if fallback else ""
            label_for_id = next((label for label, aid in self._ai_agent_options.items() if aid == selected_id), None)
            self._ai_agent_var.set(label_for_id or values[0])
        except tk.TclError:
            pass  # 页面可能已被热卸载

    def _save_ai_settings(self, **updates: Any) -> None:
        """合并保存本页设置（agent_id / menu_enabled），供 worker 侧批改时读取。"""
        payload = dict(self._ai_settings)
        payload.update(updates)
        payload["updated_at"] = now_text()
        try:
            self.ai_settings_file.parent.mkdir(parents=True, exist_ok=True)
            self.ai_settings_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self.log(f"保存批改设置失败：{exc}", "warning")
            return
        self._ai_settings = payload

    def _on_agent_selected(self, _label: str) -> None:
        """选择 Agent 即保存到本机设置；下次启动程序保持选择。"""
        agent_id = self._ai_agent_options.get(self._ai_agent_var.get(), "")
        self._save_ai_settings(agent_id=agent_id)
        self.log(f"作业批改 Agent 已设为「{self._ai_agent_var.get()}」。", "success")

    def toggle_ai_menu(self) -> None:
        """工具栏「批改作业」开关：开启后独立 Edge 页面右上角显示「AI 批改助手」菜单。

        不占用任务闸门、不需要任务：开关状态写入本机设置并发送轻量命令给 worker；
        菜单注入与批改命令由宿主空闲轮询处理，与「开始学习」等任务互不阻塞
        （任务运行期间开关命令会排队，任务结束后生效）。
        """
        enabled = not self._ai_menu_enabled
        self._ai_menu_enabled = enabled
        self._save_ai_settings(menu_enabled=enabled)
        self._sync_ai_menu_button()
        if enabled:
            self.log("已开启页面批改菜单：打开作业 / 章节测验页面，点右上角菜单「开始批改」。", "success")
        else:
            self.log("已关闭页面批改菜单。", "info")
        self.submit("chaoxing_ai_menu", enabled=enabled)

    def _sync_ai_menu_button(self) -> None:
        button = getattr(self, "ai_menu_button", None)
        if button is None:
            return
        try:
            if self._ai_menu_enabled:
                button.configure(
                    fg_color=COLORS["accent"],
                    hover_color=COLORS["accent_hover"],
                    text_color="#ffffff",
                )
            else:
                button.configure(
                    fg_color="#f2f2f0",
                    hover_color="#e7e7e4",
                    text_color=COLORS["text"],
                )
        except tk.TclError:
            pass  # 页面可能已被热卸载

    # ------------------------------------------------------------------ 事件与命令

    @staticmethod
    def _course_key(course: dict[str, Any]) -> str:
        """课程登记键：优先课程 ID，其次课程入口 URL，最后标题。"""
        return str(course.get("course_id") or course.get("course_url") or course.get("title") or "")

    def start_refresh(self) -> None:
        if self._sync_running or self.runtime.is_running():
            return
        self._sync_running = True
        self._set_task_buttons_state("disabled")
        self._set_runtime_state("running", "正在启动独立 Edge 并读取个人空间…")
        self.submit("chaoxing_refresh")

    def start_course_study(self, course: dict[str, Any]) -> None:
        """课程卡片「开始学习」：只学习该门课程。

        学习通同一账号同时只能进行一门课程的上课（同时播放多个课程页面会触发验证码），
        因此这里与「刷新全部数据」共用同一个任务闸门：任一任务运行期间全部禁用。
        """
        if self._sync_running or self.runtime.is_running():
            self.log("已有任务正在运行：学习通同一账号同一时间只能学习一门课程，请先取消或等待当前任务结束。", "warning")
            return
        course_url = str(course.get("course_url") or "").strip()
        title = str(course.get("title") or "未知课程")
        if not course_url:
            messagebox.showinfo("学习通助手", f"课程「{title}」没有可用的学习入口，请先刷新全部数据。")
            return
        self._sync_running = True
        self._set_task_buttons_state("disabled")
        self._set_runtime_state("running", f"正在进入课程「{title}」并自动学习未完成章节…")
        self.submit(
            "chaoxing_study",
            course_url=course_url,
            course_id=str(course.get("course_id") or ""),
            course_title=title,
        )

    def cancel_refresh(self) -> None:
        self.runtime.cancel()
        self._set_runtime_state("cancelling", "正在停止当前任务，请稍候…")

    def open_space(self) -> None:
        if self._sync_running or self.runtime.is_running():
            self.log("当前有任务正在运行，请等任务结束后再打开个人空间。", "warning")
            return
        self._set_runtime_state("running", "正在打开独立 Edge 中的学习通个人空间…")
        self.submit("chaoxing_open_space")

    def open_course(self, course: dict[str, Any]) -> None:
        url = str(course.get("course_url") or "").strip()
        if not url:
            messagebox.showinfo("学习通助手", "该课程没有可用的进入地址。")
            return
        if self._sync_running or self.runtime.is_running():
            self.log("当前有任务正在运行，请等任务结束后再打开课程。", "warning")
            return
        self._set_runtime_state("running", f"正在打开课程：{course.get('title') or ''}")
        self.submit("chaoxing_open_course", url=url)

    def clear_cache(self) -> None:
        if self._sync_running or self.runtime.is_running():
            self.log("插件任务正在运行，已跳过「清理本地缓存」；请等任务结束后重试。", "warning")
            return
        removed = []
        for path in (self.profile_file, self.courses_file, self.auth_file, self.study_report_file):
            try:
                if path.exists():
                    path.unlink()
                    removed.append(path.name)
            except OSError:
                self.log(f"无法删除缓存文件 {path.name}。", "warning")
        self._profile = {}
        self._courses = []
        self._refresh_ui()
        self._empty_reason_var.set("缓存已清理。点击「获取基本数据」重新登录或刷新学习通数据。")
        self._last_sync_var.set("尚未同步")
        self._set_runtime_state("idle", "缓存已清理，独立 Edge 未在执行任务。")
        self.log(f"已清理学习通本地缓存：{'、'.join(removed) if removed else '无文件'}。", "success")

    def open_profile_directory(self) -> None:
        """打开当前插件的 Edge 资料目录。"""
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(self.profile_dir)

    def open_data_file(self) -> None:
        if not self.courses_file.exists():
            messagebox.showinfo("学习通助手", "还没有课程缓存，请先点击「获取基本数据」。")
            return
        os.startfile(self.courses_file)

    _COMMAND_LABELS = {
        "restart": "重启浏览器",
        "chaoxing_refresh": "刷新全部数据",
        "chaoxing_study": "开始学习",
        "chaoxing_open_space": "打开个人空间",
        "chaoxing_open_course": "打开课程",
        "chaoxing_ai_menu": "批改菜单开关",
    }

    def handle_event(self, event: dict[str, Any]) -> None:
        if not self._active:
            return
        event_type = str(event.get("type") or "")
        if event_type == "chaoxing_profile_data":
            data = event.get("data")
            if isinstance(data, dict):
                self._profile = data
                self._refresh_ui()
        elif event_type == "chaoxing_courses_data":
            data = event.get("data")
            if isinstance(data, dict):
                courses = data.get("courses")
                if isinstance(courses, list):
                    self._courses = [
                        item for item in courses
                        if isinstance(item, dict) and item.get("is_current")
                    ]
                if isinstance(data.get("profile"), dict):
                    self._profile = data["profile"]
                self._refresh_ui()
        elif event_type == "chaoxing_profile_status":
            message = str(event.get("message") or "正在读取基本信息…")
            self._set_runtime_state("running", message)
            if not (self._profile or self._courses):
                self._empty_reason_var.set(message)
        elif event_type == "chaoxing_run_status":
            self._apply_run_status(event)
        elif event_type == "chaoxing_grade_status":
            # 页面批改菜单的进度事件：只刷新头部详情与同步时间，不改变任务状态
            state = str(event.get("state") or "")
            message = str(event.get("message") or "")
            if message:
                try:
                    self._runtime_detail_var.set(message)
                except tk.TclError:
                    pass
            if state == "done":
                self._last_sync_var.set(f"批改完成 {now_text()}")
        elif event_type == "status":
            state = str(event.get("state") or "")
            detail = str(event.get("detail") or "")
            if state == "starting":
                self._set_runtime_state("starting", detail)
            elif state == "running":
                self._browser_running = True
                self._set_runtime_state("ready", detail)
            elif state == "stopped":
                self._browser_running = False
                self._set_runtime_state("stopped", detail or "独立 Edge 已停止。")
        elif event_type == "command_done":
            command = str(event.get("command") or "")
            if not self._sync_running:
                if command == "stop":
                    self._set_runtime_state("stopped", "独立 Edge 已停止。")
                else:
                    label = self._COMMAND_LABELS.get(command, command)
                    self._set_runtime_state("ready", f"命令已完成: {label}")
        elif event_type == "plugin_process_error":
            self._sync_running = False
            self._set_task_buttons_state("normal")
            self._set_runtime_state("error", str(event.get("message") or "插件进程异常"))

    def _apply_run_status(self, event: dict[str, Any]) -> None:
        state = str(event.get("state") or "")
        message = str(event.get("message") or "学习通状态更新")
        mode = str(event.get("mode") or "")
        if state in {"running", "login_required"}:
            self._sync_running = True
            self._set_task_buttons_state("disabled")
            if state == "login_required":
                self._set_runtime_state("waiting_login", message)
            else:
                self._set_runtime_state("running", message)
            if not (self._profile or self._courses):
                self._empty_reason_var.set(message)
        elif state == "opened":
            # 打开个人空间 / 打开课程 / 仅启动浏览器这类轻命令：不进入任务态
            self._set_runtime_state("ready", message)
        elif state == "done":
            self._sync_running = False
            self._set_task_buttons_state("normal")
            self._set_runtime_state("done", message)
            if mode == "study":
                self._last_sync_var.set(f"学习完成 {now_text()}")
            else:
                self._last_sync_var.set(f"上次同步 {now_text()}")
            self._show_state("data" if (self._profile or self._courses) else "empty")
        elif state == "warning":
            # 课程异常但流程继续：保持运行态，详情展示原因
            self._set_runtime_state("running", message)
            if not (self._profile or self._courses):
                self._empty_reason_var.set(message)
        elif state in {"error", "cancelled"}:
            self._sync_running = False
            self._set_task_buttons_state("normal")
            self._set_runtime_state("cancelled" if state == "cancelled" else "error", message)
            if not (self._profile or self._courses):
                self._empty_reason_var.set(message)