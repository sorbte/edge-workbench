"""微软积分助手：Microsoft Rewards 积分数据、账户规则与任务中心。

由宿主的「Rewards」与「账户与规则」两页合并而来的单个插件页面，合并时去掉了重复内容：
- 一键自动化入口（原侧栏「一键自动运行」按钮）变为页头最左的「▶ 启动」按钮；
- 数据操作图标（刷新全部数据 / 清理缓存 / 打开 JSON / 测试点击）原先两页各有一组，只保留页头一组；
- 两个页面各有一个完全相同的空状态引导，只保留一个；
- 账户文本行中的「可用积分」与数据摘要中的「账户积分」是同一数据，只保留摘要里的；
- 账户文本行中的积分规则摘要与「SEARCH RULES」网格重复，只保留网格。
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
    ICON_OPEN_FILE,
    ICON_REFRESH,
    ICON_TEST_CLICK,
    ICON_WINDOW,
    WorkbenchPlugin,
    mono_font,
    now_text,
    sans_font,
)


class MicrosoftPointsAssistantPlugin(WorkbenchPlugin):
    """积分助手插件页：账户与数据摘要、搜索任务、每日任务与积分规则。"""

    _FLOW_RUNNING_STATES = {"planning", "planned", "news", "searching", "waiting", "scanning", "syncing"}

    def build_pages(self, parent: Any) -> list[tuple[str, str, Any]]:
        self._rewards_ok = False
        self._account_ok = False
        self._auto_running_ui = False
        self._summary_var = tk.StringVar(value="尚未获取 Rewards 数据；点击「刷新全部数据」开始。")
        self._status_var = tk.StringVar(value=f"数据文件: {self.rewards_data_file.name}")
        self._account_line_var = tk.StringVar(value="账户信息：尚未获取")
        self._search_task_var = tk.StringVar(value="搜索任务未启动")
        self._daily_task_var = tk.StringVar(value="每日任务未启动")
        self._empty_reason_var = tk.StringVar(value="尚未获取数据。请启动浏览器并登录 Microsoft 账号。")
        self._rule_labels: dict[str, ctk.CTkLabel] = {}
        self._runtime_state_var = tk.StringVar(value="未启动")
        self._runtime_detail_var = tk.StringVar(value="独立进程尚未启动；启动后使用专用 Edge 资料目录。")
        self._runtime_process_var = tk.StringVar(value="进程: 未启动")
        self._profile_label_var = tk.StringVar(value=f"资料: {self.profile_dir.name}")
        self._browser_window_visible = not self.runtime.headless
        self._browser_running = False

        page = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        self._build_header(page)
        body = ctk.CTkFrame(page, fg_color="transparent", corner_radius=0)
        body.pack(fill="both", expand=True)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self._build_empty_state(body)
        self._build_data_state(body)
        self._show_state("empty")
        self.app.after(600, self._load_cached_data)
        return [("points", str(self.manifest.get("name") or "微软积分助手"), page)]

    def _build_header(self, page: Any) -> None:
        header = ctk.CTkFrame(page, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(18, 10))

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="x")
        title_cluster = ctk.CTkFrame(title_row, fg_color="transparent")
        title_cluster.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(title_cluster, text="微软积分助手", text_color=COLORS["text"], font=sans_font(20, "bold")).pack(side="left")
        status_inline = ctk.CTkFrame(title_cluster, fg_color="transparent")
        status_inline.pack(side="left", padx=(10, 0), pady=(2, 0))
        self._runtime_dot = ctk.CTkLabel(status_inline, text="●", text_color=COLORS["muted"], font=("Segoe UI", 12))
        self._runtime_dot.pack(side="left", padx=(0, 5))
        self._runtime_state_label = ctk.CTkLabel(status_inline, textvariable=self._runtime_state_var, text_color=COLORS["muted"], font=sans_font(11, "bold"))
        self._runtime_state_label.pack(side="left")

        ctk.CTkLabel(
            title_row,
            text="Microsoft Rewards 积分、账户规则与任务中心",
            text_color=COLORS["muted"],
            font=sans_font(11),
        ).pack(side="right", padx=(16, 0), pady=(7, 0))

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

        # 操作栏独占一行，避免状态文本与按钮在窄窗口中互相覆盖。
        action_card = ctk.CTkFrame(page, fg_color="transparent", corner_radius=0)
        action_card.pack(fill="x", padx=24, pady=(0, 12))
        self.browser_actions = ctk.CTkFrame(action_card, fg_color="transparent")
        self.browser_actions.pack(side="left", pady=10)

        self.auto_run_button = self.app._button(self.browser_actions, "▶  启动", self.start_auto_run, "primary", 96)
        self.auto_run_button.pack(side="left")

        data_actions = ctk.CTkFrame(action_card, fg_color="transparent")
        data_actions.pack(side="right", pady=10)
        self.toolbar = data_actions
        self.fetch_button = self.app._icon_button(data_actions, ICON_REFRESH, self.fetch_rewards_data, "刷新全部数据")
        self.fetch_button.pack(side="left", padx=(0, 6))
        self.clear_cache_button = self.app._icon_button(data_actions, ICON_CLEAN, self.clear_rewards_cache, "清理 Rewards 缓存（任务运行中会跳过）")
        self.clear_cache_button.pack(side="left", padx=(0, 6))
        self.open_json_button = self.app._icon_button(data_actions, ICON_OPEN_FILE, self.open_rewards_data_file, "打开数据 JSON")
        self.open_json_button.pack(side="left", padx=(0, 6))
        self.test_click_button = self.app._icon_button(data_actions, ICON_TEST_CLICK, self.test_click, "测试点击")
        self.test_click_button.pack(side="left", padx=(0, 6))
        self.browser_window_button = self.app._icon_button(data_actions, ICON_WINDOW, self.toggle_browser_window, "切换独立 Edge 窗口显示（黑色=后台运行）")
        self.browser_window_button.pack(side="left")
        self._sync_browser_window_button()
        self.app._bind_busy("rewards_data", self.fetch_button, self.clear_cache_button)
        self.app._bind_busy("test_click", self.test_click_button)
        self.app._bind_busy("auto_run", self.auto_run_button)

    def _build_empty_state(self, body: Any) -> None:
        self.empty_frame = ctk.CTkFrame(body, fg_color="transparent", corner_radius=0)
        self.empty_frame.grid(row=0, column=0, sticky="nsew")
        self.empty_frame.grid_rowconfigure(0, weight=1)
        self.empty_frame.grid_columnconfigure(0, weight=1)
        empty_center = ctk.CTkFrame(self.empty_frame, fg_color="transparent")
        empty_center.grid(row=0, column=0)
        ctk.CTkLabel(empty_center, text="◇", text_color=COLORS["border_strong"], font=mono_font(34)).pack()
        ctk.CTkLabel(empty_center, text="暂无 Rewards 数据", text_color=COLORS["text"], font=sans_font(15, "bold")).pack(pady=(14, 8))
        ctk.CTkLabel(empty_center, textvariable=self._empty_reason_var, justify="center", wraplength=560, text_color=COLORS["muted"], font=sans_font(11)).pack()
        ctk.CTkLabel(empty_center, text="登录完成后点击下方按钮重新获取", text_color=COLORS["muted"], font=sans_font(10)).pack(pady=(10, 16))
        empty_fetch_button = self.app._button(empty_center, "刷新全部数据", self.fetch_rewards_data, "primary", 130)
        empty_fetch_button.pack()
        self.app._bind_busy("rewards_data", empty_fetch_button)

    def _build_data_state(self, body: Any) -> None:
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
            # 保留鼠标滚轮滚动，但不再显示右侧滚动条。
            scrollbar.grid_remove()
        self.data_frame.grid_remove()

        summary_card = self.app._card(self.data_frame, padded=True)
        self.app._section_label(summary_card, "ACCOUNT / 账户与数据摘要", top=12)
        ctk.CTkLabel(summary_card, textvariable=self._account_line_var, justify="left", anchor="w", wraplength=900, text_color=COLORS["text"], font=sans_font(11)).pack(fill="x", padx=16)
        ctk.CTkLabel(summary_card, textvariable=self._summary_var, justify="left", anchor="w", wraplength=900, text_color=COLORS["text"], font=sans_font(11)).pack(fill="x", padx=16, pady=(6, 4))
        self._data_file_button = ctk.CTkButton(
            summary_card,
            textvariable=self._status_var,
            command=self.open_rewards_data_file,
            height=24,
            corner_radius=4,
            fg_color="transparent",
            hover_color="#f2f2f0",
            text_color=COLORS["muted"],
            font=sans_font(11),
            anchor="w",
        )
        self._data_file_button.pack(fill="x", padx=10, pady=(0, 8))
        self.app._attach_tooltip(self._data_file_button, "点击打开数据 JSON")

        search_card = self.app._card(self.data_frame, padded=True)
        self.app._section_label(search_card, "SEARCH TASK / 搜索任务", top=12)
        ctk.CTkLabel(search_card, textvariable=self._search_task_var, text_color=COLORS["muted"], font=sans_font(11), anchor="w").pack(fill="x", padx=16)
        self.search_progress = ctk.CTkProgressBar(search_card, height=8, corner_radius=4, fg_color=COLORS["border"], progress_color=COLORS["accent"])
        self.search_progress.pack(fill="x", padx=16, pady=(10, 10))
        self.search_progress.set(0)
        search_row = ctk.CTkFrame(search_card, fg_color="transparent")
        search_row.pack(fill="x", padx=14, pady=(0, 14))
        self.search_start_button = self.app._button(search_row, "开始搜索任务", self.start_search_task, "primary", 125)
        self.search_start_button.pack(side="left")
        self.search_cancel_button = self.app._button(search_row, "取消任务", self.cancel_search_task, "danger", 100)
        self.search_cancel_button.configure(state="disabled")
        self.search_cancel_button.pack(side="left", padx=(8, 0))

        daily_card = self.app._card(self.data_frame, padded=True)
        self.app._section_label(daily_card, "DAILY TASKS / 每日任务", top=12)
        ctk.CTkLabel(daily_card, textvariable=self._daily_task_var, text_color=COLORS["muted"], font=sans_font(11), anchor="w").pack(fill="x", padx=16)
        self.daily_progress = ctk.CTkProgressBar(daily_card, height=8, corner_radius=4, fg_color=COLORS["border"], progress_color=COLORS["accent"])
        self.daily_progress.pack(fill="x", padx=16, pady=(10, 10))
        self.daily_progress.set(0)
        daily_row = ctk.CTkFrame(daily_card, fg_color="transparent")
        daily_row.pack(fill="x", padx=14, pady=(0, 14))
        self.daily_trigger_button = self.app._button(daily_row, "开始每日任务", self.trigger_incomplete_tasks, "primary", 135)
        self.daily_trigger_button.pack(side="left")
        self.daily_cancel_button = self.app._button(daily_row, "取消任务", self.cancel_daily_task, "danger", 100)
        self.daily_cancel_button.configure(state="disabled")
        self.daily_cancel_button.pack(side="left", padx=(8, 0))

        ctk.CTkLabel(self.data_frame, text="SEARCH RULES / 积分规则", text_color=COLORS["muted"], font=sans_font(11, "bold"), anchor="w").pack(fill="x", padx=24, pady=(6, 0))
        rules = ctk.CTkFrame(self.data_frame, fg_color="transparent")
        rules.pack(fill="x", padx=18, pady=(4, 16))
        for column in range(4):
            rules.grid_columnconfigure(column, weight=1, uniform="rule")
        definitions = [
            ("points_per_search", "每次搜索"),
            ("daily_search_limit", "每日搜索上限"),
            ("monthly_level_reward", "每月等级奖励"),
            ("default_search_reward", "默认搜索奖励"),
            ("bing_star_reward_max", "Bing Star 上限"),
            ("store_multiplier", "Store 倍率"),
            ("xbox_multiplier", "Xbox 倍率"),
            ("redemption_discount_points", "兑换折扣点数"),
        ]
        for index, (key, label) in enumerate(definitions):
            card = self.app._card(rules)
            card.grid(row=index // 4, column=index % 4, sticky="nsew", padx=6, pady=6)
            ctk.CTkLabel(card, text=label, text_color=COLORS["muted"], font=sans_font(11)).pack(anchor="w", padx=14, pady=(11, 0))
            value = ctk.CTkLabel(card, text="--", text_color=COLORS["text"], font=mono_font(16, "bold"))
            value.pack(anchor="w", padx=14, pady=(4, 12))
            self._rule_labels[key] = value

    # ------------------------------------------------------------------ 状态切换

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
        """独立切换启动时是否显示 Edge 窗口；当前页面不因此中断。"""
        visible = not self._browser_window_visible
        self.app.set_browser_window_visible(visible)
        if self._browser_running:
            self._runtime_detail_var.set("窗口显示设置已更新；重启独立浏览器后生效。")
            self.log("浏览器正在运行，窗口显示设置将在重启独立浏览器后生效。", "warning")

    def _set_runtime_state(self, state: str, detail: str = "") -> None:
        """Update this plugin's own runtime indicator inside its page."""
        mapping = {
            "idle": ("未启动", COLORS["muted"]),
            "stopped": ("未启动", COLORS["muted"]),
            "starting": ("启动中", COLORS["warning"]),
            "running": ("运行中", COLORS["success"]),
            "ready": ("就绪", COLORS["accent"]),
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

    def _show_state(self, state: str) -> None:
        """在空状态与数据态之间切换。"""
        if state == "empty":
            self.empty_frame.grid()
            self.data_frame.grid_remove()
            self._set_toolbar_visible(False)
        else:
            self.data_frame.grid()
            self.empty_frame.grid_remove()
            self._set_toolbar_visible(True)

    def _refresh_state(self) -> None:
        self._show_state("data" if (self._rewards_ok or self._account_ok) else "empty")

    # ------------------------------------------------------------------ 热更新

    def on_unload(self) -> None:
        """Stop page callbacks and the isolated plugin process."""
        super().on_unload()

    def handle_event(self, event: dict[str, Any]) -> None:
        if not self._active:
            return
        event_type = event.get("type")
        if event_type == "rewards_data":
            self._apply_rewards_data(event.get("data", {}))
        elif event_type == "rewards_account":
            self._apply_rewards_account(event.get("data", {}))
        elif event_type == "account_info_status":
            self._apply_account_info_status(event)
        elif event_type == "search_task_status":
            self._apply_search_task_status(event)
        elif event_type == "auto_status":
            self._apply_auto_status(event)
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
                self._set_runtime_state("stopped", detail)
        elif event_type == "command_done":
            command = str(event.get("command") or "")
            if not self._auto_running_ui:
                if command == "stop":
                    self._set_runtime_state("stopped", "独立浏览器已停止。")
                else:
                    self._set_runtime_state("ready", f"命令已完成: {command}")
        elif event_type == "plugin_process_error":
            self._set_runtime_state("error", str(event.get("message") or "插件进程异常"))

    def _apply_auto_status(self, event: dict[str, Any]) -> None:
        state = str(event.get("state") or "")
        message = str(event.get("message") or "")
        if state in {"started", "running"}:
            self._auto_running_ui = True
            self.auto_run_button.configure(state="disabled")
            for button in (self.search_start_button, self.daily_trigger_button, self.fetch_button, self.clear_cache_button, self.test_click_button):
                button.configure(state="disabled")
            self._set_runtime_state("running", message or "一键自动化运行中。")
        elif state == "done":
            self._auto_running_ui = False
            self.auto_run_button.configure(state="normal")
            for button in (self.search_start_button, self.daily_trigger_button, self.fetch_button, self.clear_cache_button, self.test_click_button):
                button.configure(state="normal")
            self.search_cancel_button.configure(state="disabled")
            self.daily_cancel_button.configure(state="disabled")
            self._set_runtime_state("done", message or "一键自动化全部完成。")
        elif state == "cancelled":
            self._auto_running_ui = False
            self.auto_run_button.configure(state="normal")
            for button in (self.search_start_button, self.daily_trigger_button, self.fetch_button, self.clear_cache_button, self.test_click_button):
                button.configure(state="normal")
            self.search_cancel_button.configure(state="disabled")
            self.daily_cancel_button.configure(state="disabled")
            self._set_runtime_state("cancelled", message or "一键自动化已取消。")
        elif state == "error":
            self._auto_running_ui = False
            self.auto_run_button.configure(state="normal")
            for button in (self.search_start_button, self.daily_trigger_button, self.fetch_button, self.clear_cache_button, self.test_click_button):
                button.configure(state="normal")
            self.search_cancel_button.configure(state="disabled")
            self.daily_cancel_button.configure(state="disabled")
            self._set_runtime_state("error", message or "一键自动化失败。")

    def _apply_search_task_status(self, event: dict[str, Any]) -> None:
        flow = str(event.get("flow") or "search")
        state = str(event.get("state") or "")
        message = str(event.get("message") or "任务状态更新")
        running = state in self._FLOW_RUNNING_STATES
        if flow == "search":
            plan = event.get("plan") or {}
            if state == "planned" and isinstance(plan, dict):
                message = (
                    f"计划：搜索积分 {plan.get('search_progress_text', '--')}，"
                    f"需搜索 {plan.get('required_searches', 0)} 次，"
                    f"准备新闻 {plan.get('news_needed', 0)} 条"
                )
            self._search_task_var.set(message)
            self._set_flow_progress(self.search_progress, event, state)
            if not self._auto_running_ui:
                self.search_start_button.configure(state="disabled" if running else "normal")
            self.search_cancel_button.configure(state="normal" if running else "disabled")
        else:
            self._daily_task_var.set(message)
            self._set_flow_progress(self.daily_progress, event, state)
            if not self._auto_running_ui:
                self.daily_trigger_button.configure(state="disabled" if running else "normal")
            self.daily_cancel_button.configure(state="normal" if running else "disabled")
        if state in {"planning", "planned", "news", "searching", "waiting", "scanning", "syncing"}:
            self._set_runtime_state("running", message)
        elif state == "done":
            if not self._auto_running_ui:
                self._set_runtime_state("done", message)
        elif state == "cancelled":
            self._set_runtime_state("cancelled", message)
        elif state == "error":
            self._set_runtime_state("error", message)

    @staticmethod
    def _set_flow_progress(bar: Any, event: dict[str, Any], state: str) -> None:
        total = int(event.get("total") or 0)
        if total > 0:
            current = int(event.get("current") or 0)
            bar.set(max(0.0, min(1.0, current / total)))
        elif state == "done":
            bar.set(1.0)

    def _apply_account_info_status(self, event: dict[str, Any]) -> None:
        state = str(event.get("state") or "")
        message = str(event.get("message") or "")
        if state == "loading":
            self._account_line_var.set(message or "正在获取账户信息…")
            self._set_runtime_state("running", message or "正在获取账户信息与积分规则。")
            if not (self._rewards_ok or self._account_ok):
                self._show_state("empty")
                self._empty_reason_var.set(message or "正在获取账户信息与积分规则…")
        elif state == "error":
            self._set_runtime_state("error", message or "获取账户信息失败。")
            if not (self._rewards_ok or self._account_ok):
                self._show_state("empty")
                self._empty_reason_var.set(message or "获取账户信息失败，请重试。")
            self.log(message or "获取账户信息失败。", "warning")
        elif state == "done":
            self._set_runtime_state("done", "账户信息与积分规则获取完成。")

    def _apply_rewards_data(self, data: dict[str, Any]) -> None:
        login_required = bool(data.get("login_required"))
        has_payload = any(
            data.get(key) is not None
            for key in ("today_points", "account_points", "daily_task_progress", "daily_tasks")
        )
        if login_required or not has_payload:
            # 未登录 / 数据无法识别：切到空状态并给出原因与下一步指引
            self._rewards_ok = False
            self._summary_var.set("Rewards 数据未能识别；点击「刷新全部数据」重新获取。")
            if login_required:
                reason = "未检测到登录状态：请先在 Edge 中登录 Microsoft 账号后重新获取。"
                if self.worker.headless:
                    reason += " 当前为隐藏模式，无法手动登录——请先关闭隐藏模式并重启浏览器。"
                self._empty_reason_var.set(reason)
                self.log("Rewards 页面检测到未登录，已切换到空状态。", "warning")
            else:
                self._empty_reason_var.set("已打开 Rewards 页面，但未能识别到数据（可能未登录或页面结构变化）。")
                self.log("未能识别 Rewards 数据，已切换到空状态。", "warning")
            self._refresh_state()
            return

        self._rewards_ok = True
        today_points = data.get("today_points")
        account_points = data.get("account_points")
        task_progress = self._progress_text(data.get("daily_task_progress"))
        points_progress = self._progress_text(data.get("today_points_progress"))
        edge_progress = self._progress_text(data.get("edge_browsing"))
        completed = data.get("daily_tasks_completed", 0)
        pending = data.get("daily_tasks_pending", 0)
        summary = (
            f"今日积分 {today_points if today_points is not None else '--'}  ·  "
            f"账户积分 {account_points if account_points is not None else '--'}  ·  "
            f"日常任务 {task_progress}  ·  已完成 {completed} / 未完成 {pending}  ·  "
            f"当日进度 {points_progress}  ·  Edge 浏览 {edge_progress} 分钟"
        )
        tasks = data.get("daily_tasks") or []
        if tasks:
            preview = "  |  ".join(
                f"{'✓' if task.get('completed') else '○'} {task.get('title', '未命名')} +{task.get('points', 0)}"
                for task in tasks[:8]
            )
            summary += f"\n带积分任务：{preview}"
        else:
            summary += "\n未识别到带积分的日常任务（无积分选项已自动排除）。"
        self._summary_var.set(summary)
        fetched_at = data.get("fetched_at") or now_text()
        self._status_var.set(f"最近更新: {fetched_at}  ·  文件: {self.rewards_data_file.name}")
        self.log("Rewards 基本数据面板已更新。", "success")
        self._refresh_state()

    def _apply_rewards_account(self, data: dict[str, Any]) -> None:
        account = data.get("account") or {}
        rules = data.get("search_rules") or (data.get("rules") or {}).get("search_rules") or {}
        if not account and not rules:
            self._account_ok = False
            self._account_line_var.set("账户信息：未能识别（可能未登录或页面结构变化）。")
            self.log("未能识别账户数据。", "warning")
            self._refresh_state()
            return
        self._account_ok = True
        # 「可用积分」与摘要中的「账户积分」重复，此处不再展示；积分规则交给下方网格
        line = (
            f"账户：{account.get('username') or '--'}  ·  "
            f"等级：{account.get('membership_level') or rules.get('current_tier') or '--'}  ·  "
            f"可领取：{self._format_points(account.get('claimable_points'))}  ·  "
            f"连续打卡：{account.get('streak_days') if account.get('streak_days') is not None else '--'} 天  ·  "
            f"升级还需活动：{account.get('next_level_activities') if account.get('next_level_activities') is not None else '--'}"
        )
        self._account_line_var.set(line)
        for key, label in self._rule_labels.items():
            value = rules.get(key)
            if value is None:
                display = "--"
            elif key in {"points_per_search", "daily_search_limit"}:
                display = f"{value} 分"
            elif key in {"store_multiplier", "xbox_multiplier"}:
                display = f"{value}x"
            else:
                display = self._format_points(value)
            label.configure(text=display)
        self.log("账户信息和积分规则面板已更新。", "success")
        self._refresh_state()

    # ------------------------------------------------------------------ 动作

    def start_auto_run(self) -> None:
        """一键自动化入口：在本插件自己的独立进程中启动浏览器并执行完整流程。"""
        self.auto_run_button.configure(state="disabled")
        self._set_runtime_state("starting", "已提交一键自动化任务，正在启动独立浏览器进程。")
        self.submit("auto_run")

    def fetch_rewards_data(self) -> None:
        self.app._disable_for("rewards_data")
        self._set_runtime_state("running", "正在获取 Rewards 数据与账户规则。")
        self._status_var.set("正在读取 Rewards 页面…")
        self._account_line_var.set("正在读取账户信息与积分规则…")
        self.submit("rewards_data")

    def test_click(self) -> None:
        self.app._disable_for("test_click")
        self._set_runtime_state("running", "正在执行测试点击。")
        self._status_var.set("测试点击中：优先打开今日积分抽屉…")
        self.submit("test_click")

    def start_search_task(self) -> None:
        self.search_start_button.configure(state="disabled")
        self.search_cancel_button.configure(state="normal")
        self._set_runtime_state("running", "正在准备搜索任务。")
        self._search_task_var.set("正在计算搜索计划…")
        self.search_progress.set(0)
        self.submit("search_task")

    def cancel_search_task(self) -> None:
        self.worker.cancel_search_task()
        self._set_runtime_state("cancelling", "正在取消搜索任务。")
        self._search_task_var.set("正在取消搜索任务…")

    def cancel_daily_task(self) -> None:
        self.worker.cancel_search_task()
        self._set_runtime_state("cancelling", "正在取消每日任务。")
        self._daily_task_var.set("正在取消每日任务…")

    def trigger_incomplete_tasks(self) -> None:
        self.daily_trigger_button.configure(state="disabled")
        self.daily_cancel_button.configure(state="normal")
        self._set_runtime_state("running", "正在触发未完成的每日任务。")
        self._daily_task_var.set("正在扫描每日任务 / 每日活动…")
        self.daily_progress.set(0)
        self.submit("trigger_tasks")

    def open_profile_directory(self) -> None:
        """打开当前插件的 Edge 资料目录。"""
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(self.profile_dir)

    def open_rewards_data_file(self) -> None:
        if not self.rewards_data_file.exists():
            messagebox.showinfo("Rewards 数据", "还没有数据文件，请先点击「刷新全部数据」。")
            return
        os.startfile(self.rewards_data_file)

    def clear_rewards_cache(self) -> None:
        """删除插件自己的两份数据缓存；任务运行期间跳过。"""
        if self.is_running():
            self.log("插件任务正在运行，已跳过「清理 Rewards 缓存」；请等任务结束后重试。", "warning")
            return
        removed: list[str] = []
        for path in (self.rewards_data_file, self.rewards_account_file):
            try:
                if path.exists():
                    path.unlink()
                    removed.append(path.name)
            except OSError:
                self.log(f"无法删除缓存文件 {path.name}，可能正被占用。", "warning")
        self._rewards_ok = False
        self._account_ok = False
        self._summary_var.set("尚未获取 Rewards 数据；点击「刷新全部数据」开始。")
        self._status_var.set(f"数据文件: {self.rewards_data_file.name}")
        self._account_line_var.set("账户信息：尚未获取")
        self._empty_reason_var.set("缓存已清理。启动浏览器并登录 Microsoft 账号后可重新获取。")
        for label in self._rule_labels.values():
            label.configure(text="--")
        self._show_state("empty")
        self._set_runtime_state("idle", "缓存已清理，插件进程未启动任务。")
        if removed:
            self.log(f"已清理 Rewards 缓存（{'、'.join(removed)}），页面已重置为空状态。", "success")
        else:
            self.log("没有可清理的 Rewards 缓存文件。")

    def _load_cached_data(self) -> None:
        """启动/热装载时读取本插件自己的缓存数据。"""
        if not self._active:
            return
        if self.rewards_data_file.exists():
            try:
                payload = json.loads(self.rewards_data_file.read_text(encoding="utf-8"))
                data = payload.get("summary", payload)
                if isinstance(data, dict) and data:
                    self._apply_rewards_data(data)
            except Exception:
                pass
        if self.rewards_account_file.exists():
            try:
                payload = json.loads(self.rewards_account_file.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload:
                    self._apply_rewards_account(payload)
            except Exception:
                pass

    @staticmethod
    def _format_points(value: Any) -> str:
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return "--"

    @staticmethod
    def _progress_text(value: Any) -> str:
        if isinstance(value, dict):
            return f"{value.get('current', '?')}/{value.get('target', '?')}"
        return str(value or "--")
