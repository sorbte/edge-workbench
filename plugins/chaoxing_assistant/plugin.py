"""学习通助手插件页：缓存并展示个人空间基本信息和学历课程。"""

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
    WorkbenchPlugin,
    mono_font,
    now_text,
    sans_font,
)


class ChaoxingAssistantPlugin(WorkbenchPlugin):
    """学习通个人空间数据同步与课程总览。"""

    FILTERS = (("all", "全部"), ("current", "在读"), ("history", "历史"))

    def __init__(self, manifest: dict[str, Any], app: Any) -> None:
        super().__init__(manifest, app)
        self.profile_file = self.runtime_dir / "profile.json"
        self.courses_file = self.runtime_dir / "courses.json"
        self.auth_file = self.runtime_dir / "auth_state.json"
        self.study_report_file = self.runtime_dir / "study_report.json"
        self._profile: dict[str, Any] = {}
        self._courses: list[dict[str, Any]] = []
        self._stats: dict[str, Any] = {}
        self._course_filter = "all"
        self._sync_running = False

    def build_pages(self, parent: Any) -> list[tuple[str, str, Any]]:
        """构建学习通助手页面，并在启动时自动读取缓存、触发同步。"""
        self._status_var = tk.StringVar(value="等待同步")
        self._detail_var = tk.StringVar(value="正在读取本机缓存…")
        self._profile_name_var = tk.StringVar(value="未获取用户信息")
        self._profile_id_var = tk.StringVar(value="用户 ID：--")
        self._profile_meta_var = tk.StringVar(value="登录后同步个人空间数据")
        self._last_sync_var = tk.StringVar(value="尚未同步")
        self._stat_total_var = tk.StringVar(value="--")
        self._stat_current_var = tk.StringVar(value="--")
        self._stat_history_var = tk.StringVar(value="--")
        self._stat_progress_var = tk.StringVar(value="--")
        self._course_count_var = tk.StringVar(value="暂无课程")
        self._empty_reason_var = tk.StringVar(value="尚未同步学习通数据。首次使用会打开独立 Edge，请使用“学习通”App 扫描二维码登录。")
        self._sync_progress_var = tk.DoubleVar(value=0.0)

        page = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        self._build_header(page)
        body = ctk.CTkFrame(page, fg_color="transparent", corner_radius=0)
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

        self._build_profile_card()
        self._build_stats_row()
        self._build_course_section()
        self._show_state("empty")
        self._load_cache()
        return [("chaoxing", str(self.manifest.get("name") or "学习通助手"), page)]

    # ------------------------------------------------------------------ 页面构建

    def _build_header(self, page: Any) -> None:
        header = ctk.CTkFrame(page, fg_color="transparent", corner_radius=0)
        header.pack(fill="x", padx=24, pady=(18, 10))

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
        self._status_dot = ctk.CTkLabel(title_cluster, text="●", text_color=COLORS["muted"], font=("Segoe UI", 12))
        self._status_dot.pack(side="left", padx=(12, 5), pady=(2, 0))
        self._status_label = ctk.CTkLabel(
            title_cluster,
            textvariable=self._status_var,
            text_color=COLORS["muted"],
            font=sans_font(11, "bold"),
        )
        self._status_label.pack(side="left", pady=(2, 0))
        ctk.CTkLabel(
            title_row,
            text="个人空间基本信息与全部学历课程",
            text_color=COLORS["muted"],
            font=sans_font(11),
        ).pack(side="right", padx=(16, 0), pady=(7, 0))

        self.header_actions = ctk.CTkFrame(header, fg_color="transparent")
        self.header_actions.pack(fill="x", pady=(10, 0))
        self.browser_actions = ctk.CTkFrame(self.header_actions, fg_color="transparent")
        self.browser_actions.pack(side="left")
        self.sync_button = self.app._button(self.browser_actions, "同步全部数据", self.start_refresh, "secondary", 130)
        self.sync_button.pack(side="left")
        self.study_button = self.app._button(self.browser_actions, "开始学习", self.start_study, "primary", 105)
        self.study_button.pack(side="left", padx=(8, 0))
        self.cancel_button = self.app._button(self.browser_actions, "取消同步", self.cancel_refresh, "danger", 100)
        self.cancel_button.configure(state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.open_space_button = self.app._button(self.browser_actions, "打开个人空间", self.open_space, "secondary", 115)
        self.open_space_button.pack(side="left", padx=(8, 0))
        self.data_actions = ctk.CTkFrame(self.header_actions, fg_color="transparent")
        self.data_actions.pack(side="right")
        self.open_json_button = self.app._icon_button(self.data_actions, ICON_OPEN_FILE, self.open_data_file, "打开缓存 JSON")
        self.open_json_button.pack(side="right", padx=(6, 0))
        self.clear_button = self.app._icon_button(self.data_actions, ICON_CLEAN, self.clear_cache, "清理本地缓存")
        self.clear_button.pack(side="right", padx=(6, 0))
        self.sync_progress = ctk.CTkProgressBar(
            header,
            height=5,
            corner_radius=3,
            fg_color=COLORS["border"],
            progress_color=COLORS["accent"],
        )
        self.sync_progress.pack(fill="x", pady=(10, 0))
        self.sync_progress.set(0)

        detail_row = ctk.CTkFrame(header, fg_color="transparent")
        detail_row.pack(fill="x", pady=(6, 0))
        ctk.CTkLabel(
            detail_row,
            textvariable=self._detail_var,
            text_color=COLORS["muted"],
            font=sans_font(10),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            detail_row,
            textvariable=self._last_sync_var,
            text_color=COLORS["muted"],
            font=mono_font(10),
            anchor="e",
        ).pack(side="right")

    def _build_empty_state(self, body: Any) -> None:
        self.empty_frame = ctk.CTkFrame(body, fg_color="transparent", corner_radius=0)
        self.empty_frame.grid(row=0, column=0, sticky="nsew")
        self.empty_frame.grid_rowconfigure(0, weight=1)
        self.empty_frame.grid_columnconfigure(0, weight=1)
        center = ctk.CTkFrame(self.empty_frame, fg_color="transparent")
        center.grid(row=0, column=0)
        ctk.CTkLabel(center, text="▣", text_color=COLORS["border_strong"], font=mono_font(34)).pack()
        ctk.CTkLabel(center, text="暂无学习通数据", text_color=COLORS["text"], font=sans_font(15, "bold")).pack(pady=(14, 8))
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
            text="首次使用请在弹出的独立 Edge 窗口中使用“学习通”App 扫描二维码登录。",
            text_color=COLORS["muted"],
            font=sans_font(10),
        ).pack(pady=(10, 16))
        empty_actions = ctk.CTkFrame(center, fg_color="transparent")
        empty_actions.pack()
        self.empty_sync_button = self.app._button(empty_actions, "同步全部数据", self.start_refresh, "primary", 140)
        self.empty_sync_button.pack(side="left")
        self.empty_cancel_button = self.app._button(empty_actions, "取消同步", self.cancel_refresh, "danger", 100)
        self.empty_cancel_button.configure(state="disabled")
        self.empty_cancel_button.pack(side="left", padx=(8, 0))

    def _show_state(self, state: str) -> None:
        """在空状态与数据态之间切换，布局与微软积分助手保持一致。"""
        if state == "empty":
            self.empty_frame.grid()
            self.data_frame.grid_remove()
            self._set_header_actions_visible(False)
        else:
            self.data_frame.grid()
            self.empty_frame.grid_remove()
            self._set_header_actions_visible(True)

    def _set_header_actions_visible(self, visible: bool) -> None:
        header_actions = getattr(self, "header_actions", None)
        if header_actions is None:
            return
        if visible:
            if not header_actions.winfo_manager():
                header_actions.pack(fill="x", pady=(10, 0), before=self.sync_progress)
        else:
            header_actions.pack_forget()

    def _set_sync_buttons_state(self, state: str) -> None:
        for button in (
            getattr(self, "sync_button", None),
            getattr(self, "study_button", None),
            getattr(self, "empty_sync_button", None),
        ):
            if button is not None:
                try:
                    button.configure(state=state)
                except tk.TclError:
                    pass
        for cancel in (getattr(self, "cancel_button", None), getattr(self, "empty_cancel_button", None)):
            if cancel is not None:
                try:
                    cancel.configure(state="normal" if state == "disabled" else "disabled")
                except tk.TclError:
                    pass

    def _build_profile_card(self) -> None:
        card = self.app._card(self.data_frame, padded=True)
        ctk.CTkLabel(
            card,
            text="USER / 用户",
            text_color=COLORS["muted"],
            font=sans_font(11, "bold"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(12, 6))

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 12))
        self.avatar_label = ctk.CTkLabel(
            row,
            text="学",
            width=54,
            height=54,
            corner_radius=27,
            fg_color=COLORS["accent"],
            text_color="#ffffff",
            font=sans_font(20, "bold"),
        )
        self.avatar_label.pack(side="left", padx=(0, 14))
        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            info,
            textvariable=self._profile_name_var,
            text_color=COLORS["text"],
            font=sans_font(17, "bold"),
            anchor="w",
        ).pack(fill="x")
        ctk.CTkLabel(
            info,
            textvariable=self._profile_id_var,
            text_color=COLORS["muted"],
            font=mono_font(11),
            anchor="w",
        ).pack(fill="x", pady=(3, 0))
        ctk.CTkLabel(
            info,
            textvariable=self._profile_meta_var,
            text_color=COLORS["muted"],
            font=sans_font(10),
            anchor="w",
            wraplength=780,
            justify="left",
        ).pack(fill="x", pady=(3, 0))

    def _build_stats_row(self) -> None:
        stats = ctk.CTkFrame(self.data_frame, fg_color="transparent")
        stats.pack(fill="x", pady=(0, 12))
        for column in range(4):
            stats.grid_columnconfigure(column, weight=1, uniform="chaoxing-stat")
        self._stat_card(stats, "学历课程", self._stat_total_var, 0)
        self._stat_card(stats, "在读课程", self._stat_current_var, 1)
        self._stat_card(stats, "历史课程", self._stat_history_var, 2)
        self._stat_card(stats, "平均进度", self._stat_progress_var, 3)

    def _stat_card(self, parent: Any, title: str, variable: tk.StringVar, column: int) -> None:
        card = self.app._card(parent)
        card.grid(row=0, column=column, sticky="nsew", padx=6, pady=2)
        ctk.CTkLabel(card, text=title, text_color=COLORS["muted"], font=sans_font(11)).pack(anchor="w", padx=14, pady=(11, 0))
        ctk.CTkLabel(card, textvariable=variable, text_color=COLORS["text"], font=mono_font(20, "bold")).pack(anchor="w", padx=14, pady=(4, 12))

    def _build_course_section(self) -> None:
        section = ctk.CTkFrame(self.data_frame, fg_color="transparent")
        section.pack(fill="x")
        title_row = ctk.CTkFrame(section, fg_color="transparent")
        title_row.pack(fill="x", padx=2, pady=(0, 8))
        ctk.CTkLabel(
            title_row,
            text="COURSES / 学历课程",
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
        filters.pack(fill="x", padx=2, pady=(0, 4))
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
                        self._courses = [item for item in courses if isinstance(item, dict)]
                    stats = payload.get("stats")
                    self._stats = stats if isinstance(stats, dict) else {}
                    if not self._profile and isinstance(payload.get("profile"), dict):
                        self._profile = payload["profile"]
            except Exception:
                pass
        self._refresh_ui()
        if self._profile or self._courses:
            self._status_var.set("已加载缓存")
            self._detail_var.set("已读取本机缓存；点击「同步全部数据」才会启动独立 Edge 并刷新数据。")
            self._status_dot.configure(text_color=COLORS["accent"])
            self._status_label.configure(text_color=COLORS["accent"])
        else:
            self._status_var.set("等待手动同步")
            self._detail_var.set("不会自动启动；点击「同步全部数据」后才会打开独立 Edge 并引导扫码登录。")

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
        self.avatar_label.configure(text=name[:2] if name and name != "未获取用户信息" else "学")
        if fetched_at:
            self._last_sync_var.set(f"上次同步 {fetched_at}")

        stats = self._stats if isinstance(self._stats, dict) else {}
        total = int(stats.get("total", len(self._courses)) or len(self._courses))
        current = int(stats.get("current", sum(1 for item in self._courses if item.get("is_current"))) or 0)
        history = int(stats.get("history", max(0, total - current)) or 0)
        progress = stats.get("average_progress")
        self._stat_total_var.set(str(total))
        self._stat_current_var.set(str(current))
        self._stat_history_var.set(str(history))
        self._stat_progress_var.set(f"{float(progress):.1f}%" if isinstance(progress, (int, float)) else "--")
        self._course_count_var.set(f"共 {total} 门" if total else "暂无课程")
        self._rebuild_course_list()
        self._show_state("data" if (self._profile or self._courses) else "empty")

    def _filtered_courses(self) -> list[dict[str, Any]]:
        if self._course_filter == "current":
            return [course for course in self._courses if course.get("is_current")]
        if self._course_filter == "history":
            return [course for course in self._courses if not course.get("is_current")]
        return list(self._courses)

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
        courses = self._filtered_courses()
        if not courses:
            text = "暂未同步到课程，点击「同步全部数据」开始获取。" if not self._courses else "当前筛选条件下没有课程。"
            ctk.CTkLabel(
                container,
                text=text,
                text_color=COLORS["muted"],
                font=sans_font(11),
            ).pack(fill="x", padx=18, pady=28)
            return
        for course in courses:
            self._build_course_card(container, course)

    def _build_course_card(self, parent: Any, course: dict[str, Any]) -> None:
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
            wraplength=720,
            justify="left",
        ).pack(fill="x")
        tags = ctk.CTkFrame(title_box, fg_color="transparent")
        tags.pack(fill="x", pady=(5, 0))
        self._tag(tags, str(course.get("type") or "课程"), COLORS["accent"], "#ffffff")
        self._tag(tags, str(course.get("term") or "未标注学期"), "#f2f2f0", COLORS["muted"])
        self._tag(
            tags,
            str(course.get("status") or "课程"),
            "#e8f7ee" if course.get("is_current") else "#f2f2f0",
            "#15803d" if course.get("is_current") else COLORS["muted"],
        )
        action_text = str(course.get("action_text") or ("进入学习" if course.get("is_current") else "回顾课程"))
        action = self.app._button(top, action_text, lambda item=course: self.open_course(item), "primary" if course.get("is_current") else "secondary", 96)
        action.pack(side="right", padx=(14, 0), pady=(2, 0))
        if not course.get("course_url"):
            action.configure(state="disabled")

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
                progress_color=COLORS["accent"],
            )
            bar.pack(side="left", fill="x", expand=True, padx=(4, 10))
            bar.set(max(0.0, min(1.0, float(progress) / 100.0)))
            ctk.CTkLabel(
                progress_row,
                text=f"{float(progress):.1f}%",
                text_color=COLORS["text"],
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
        if isinstance(course.get("score"), (int, float)) and course.get("is_current"):
            detail_parts.append(f"总评成绩 {float(course['score']):g}")
        if detail_parts:
            ctk.CTkLabel(
                card,
                text=" · ".join(detail_parts),
                text_color=COLORS["muted"],
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

    # ------------------------------------------------------------------ 事件与命令

    def start_refresh(self) -> None:
        if self._sync_running or self.runtime.is_running():
            return
        self._sync_running = True
        self._set_sync_buttons_state("disabled")
        self._status_var.set("同步中")
        self._status_dot.configure(text_color=COLORS["warning"])
        self._status_label.configure(text_color=COLORS["warning"])
        self._detail_var.set("正在启动独立 Edge 并读取个人空间…")
        self.sync_progress.set(0.05)
        self.submit("chaoxing_refresh")

    def start_study(self) -> None:
        if self._sync_running or self.runtime.is_running():
            return
        if not (self._profile or self._courses):
            messagebox.showinfo("学习通助手", "请先同步课程数据，再开始学习。")
            return
        self._sync_running = True
        self._set_sync_buttons_state("disabled")
        self._status_var.set("学习运行中")
        self._status_dot.configure(text_color=COLORS["success"])
        self._status_label.configure(text_color=COLORS["success"])
        self._detail_var.set("正在检查未完成课程并进入章节…")
        self.sync_progress.set(0.05)
        self.submit("chaoxing_study")

    def cancel_refresh(self) -> None:
        self.runtime.cancel()
        self._status_var.set("取消中")
        self._detail_var.set("正在停止同步，请稍候…")

    def open_space(self) -> None:
        self._detail_var.set("正在打开独立 Edge 中的学习通个人空间…")
        self.submit("chaoxing_open_space")

    def open_course(self, course: dict[str, Any]) -> None:
        url = str(course.get("course_url") or "").strip()
        if not url:
            messagebox.showinfo("学习通助手", "该课程没有可用的进入地址。")
            return
        self._detail_var.set(f"正在打开课程：{course.get('title') or ''}")
        self.submit("chaoxing_open_course", url=url)

    def clear_cache(self) -> None:
        if self._sync_running or self.runtime.is_running():
            self.log("同步正在进行，暂时不能清理学习通缓存。", "warning")
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
        self._stats = {}
        self._refresh_ui()
        self._empty_reason_var.set("缓存已清理。点击「同步全部数据」重新登录或刷新学习通数据。")
        self._status_var.set("缓存已清理")
        self._status_dot.configure(text_color=COLORS["muted"])
        self._status_label.configure(text_color=COLORS["muted"])
        self._detail_var.set("缓存已清理；点击「同步全部数据」重新获取。")
        self._last_sync_var.set("尚未同步")
        self.sync_progress.set(0)
        self.log(f"已清理学习通本地缓存：{'、'.join(removed) if removed else '无文件'}。", "success")

    def open_data_file(self) -> None:
        if not self.courses_file.exists():
            messagebox.showinfo("学习通助手", "还没有课程缓存，请先同步全部数据。")
            return
        os.startfile(self.courses_file)

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
                    self._courses = [item for item in courses if isinstance(item, dict)]
                stats = data.get("stats")
                self._stats = stats if isinstance(stats, dict) else {}
                if isinstance(data.get("profile"), dict):
                    self._profile = data["profile"]
                self._refresh_ui()
        elif event_type == "chaoxing_profile_status":
            message = str(event.get("message") or "正在读取基本信息…")
            self._detail_var.set(message)
            if not (self._profile or self._courses):
                self._empty_reason_var.set(message)
        elif event_type == "chaoxing_run_status":
            self._apply_run_status(event)
        elif event_type == "status":
            state = str(event.get("state") or "")
            detail = str(event.get("detail") or "")
            if state == "stopped":
                self._detail_var.set(detail or "独立 Edge 已停止。")
        elif event_type == "command_done":
            command = str(event.get("command") or "")
            if command in {"chaoxing_refresh", "chaoxing_study"} and not self._sync_running:
                self._set_sync_buttons_state("normal")
        elif event_type == "plugin_process_error":
            self._sync_running = False
            self._set_sync_buttons_state("normal")
            self._status_var.set("异常")
            self._status_dot.configure(text_color=COLORS["danger"])
            self._status_label.configure(text_color=COLORS["danger"])
            self._detail_var.set(str(event.get("message") or "插件进程异常。"))

    def _apply_run_status(self, event: dict[str, Any]) -> None:
        state = str(event.get("state") or "")
        message = str(event.get("message") or "学习通状态更新")
        mode = str(event.get("mode") or "")
        current = event.get("current")
        total = event.get("total")
        if isinstance(current, (int, float)) and isinstance(total, (int, float)) and total:
            self.sync_progress.set(max(0.0, min(1.0, float(current) / float(total))))
        if state in {"running", "login_required"}:
            self._sync_running = True
            self._set_sync_buttons_state("disabled")
            color = COLORS["warning"] if state == "login_required" else COLORS["success"]
            self._status_var.set("等待扫码登录" if state == "login_required" else ("学习运行中" if mode == "study" else "同步中"))
            self._status_dot.configure(text_color=color)
            self._status_label.configure(text_color=color)
            self._detail_var.set(message)
            if not (self._profile or self._courses):
                self._empty_reason_var.set(message)
        elif state == "done":
            self._sync_running = False
            self._set_sync_buttons_state("normal")
            self.sync_progress.set(1.0)
            self._status_var.set("学习完成" if mode == "study" else "已同步")
            self._status_dot.configure(text_color=COLORS["success"])
            self._status_label.configure(text_color=COLORS["success"])
            self._detail_var.set(message)
            self._last_sync_var.set(f"学习完成 {now_text()}" if mode == "study" else f"上次同步 {now_text()}")
            self._show_state("data" if (self._profile or self._courses) else "empty")
        elif state == "warning":
            self._status_var.set("课程异常")
            self._status_dot.configure(text_color=COLORS["warning"])
            self._status_label.configure(text_color=COLORS["warning"])
            self._detail_var.set(message)
            if not (self._profile or self._courses):
                self._empty_reason_var.set(message)
        elif state in {"error", "cancelled"}:
            self._sync_running = False
            self._set_sync_buttons_state("normal")
            color = COLORS["danger"] if state == "error" else COLORS["warning"]
            self._status_var.set("异常" if state == "error" else "已取消")
            self._status_dot.configure(text_color=color)
            self._status_label.configure(text_color=color)
            self._detail_var.set(message)
            if not (self._profile or self._courses):
                self._empty_reason_var.set(message)
        elif state == "opened":
            self._detail_var.set(message)
            if not (self._profile or self._courses):
                self._empty_reason_var.set(message)
        else:
            self._detail_var.set(message)