"""桌面 UI：CustomTkinter 工作台主窗口与程序入口 main()。"""
from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk

from . import ai_manager
from .ai_manager import AIError

from .config import (
    APP_DIR,
    CARD_CORNER_RADIUS,
    COLORS,
    CONTENT_MARGIN_B,
    CONTENT_MARGIN_L,
    CONTENT_MARGIN_R,
    CONTENT_MARGIN_T,
    DEFAULT_PET_ID,
    FONT_MONO,
    FONT_SANS,
    HOME_URL,
    ICON_CLEAN,
    ICON_FOLDER,
    ICON_NEW_TAB,
    ICON_PLAY,
    ICON_REFRESH,
    ICON_RESTART,
    ICON_SNAPSHOT,
    ICON_STOP,
    ICON_VISUALIZE,
    ICON_WINDOW,
    OPERATIONS_LOG,
    PETS,
    PLUGINS_DIR,
    PROFILE_DIR,
    ensure_directories,
    find_edge_executable,
    format_bytes,
    load_workbench_settings,
    mono_font,
    now_text,
    sans_font,
)
from .plugin_runtime import PluginManager
from .worker import BrowserWorker


class EdgeWorkbenchApp(ctk.CTk):
    def __init__(self) -> None:
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title("Microsoft Edge Workbench")
        self.geometry("1240x860")
        self.minsize(1040, 720)
        self.configure(fg_color=COLORS["bg"])
        self.overrideredirect(True)

        self._maximized = False
        self._restore_geometry = ""
        self._drag_offset_x = 0
        self._drag_offset_y = 0
        self._resize_origin = (0, 0, 0, 0)
        self._resizing = False
        self._current_page = ""
        self._closing = False
        self._close_deadline = 0.0
        self._busy_text = ""
        self._plugin_status: dict[str, dict[str, str]] = {}
        self._nav_labels: dict[str, str] = {}

        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.worker = BrowserWorker(self.events)
        self.worker.start()
        self.plugin_manager = PluginManager(self)
        self.plugin_manager.discover()
        self.plugin_manager.load_plugins()
        self._metric_labels: dict[str, ctk.CTkLabel] = {}
        self._nav_buttons: dict[str, ctk.CTkLabel] = {}
        self._nav_containers: dict[str, ctk.CTkFrame] = {}
        self._plugin_page_keys: dict[str, list[str]] = {}
        self._command_buttons: dict[str, list[ctk.CTkButton]] = {}
        self._url_var = tk.StringVar(value=HOME_URL)
        self._status_var = tk.StringVar(value="未运行")
        self._status_detail_var = tk.StringVar(value="Playwright 持久化会话尚未启动")
        self._cache_cleanup_var = tk.StringVar(value="可回收缓存：--")
        self._last_stats_refresh = 0.0
        self._spider_overlay_visible = bool(load_workbench_settings().get("spider_overlay"))
        self._active_pet_id = str(load_workbench_settings().get("spider_pet") or DEFAULT_PET_ID)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Alt-F4>", lambda _event: self._on_close())
        self._rebind_nav_hotkeys()
        self.after(120, self._apply_window_shape)
        self.after(100, self._poll_events)
        self.after(300, self._request_stats)
        for plugin in self.plugin_manager.plugins:
            self._append_log(f"插件已加载：{plugin.display_name}（{plugin.id}）", "success")
        if self.plugin_manager.disabled_ids:
            self._append_log(f"已禁用插件：{'、'.join(sorted(self.plugin_manager.disabled_ids))}", "info")

    def _get_hwnd(self) -> int:
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            return int(hwnd or self.winfo_id())
        except Exception:
            return int(self.winfo_id())

    def _apply_window_shape(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = self._get_hwnd()
            corner = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
            ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ex_style = (ex_style & ~0x00000080) | 0x00040000
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, ex_style)
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
        except Exception:
            pass

    def _window_button(self, parent: Any, text: str, command: Any, hover: str) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=38,
            height=30,
            corner_radius=7,
            fg_color="transparent",
            hover_color=hover,
            text_color=COLORS["muted"],
            font=("Segoe UI Symbol", 12),
        )

    def _button(self, parent: Any, text: str, command: Any, kind: str = "secondary", width: int = 140) -> ctk.CTkButton:
        palette = {
            "primary": (COLORS["accent"], COLORS["accent_hover"], "#ffffff"),
            "secondary": ("#f2f2f0", "#e7e7e4", COLORS["text"]),
            "ghost": ("transparent", "#ececea", COLORS["muted"]),
            "danger": ("#fdf3f3", "#fbdcdc", "#b91c1c"),
        }
        fg, hover, fg_text = palette.get(kind, palette["secondary"])
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=34,
            corner_radius=10,
            fg_color=fg,
            hover_color=hover,
            text_color=fg_text,
            text_color_disabled="#9c9c98",
            border_width=0,
            font=sans_font(11, "bold"),
        )

    def _icon_button(self, parent: Any, glyph: str, command: Any, tip: str, kind: str = "secondary") -> ctk.CTkButton:
        """只显示图标的小方按钮（Segoe MDL2 Assets 字形），悬停显示中文名称。"""
        palette = {
            "primary": (COLORS["accent"], COLORS["accent_hover"], "#ffffff"),
            "secondary": ("#f2f2f0", "#e7e7e4", COLORS["text"]),
            "danger": ("#fdf3f3", "#fbdcdc", "#b91c1c"),
        }
        fg, hover, fg_text = palette.get(kind, palette["secondary"])
        button = ctk.CTkButton(
            parent,
            text=glyph,
            command=command,
            width=34,
            height=34,
            corner_radius=8,
            fg_color=fg,
            hover_color=hover,
            text_color=fg_text,
            text_color_disabled="#9c9c98",
            border_width=0,
            font=("Segoe MDL2 Assets", 12),
        )
        self._attach_tooltip(button, tip)
        return button

    def _attach_tooltip(self, widget: Any, text: str) -> None:
        """图标按钮的悬停提示：进入显示悬浮小窗，离开销毁。"""
        state: dict[str, tk.Toplevel | None] = {"win": None}

        def enter(_event: Any) -> None:
            if state["win"] is not None:
                return
            x = widget.winfo_rootx() + widget.winfo_width() + 8
            y = widget.winfo_rooty() + max(0, (widget.winfo_height() - 26) // 2)
            win = tk.Toplevel(widget)
            win.wm_overrideredirect(True)
            win.wm_geometry(f"+{x}+{y}")
            tk.Label(win, text=text, bg="#242422", fg="#ffffff", font=(FONT_SANS, 9), padx=9, pady=4).pack()
            state["win"] = win

        def leave(_event: Any) -> None:
            if state["win"] is not None:
                state["win"].destroy()
                state["win"] = None

        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)

    def _card(self, parent: Any, padded: bool = False) -> ctk.CTkFrame:
        card = ctk.CTkFrame(
            parent,
            fg_color=COLORS["panel"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
        )
        if padded:
            card.pack(fill="x", padx=24, pady=(0, 12))
        return card

    @staticmethod
    def _section_label(parent: Any, text: str, top: int = 14) -> ctk.CTkLabel:
        label = ctk.CTkLabel(parent, text=text, text_color=COLORS["muted"], font=sans_font(12, "bold"), anchor="w")
        label.pack(fill="x", padx=16, pady=(top, 6))
        return label

    def _bind_busy(self, command: str, *buttons: ctk.CTkButton) -> None:
        """注册按钮与 worker 命令的关联：提交命令时禁用，命令完成后恢复。"""
        for button in buttons:
            self._command_buttons.setdefault(command, []).append(button)

    def _disable_for(self, command: str) -> None:
        for button in self._command_buttons.get(command, []):
            try:
                button.configure(state="disabled")
            except tk.TclError:
                pass  # 插件页可能已被热卸载

    def _metric_card(self, parent: Any, key: str, label: str, row: int, column: int) -> None:
        card = self._card(parent)
        card.grid(row=row, column=column, sticky="nsew", padx=6, pady=6)
        ctk.CTkLabel(card, text=label, text_color=COLORS["muted"], font=sans_font(11)).pack(anchor="w", padx=14, pady=(11, 0))
        value = ctk.CTkLabel(card, text="--", text_color=COLORS["text"], font=mono_font(16, "bold"))
        value.pack(anchor="w", padx=14, pady=(4, 12))
        self._metric_labels[key] = value

    def _start_drag(self, event: Any) -> None:
        if self._maximized:
            return
        self._drag_offset_x = event.x_root - self.winfo_x()
        self._drag_offset_y = event.y_root - self.winfo_y()

    def _on_drag(self, event: Any) -> None:
        if self._maximized:
            return
        x = event.x_root - self._drag_offset_x
        y = event.y_root - self._drag_offset_y
        self.geometry(f"+{x}+{y}")

    def _toggle_maximize(self) -> None:
        if self._maximized:
            if self._restore_geometry:
                self.geometry(self._restore_geometry)
            self._maximized = False
        else:
            self._restore_geometry = self.geometry()
            work_x, work_y, work_w, work_h = self._work_area()
            self.geometry(f"{work_w}x{work_h}+{work_x}+{work_y}")
            self._maximized = True

    def _work_area(self) -> tuple[int, int, int, int]:
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            return 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()

    def _minimize_window(self) -> None:
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(self._get_hwnd(), 6)
        except Exception:
            self.iconify()

    def _start_resize(self, event: Any) -> None:
        if self._maximized:
            self._resizing = False
            return
        x = event.x_root - self.winfo_rootx()
        y = event.y_root - self.winfo_rooty()
        in_corner = x >= self.winfo_width() - 24 and y >= self.winfo_height() - 24
        self._resizing = in_corner
        if in_corner:
            self._resize_origin = (event.x_root, event.y_root, self.winfo_width(), self.winfo_height())

    def _on_resize(self, event: Any) -> None:
        if not self._resizing:
            return
        start_x, start_y, start_w, start_h = self._resize_origin
        width = max(1040, start_w + (event.x_root - start_x))
        height = max(720, start_h + (event.y_root - start_y))
        self.geometry(f"{width}x{height}")

    def _finish_resize(self, _event: Any) -> None:
        self._resizing = False

    def _build_ui(self) -> None:
        self._build_titlebar()
        body = ctk.CTkFrame(self, fg_color=COLORS["panel_alt"], corner_radius=0)
        body.pack(fill="both", expand=True)

        sidebar = ctk.CTkFrame(body, width=216, corner_radius=0, fg_color=COLORS["panel_alt"], border_width=0)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        content_host = ctk.CTkFrame(body, fg_color="transparent", corner_radius=0)
        content_host.pack(side="left", fill="both", expand=True)
        self.shadow_canvas = tk.Canvas(content_host, bg=COLORS["panel_alt"], highlightthickness=0, bd=0)
        self.shadow_canvas.pack(fill="both", expand=True)
        self.shadow_canvas.bind("<Configure>", self._on_content_resize)
        # 页面容器四边内缩一个圆角半径：方形的它不再遮挡画布上绘制的圆角与阴影
        inset = CARD_CORNER_RADIUS
        pages_frame = ctk.CTkFrame(content_host, fg_color=COLORS["panel"], corner_radius=0, bg_color=COLORS["panel_alt"])
        pages_frame.place(x=CONTENT_MARGIN_L + inset, y=CONTENT_MARGIN_T + inset, anchor="nw")
        pages_frame.pack_propagate(False)
        self._pages_frame = pages_frame
        self.content = ctk.CTkFrame(pages_frame, fg_color="transparent", corner_radius=0)
        self.content.pack(fill="both", expand=True)
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        # 核心页面 + 插件页面：插件页面在前（安装即出现在侧栏顶部）
        self.pages: dict[str, ctk.CTkFrame] = {}
        nav_items: list[tuple[str, str]] = []
        for plugin in self.plugin_manager.plugins:
            try:
                page_keys: list[str] = []
                for key, label, frame in plugin.build_pages(self.content):
                    self.pages[key] = frame
                    nav_items.append((key, label))
                    page_keys.append(key)
                self._plugin_page_keys[plugin.id] = page_keys
            except Exception as exc:
                self.plugin_manager.errors[plugin.id] = str(exc)
                self._append_log(f"插件页面构建失败 {plugin.id}: {exc}", "warning")
        self.pages["overview"] = self._build_overview_page()
        self.pages["pet"] = self._build_pet_page()
        self.pages["agents"] = self._build_agents_page()
        self.pages["logs"] = self._build_logs_page()
        self.pages["plugins"] = self._build_plugins_page()
        nav_items += [("overview", "总览"), ("pet", "宠物"), ("agents", "Agent 管理"), ("logs", "运行日志"), ("plugins", "插件管理")]

        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")
        self._build_nav(sidebar, nav_items)
        for plugin in self.plugin_manager.plugins:
            self._set_plugin_status(plugin.id, "idle", "未启动")
        self._show_page(nav_items[0][0] if nav_items else "overview")

        # 右下角使用透明热区缩放窗口，不再显示可见方块。
        self.bind_all("<ButtonPress-1>", self._start_resize, add="+")
        self.bind_all("<B1-Motion>", self._on_resize, add="+")
        self.bind_all("<ButtonRelease-1>", self._finish_resize, add="+")

    def _build_nav(self, sidebar: Any, nav_items: list[tuple[str, str]]) -> None:
        self._sidebar = sidebar
        for key, label in nav_items:
            self._add_nav_item(key, label)
        footer = ctk.CTkFrame(sidebar, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=18, pady=16)
        ctk.CTkLabel(footer, text="channel=msedge", text_color=COLORS["muted"], font=mono_font(10), anchor="w").pack(fill="x")
        self._nav_footer_label = ctk.CTkLabel(footer, text="", text_color=COLORS["muted"], font=sans_font(11), anchor="w")
        self._nav_footer_label.pack(fill="x", pady=(3, 0))
        self._update_nav_footer()

    def _add_nav_item(self, key: str, label: str, before_key: str | None = None) -> None:
        """向侧栏追加一个导航项；before_key 给出时插到该项之前（插件页插在核心页前面）。"""
        if key in self._nav_buttons or getattr(self, "_sidebar", None) is None:
            return
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        nav_overflow = 10  # 选中条向左溢出窗口边缘：左侧被裁平，右上右下保留圆角
        container = ctk.CTkFrame(self._sidebar, fg_color="transparent", height=40)
        container.pack(fill="x", pady=(16, 1) if not self._nav_containers else 1, before=self._nav_containers[before_key] if before_key in self._nav_containers else None)
        container.pack_propagate(False)
        item = ctk.CTkLabel(
            container,
            text=label,
            anchor="w",
            padx=16,
            height=40,
            width=216 + nav_overflow,
            corner_radius=10,
            fg_color="transparent",
            text_color=COLORS["muted"],
            font=sans_font(12, "bold"),
            cursor="hand2",
        )
        item.place(x=-int(nav_overflow * scaling), rely=0.5, anchor="w")
        item.bind("<Button-1>", lambda _event, page=key: self._show_page(page))
        item.bind("<Enter>", lambda _event, widget=item: widget.configure(text_color=COLORS["text"]) if widget.cget("fg_color") == "transparent" else None)
        item.bind("<Leave>", lambda _event, page=key: self._refresh_nav_item(page))
        self._nav_containers[key] = container
        self._nav_buttons[key] = item
        self._nav_labels[key] = label
        self._sync_nav_order()
        self._rebind_nav_hotkeys()
        self._update_nav_footer()

    def _remove_nav_item(self, key: str) -> None:
        container = self._nav_containers.pop(key, None)
        if container is not None:
            container.destroy()
        self._nav_buttons.pop(key, None)
        self._nav_labels.pop(key, None)
        self._sync_nav_order()
        self._rebind_nav_hotkeys()
        self._update_nav_footer()

    def _sync_nav_order(self) -> None:
        """让导航字典顺序与侧栏实际打包顺序一致（热重装插到中间时保持 Alt 键映射正确）。"""
        sidebar = getattr(self, "_sidebar", None)
        if sidebar is None:
            return
        by_container = {id(container): key for key, container in self._nav_containers.items()}
        order: list[str] = []
        for slave in sidebar.pack_slaves():
            key = by_container.get(id(slave))
            if key is not None:
                order.append(key)
        self._nav_containers = {key: self._nav_containers[key] for key in order}
        self._nav_buttons = {key: self._nav_buttons[key] for key in order if key in self._nav_buttons}

    def _rebind_nav_hotkeys(self) -> None:
        """按当前导航顺序重绑 Alt+1…N。"""
        for index in range(1, 10):
            try:
                self.unbind(f"<Alt-Key-{index}>")
            except tk.TclError:
                pass
        for index, key in enumerate(self._nav_buttons, start=1):
            self.bind(f"<Alt-Key-{index}>", lambda _event, page=key: self._show_page(page))

    def _update_nav_footer(self) -> None:
        label = getattr(self, "_nav_footer_label", None)
        if label is not None:
            last = len(self._nav_buttons)
            label.configure(text=f"Alt+1…{last} 切换页面" if last else "Alt+N 切换页面")

    def _on_content_resize(self, _event: Any = None) -> None:
        canvas = getattr(self, "shadow_canvas", None)
        if canvas is None:
            return
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        inset = CARD_CORNER_RADIUS
        pages_frame = getattr(self, "_pages_frame", None)
        if pages_frame is not None:
            pages_frame.configure(
                width=max(80, width - CONTENT_MARGIN_L - CONTENT_MARGIN_R - 2 * inset),
                height=max(80, height - CONTENT_MARGIN_T - CONTENT_MARGIN_B - 2 * inset),
            )
        self._draw_content_shadow(width, height)

    def _draw_content_shadow(self, width: int, height: int) -> None:
        """在画布上绘制内容卡片：阴影由外向内逐层加深，白色圆角卡片本体最后绘制。"""
        canvas = getattr(self, "shadow_canvas", None)
        if canvas is None or width < 80 or height < 80:
            return
        canvas.delete("all")
        depth = 8
        steps = 8
        r = CARD_CORNER_RADIUS
        x0, y0 = CONTENT_MARGIN_L, CONTENT_MARGIN_T
        x1, y1 = width - CONTENT_MARGIN_R, height - CONTENT_MARGIN_B
        bg_hex = COLORS["panel_alt"].lstrip("#")
        bg = tuple(int(bg_hex[i:i + 2], 16) for i in (0, 2, 4))
        dark = (219, 219, 216)
        for i in range(steps):
            e = depth - i * (depth / steps)
            t = i / (steps - 1)
            shade = "#%02x%02x%02x" % tuple(int(b + (d - b) * t) for b, d in zip(bg, dark))
            self._filled_round_rect(canvas, x0 - e, y0 - e, x1 + e, y1 + e, r + e, shade)
        self._filled_round_rect(canvas, x0, y0, x1, y1, r, COLORS["panel"])

    @staticmethod
    def _filled_round_rect(canvas: Any, x0: float, y0: float, x1: float, y1: float, radius: float, fill: str) -> None:
        rr = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
        points = [
            x0 + rr, y0, x1 - rr, y0, x1, y0, x1, y0 + rr,
            x1, y1 - rr, x1, y1, x1 - rr, y1, x0 + rr, y1,
            x0, y1, x0, y1 - rr, x0, y0 + rr, x0, y0,
        ]
        canvas.create_polygon(points, smooth=True, fill=fill, outline=fill, width=1)

    def _build_titlebar(self) -> None:
        self.title_bar = ctk.CTkFrame(self, height=46, corner_radius=0, fg_color=COLORS["panel_alt"], border_width=0)
        self.title_bar.pack(fill="x")
        self.title_bar.pack_propagate(False)

        brand = ctk.CTkFrame(self.title_bar, fg_color="transparent")
        brand.pack(side="left", fill="y", padx=(16, 0))
        self._brand_title = ctk.CTkLabel(brand, text="▌ edge workbench", text_color=COLORS["text"], font=mono_font(12, "bold"))
        self._brand_title.pack(side="left", pady=13)
        self._brand_subtitle = ctk.CTkLabel(brand, text="  ·  persistent playwright session", text_color=COLORS["muted"], font=mono_font(10))
        self._brand_subtitle.pack(side="left", pady=17)

        controls = ctk.CTkFrame(self.title_bar, fg_color="transparent")
        controls.pack(side="right", fill="y", padx=(0, 8), pady=8)
        self._window_button(controls, "—", self._minimize_window, "#e9e9e6").pack(side="left", padx=2)
        self._window_button(controls, "□", self._toggle_maximize, "#e9e9e6").pack(side="left", padx=2)
        self._window_button(controls, "✕", self._on_close, "#fee2e2").pack(side="left", padx=2)

        for widget in (self.title_bar, brand, self._brand_title, self._brand_subtitle):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)
            widget.bind("<Double-Button-1>", lambda _event: self._toggle_maximize())

    def _set_busy(self, text: str = "") -> None:
        """Keep legacy busy-state messages without showing a global top bar."""
        self._busy_text = text

    def _show_page(self, page_name: str) -> None:
        self._current_page = page_name
        for key, button in self._nav_buttons.items():
            selected = key == page_name
            button.configure(
                fg_color=COLORS["panel"] if selected else "transparent",
                text_color=self._nav_item_color(key, selected),
            )
        for key, page in self.pages.items():
            if key == page_name:
                page.grid(row=0, column=0, sticky="nsew")
            else:
                page.grid_remove()

    def _plugin_id_for_page(self, page_key: str) -> str | None:
        for plugin_id, keys in getattr(self, "_plugin_page_keys", {}).items():
            if page_key in keys:
                return plugin_id
        return None

    def _nav_item_color(self, page_key: str, selected: bool = False) -> str:
        plugin_id = self._plugin_id_for_page(page_key)
        status = self._plugin_status.get(plugin_id, {}) if plugin_id else {}
        state = str(status.get("state") or "")
        if state in {"idle", "stopped", ""}:
            return COLORS["text"] if selected else COLORS["muted"]
        return str(status.get("color") or (COLORS["text"] if selected else COLORS["muted"]))

    def _refresh_nav_item(self, page_key: str) -> None:
        item = self._nav_buttons.get(page_key)
        if item is None:
            return
        selected = page_key == self._current_page
        try:
            item.configure(text_color=self._nav_item_color(page_key, selected))
        except tk.TclError:
            pass

    def _set_plugin_status(self, plugin_id: str, state: str, text: str = "", detail: str = "") -> None:
        """Store and render sidebar-level status for one isolated plugin."""
        mapping = {
            "idle": ("未启动", COLORS["muted"]),
            "stopped": ("未启动", COLORS["muted"]),
            "ready": ("就绪", COLORS["accent"]),
            "starting": ("启动中", COLORS["warning"]),
            "running": ("运行中", COLORS["success"]),
            "cancelling": ("取消中", COLORS["warning"]),
            "done": ("已完成", COLORS["success"]),
            "error": ("异常", COLORS["danger"]),
            "cancelled": ("已取消", COLORS["warning"]),
        }
        default_text, color = mapping.get(str(state), ("运行中", COLORS["success"]))
        short_text = str(text or default_text).strip()
        if len(short_text) > 5:
            short_text = default_text
        self._plugin_status[plugin_id] = {
            "state": str(state),
            "text": short_text,
            "detail": str(detail),
            "color": color,
        }
        page_keys = getattr(self, "_plugin_page_keys", {}).get(plugin_id, [])
        if page_keys:
            key = page_keys[0]
            item = self._nav_buttons.get(key)
            base_label = self._nav_labels.get(key, plugin_id)
            if item is not None:
                try:
                    item.configure(text=f"{base_label}  ·  {short_text}")
                    self._refresh_nav_item(key)
                except tk.TclError:
                    pass

    def _page_header(self, parent: Any, title: str, subtitle: str) -> None:
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(20, 12))
        ctk.CTkLabel(header, text=title, text_color=COLORS["text"], font=sans_font(20, "bold")).pack(anchor="w")
        ctk.CTkLabel(header, text=subtitle, text_color=COLORS["muted"], font=sans_font(11)).pack(anchor="w", pady=(4, 0))
        return header

    def _build_overview_page(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self.content, fg_color="transparent", corner_radius=0)
        header = self._page_header(page, "运行总览", "持久化 Microsoft Edge 会话、缓存与自动化操作")

        # 标题行右侧：浏览器控制图标（启动 / 重启 / 隐藏切换 / 停止）
        header_actions = ctk.CTkFrame(header, fg_color="transparent")
        header_actions.pack(side="right")
        self.start_edge_button = self._icon_button(header_actions, ICON_PLAY, self.start_browser, "启动浏览器")
        self.start_edge_button.pack(side="left", padx=(0, 6))
        self.restart_edge_button = self._icon_button(header_actions, ICON_RESTART, self.restart_browser, "重启浏览器")
        self.restart_edge_button.pack(side="left", padx=(0, 6))
        self.headless_button = self._icon_button(header_actions, ICON_WINDOW, self._toggle_headless, "隐藏/显示浏览器窗口（黑色=隐藏已开启）")
        self.headless_button.pack(side="left", padx=(0, 6))
        self._bind_busy("restart", self.headless_button)
        self.spider_button = self._icon_button(header_actions, ICON_VISUALIZE, self.toggle_spider_overlay, "操作可视化：宠物联动特效（配合网页操作与数据获取展示，可在「宠物」页选择宠物）")
        self.spider_button.pack(side="left", padx=(0, 6))
        self._sync_spider_button()
        self.stop_button = self._icon_button(header_actions, ICON_STOP, self.stop_browser_and_task, "停止浏览器", "danger")
        self.stop_button.pack(side="left")
        ctk.CTkLabel(header, textvariable=self._status_detail_var, text_color=COLORS["muted"], font=sans_font(11)).pack(side="right", padx=(0, 14))

        command_bar = self._card(page, padded=True)
        self.url_entry = ctk.CTkEntry(
            command_bar,
            textvariable=self._url_var,
            height=42,
            corner_radius=10,
            fg_color="#fafaf8",
            border_color=COLORS["border"],
            text_color=COLORS["text"],
            placeholder_text="输入网址或搜索关键词，回车执行",
            font=mono_font(11),
        )
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(14, 10), pady=14)
        self.url_entry.bind("<Return>", lambda _event: self.open_url())
        self.open_page_button = self._button(command_bar, "打开页面", self.open_url, "primary", 100)
        self.open_page_button.pack(side="left", pady=14)
        self.bing_search_button = self._button(command_bar, "必应搜索", self.bing_search, "secondary", 110)
        self.bing_search_button.pack(side="left", padx=(8, 0), pady=14)
        self.new_tab_button = self._icon_button(command_bar, ICON_NEW_TAB, self.new_tab, "新建标签页")
        self.new_tab_button.pack(side="left", padx=(8, 14), pady=14)
        self._bind_busy("navigate", self.open_page_button)
        self._bind_busy("bing_search", self.bing_search_button)
        self._bind_busy("new_tab", self.new_tab_button)
        self._bind_busy("start", self.start_edge_button)
        self._bind_busy("restart", self.restart_edge_button)
        self._bind_busy("stop", self.stop_button)

        # 运行指标标题行：右侧工具图标（刷新 / 快照 / 清缓存 / 资料目录）
        metrics_header = ctk.CTkFrame(page, fg_color="transparent")
        metrics_header.pack(fill="x", padx=24, pady=(6, 0))
        ctk.CTkLabel(metrics_header, text="TELEMETRY / 运行指标", text_color=COLORS["muted"], font=sans_font(12, "bold"), anchor="w").pack(side="left")
        telemetry_actions = ctk.CTkFrame(metrics_header, fg_color="transparent")
        telemetry_actions.pack(side="right")
        self.refresh_button = self._icon_button(telemetry_actions, ICON_REFRESH, self._request_stats, "刷新统计")
        self.refresh_button.pack(side="left", padx=(0, 6))
        self.snapshot_button = self._icon_button(telemetry_actions, ICON_SNAPSHOT, self.save_snapshot, "保存状态快照")
        self.snapshot_button.pack(side="left", padx=(0, 6))
        self.clean_cache_button = self._icon_button(telemetry_actions, ICON_CLEAN, self.clean_browser_cache, "清理缓存（任务运行中会跳过）")
        self.clean_cache_button.pack(side="left", padx=(0, 6))
        self.open_profile_button = self._icon_button(telemetry_actions, ICON_FOLDER, self.open_profile_directory, "打开资料目录")
        self.open_profile_button.pack(side="left")
        self._bind_busy("snapshot", self.snapshot_button)
        self._bind_busy("clean_cache", self.clean_cache_button)

        metrics = ctk.CTkFrame(page, fg_color="transparent")
        metrics.pack(fill="x", padx=18, pady=(4, 8))
        for column in range(4):
            metrics.grid_columnconfigure(column, weight=1, uniform="metric")
        definitions = [
            ("profile_size", "全部用户数据"),
            ("cache_size", "HTTP 磁盘缓存"),
            ("site_data_size", "站点存储数据"),
            ("cookie_count", "Cookies 数量"),
            ("history_count", "历史记录条数"),
            ("page_count", "活动标签页"),
            ("history_size", "历史数据库"),
            ("profile_dir", "资料目录"),
        ]
        for index, (key, label) in enumerate(definitions):
            self._metric_card(metrics, key, label, index // 4, index % 4)

        hint = ctk.CTkFrame(page, fg_color="transparent")
        hint.pack(fill="x", padx=24, pady=(0, 12))
        ctk.CTkLabel(hint, text=f"$ edge: {find_edge_executable() or '未检测到'}", text_color=COLORS["muted"], font=sans_font(11)).pack(side="left")
        ctk.CTkLabel(hint, textvariable=self._cache_cleanup_var, text_color=COLORS["muted"], font=sans_font(11)).pack(side="left", padx=(16, 0))
        ctk.CTkLabel(hint, text=str(PROFILE_DIR), text_color=COLORS["muted"], font=mono_font(10)).pack(side="right")
        return page

    def _build_pet_page(self) -> ctk.CTkFrame:
        """宠物页：选择/管理操作可视化小生物（当前为蜘蛛、瓢虫、蜜蜂），即时切换生效。"""
        page = ctk.CTkFrame(self.content, fg_color="transparent", corner_radius=0)
        self._page_header(page, "宠物", "操作可视化小生物：蜘蛛现在陪你浏览网页、采集与自动化动作全程联动；更多宠物后续加入")

        self._pet_overlay_var = tk.IntVar(value=1 if self._spider_overlay_visible else 0)
        self._pet_status_var = tk.StringVar(value="")

        switch_card = self._card(page, padded=True)
        switch_row = ctk.CTkFrame(switch_card, fg_color="transparent")
        switch_row.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkSwitch(
            switch_row,
            text="启用宠物特效（操作可视化）",
            variable=self._pet_overlay_var,
            command=self._on_pet_toggle,
            font=sans_font(12, "bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            switch_row,
            textvariable=self._pet_status_var,
            text_color=COLORS["muted"],
            font=sans_font(10),
        ).pack(side="right")
        ctk.CTkLabel(
            switch_card,
            text="关闭后不再注入任何页面；此开关与总览页的“眼睛”图标同步，设置即时生效并写入本机 workbench.json。",
            text_color=COLORS["muted"],
            font=sans_font(10),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 12))

        pets_title = ctk.CTkFrame(page, fg_color="transparent")
        pets_title.pack(fill="x", padx=26, pady=(14, 8))
        ctk.CTkLabel(
            pets_title, text="PETS / 选择宠物", text_color=COLORS["muted"], font=sans_font(11, "bold")
        ).pack(side="left")

        grid = ctk.CTkFrame(page, fg_color="transparent")
        grid.pack(fill="x", padx=20)
        for column in range(len(PETS)):
            grid.grid_columnconfigure(column, weight=1, uniform="pet-card")
        self._pet_cards: dict[str, ctk.CTkButton] = {}
        for column, pet in enumerate(PETS):
            self._build_pet_card(grid, pet, column)

        play_card = self._card(page, padded=True)
        play_row = ctk.CTkFrame(play_card, fg_color="transparent")
        play_row.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(play_row, text="互动", text_color=COLORS["text"], font=sans_font(12, "bold")).pack(side="left")
        ctk.CTkLabel(
            play_row,
            text="作用于当前正在浏览的标签页；浏览器未运行或特效关闭时会在日志里提示。",
            text_color=COLORS["muted"],
            font=sans_font(10),
        ).pack(side="right")
        actions = ctk.CTkFrame(play_card, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=(6, 12))
        self._button(actions, "召唤到页面中央", lambda: self._pet_play("call"), "secondary", 130).pack(side="left")
        self._button(actions, "开心庆祝", lambda: self._pet_play("celebrate"), "secondary", 100).pack(side="left", padx=(8, 0))
        self._button(actions, "撒一把粒子", lambda: self._pet_play("burst"), "secondary", 110).pack(side="left", padx=(8, 0))
        self._sync_pet_cards()
        return page

    def _build_pet_card(self, parent: Any, pet: dict[str, Any], column: int) -> None:
        card = self._card(parent)
        card.grid(row=0, column=column, sticky="nsew", padx=6, pady=2)
        ctk.CTkLabel(card, text=pet["glyph"], text_color=COLORS["text"], font=("Segoe UI Emoji", 32)).pack(pady=(14, 2))
        ctk.CTkLabel(card, text=pet["name"], text_color=COLORS["text"], font=sans_font(14, "bold")).pack()
        ctk.CTkLabel(
            card,
            text=pet["description"],
            text_color=COLORS["muted"],
            font=sans_font(10),
            wraplength=340,
            justify="center",
        ).pack(fill="x", padx=14, pady=(4, 10))
        button = self._button(card, "应用", lambda item=pet["id"]: self._apply_pet_choice(item), "primary", 96)
        button.pack(pady=(0, 14))
        self._pet_cards[pet["id"]] = button

    def _sync_pet_cards(self) -> None:
        for pet_id, button in getattr(self, "_pet_cards", {}).items():
            active = pet_id == self._active_pet_id
            button.configure(
                text="使用中" if active else "应用",
                state="disabled" if active else "normal",
                fg_color=COLORS["accent"] if active else "#f2f2f0",
                hover_color=COLORS["accent_hover"] if active else "#e7e7e4",
                text_color="#ffffff" if active else COLORS["text"],
            )
        self._sync_pet_status()

    def _sync_pet_status(self) -> None:
        var = getattr(self, "_pet_status_var", None)
        if var is None:
            return
        pet = next((item for item in PETS if item["id"] == self._active_pet_id), PETS[0])
        state_text = "已开启" if self._spider_overlay_visible else "已关闭（页面上不会出现宠物）"
        var.set(f"当前宠物：{pet['name']} · 宠物特效{state_text}")

    def _apply_pet_choice(self, pet_id: str) -> None:
        pet = next((item for item in PETS if item["id"] == str(pet_id)), None)
        if pet is None or pet["id"] == self._active_pet_id:
            return
        self._active_pet_id = pet["id"]
        self.worker.submit("spider_pet", pet=pet["id"])
        self._append_log(f"已选择宠物：{pet['name']}，正在应用到已打开的标签页…", "info")
        self._sync_pet_cards()

    def _on_pet_toggle(self) -> None:
        try:
            enabled = bool(int(self._pet_overlay_var.get() or 0))
        except Exception:
            enabled = self._spider_overlay_visible
        if enabled == self._spider_overlay_visible:
            return
        self._spider_overlay_visible = enabled
        self._sync_spider_button()
        self.worker.submit("spider_overlay", enabled=enabled)
        self._append_log(f"宠物特效（操作可视化）已{'开启' if enabled else '关闭'}。", "info")
        self._sync_pet_status()

    def _pet_play(self, action: str) -> None:
        self.worker.submit("pet_play", action=action)

    # ------------------------------------------------------------------ AI 管理

    def _build_agents_page(self) -> ctk.CTkFrame:
        """Agent 管理页：独立 Agent（模型接入 + 系统提示词 + 工具）的新增 / 编辑 / 删除 / 测试与默认选择。"""
        page = ctk.CTkFrame(self.content, fg_color="transparent", corner_radius=0)
        header = self._page_header(page, "Agent 管理", "独立 Agent：绑定模型接入、系统提示词与工具；插件统一按 Agent 调用（学习通「AI 批改作业」在此选择 Agent）")
        header_actions = ctk.CTkFrame(header, fg_color="transparent")
        header_actions.pack(side="right")
        add_agent_button = self._button(header_actions, "＋ 新增 Agent", lambda: self._open_agent_dialog(None), "primary", 118)
        add_agent_button.pack(side="left", padx=(0, 8))
        open_agents_button = self._icon_button(header_actions, ICON_FOLDER, self.open_agents_config_file, "打开 Agent 配置文件")
        open_agents_button.pack(side="left")

        # ---- AGENTS / 智能体（插件统一调用的入口）
        agents_header = ctk.CTkFrame(page, fg_color="transparent")
        agents_header.pack(fill="x", padx=24, pady=(0, 8))
        ctk.CTkLabel(
            agents_header, text="AGENTS / 智能体", text_color=COLORS["muted"], font=sans_font(12, "bold"), anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            agents_header,
            text="每个 Agent 绑定一个模型接入，拥有自己的系统提示词与工具；未指定时使用默认 Agent",
            text_color=COLORS["muted"], font=sans_font(10),
        ).pack(side="right")
        self._agents_list_frame = ctk.CTkFrame(page, fg_color="transparent", corner_radius=0)
        self._agents_list_frame.pack(fill="both", expand=True)
        self._rebuild_agents_list()

        # ---- MODELS / 模型接入（密钥与接口在此统一管理，供 Agent 引用）
        models_header = ctk.CTkFrame(page, fg_color="transparent")
        models_header.pack(fill="x", padx=24, pady=(16, 8))
        ctk.CTkLabel(
            models_header, text="MODELS / 模型接入", text_color=COLORS["muted"], font=sans_font(12, "bold"), anchor="w",
        ).pack(side="left")
        add_model_button = self._button(models_header, "＋ 新增接入", lambda: self._open_ai_dialog(None), "secondary", 104)
        add_model_button.pack(side="right")
        ctk.CTkLabel(
            models_header,
            text="OpenAI 兼容接口与密钥，Agent 通过「模型接入」引用",
            text_color=COLORS["muted"], font=sans_font(10),
        ).pack(side="right", padx=(0, 10))
        self._ai_list_frame = ctk.CTkFrame(page, fg_color="transparent", corner_radius=0)
        self._ai_list_frame.pack(fill="both", expand=True)
        self._rebuild_ai_list()

        # ---- TEMPLATES / 提示词模板（技能）
        skills_header = ctk.CTkFrame(page, fg_color="transparent")
        skills_header.pack(fill="x", padx=24, pady=(16, 8))
        ctk.CTkLabel(
            skills_header, text="TEMPLATES / 提示词模板", text_color=COLORS["muted"], font=sans_font(12, "bold"), anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(
            skills_header,
            text="创建 Agent 时可一键载入的提示词模板；内置随程序提供，自定义保存在本机",
            text_color=COLORS["muted"], font=sans_font(10),
        ).pack(side="right")
        self._skill_add_button = self._button(skills_header, "＋ 新增模板", lambda: self._open_ai_skill_dialog(None), "secondary", 104)
        self._skill_add_button.pack(side="right", padx=(0, 10))

        self._ai_skills_frame = ctk.CTkFrame(page, fg_color="transparent", corner_radius=0)
        self._ai_skills_frame.pack(fill="both", expand=True)
        self._rebuild_ai_skills()
        return page

    def open_agents_config_file(self) -> None:
        config_file = ai_manager.AI_AGENTS_FILE
        config_file.parent.mkdir(parents=True, exist_ok=True)
        if not config_file.exists():
            ai_manager.save_agents({"agents": [], "default_id": ""})
        os.startfile(config_file)

    def _rebuild_agents_list(self) -> None:
        frame = getattr(self, "_agents_list_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        payload = ai_manager.load_agents()
        agents = payload["agents"]
        default_id = str(payload.get("default_id") or "")
        if not agents:
            empty = self._card(frame, padded=True)
            ctk.CTkLabel(
                empty,
                text="还没有 Agent。点击右上角「＋ 新增 Agent」创建：绑定下方「模型接入」、写好系统提示词（可从提示词模板一键载入）并按需勾选工具；"
                     "创建后在学习通助手页选择该 Agent 即可批改作业。",
                text_color=COLORS["muted"], font=sans_font(11), justify="left", anchor="w", wraplength=780,
            ).pack(fill="x", padx=16, pady=14)
            return
        for agent in agents:
            self._build_agent_card(frame, agent, str(agent.get("id")) == default_id)

    def _agent_models_label(self, agent: dict[str, Any]) -> str:
        provider = ai_manager.get_provider(str(agent.get("provider_id") or ""))
        if provider is None:
            return "模型接入缺失"
        return f"{provider.get('name') or provider.get('id')} · {provider.get('model') or '--'}"

    def _build_agent_card(self, parent: Any, agent: dict[str, Any], is_default: bool) -> None:
        name = str(agent.get("name") or "未命名 Agent")
        card = self._card(parent, padded=True)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(12, 0))
        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True)
        title_row = ctk.CTkFrame(info, fg_color="transparent")
        title_row.pack(fill="x")
        ctk.CTkLabel(
            title_row, text=name, text_color=COLORS["text"], font=sans_font(13, "bold"), anchor="w",
        ).pack(side="left")
        if is_default:
            self._ai_tag(title_row, "默认", COLORS["accent"], "#ffffff")
        tools = [str(item) for item in (agent.get("tools") or []) if str(item) in ai_manager.BUILT_IN_TOOLS]
        tools_text = "、".join(tools) if tools else "无工具"
        meta_text = f"{self._agent_models_label(agent)} · 工具：{tools_text} · 温度 {agent.get('temperature', 0.2)}"
        ctk.CTkLabel(info, text=meta_text, text_color=COLORS["muted"], font=mono_font(10), anchor="w").pack(
            anchor="w", pady=(2, 0)
        )
        if agent.get("description"):
            ctk.CTkLabel(
                info, text=str(agent.get("description")), text_color=COLORS["muted"], font=sans_font(10),
                anchor="w", wraplength=660, justify="left",
            ).pack(anchor="w", pady=(1, 0))
        prompt_text = str(agent.get("system_prompt") or "")
        preview = prompt_text if len(prompt_text) <= 140 else prompt_text[:140] + "…"
        ctk.CTkLabel(
            info, text=preview, text_color=COLORS["muted"], font=mono_font(9),
            anchor="w", wraplength=680, justify="left",
        ).pack(anchor="w", pady=(2, 12))

        actions = ctk.CTkFrame(row, fg_color="transparent")
        actions.pack(side="right", padx=(14, 0), pady=(2, 0))
        default_button = self._button(
            actions, "使用中" if is_default else "设为默认",
            lambda pid=str(agent.get("id")): self._agent_set_default(pid),
            "secondary", 86,
        )
        if is_default:
            default_button.configure(state="disabled")
        default_button.pack(side="left")
        test_button = self._button(actions, "测试", lambda: None, "secondary", 64)
        test_button.configure(command=lambda: self._agent_test(agent, test_button))
        test_button.pack(side="left", padx=(8, 0))
        edit_button = self._button(actions, "编辑", lambda item=agent: self._open_agent_dialog(item), "secondary", 64)
        edit_button.pack(side="left", padx=(8, 0))
        delete_button = self._button(actions, "删除", lambda item=agent: self._agent_delete(item), "danger", 64)
        delete_button.pack(side="left", padx=(8, 0))

    def _agent_set_default(self, agent_id: str) -> None:
        try:
            ai_manager.set_default_agent(agent_id)
        except AIError as exc:
            messagebox.showerror("Agent 管理", str(exc))
            return
        self._append_log("已更新默认 Agent。", "success")
        self._rebuild_agents_list()

    def _agent_delete(self, agent: dict[str, Any]) -> None:
        name = str(agent.get("name") or agent.get("id") or "未命名 Agent")
        if not messagebox.askyesno("Agent 管理", f"确定删除 Agent「{name}」？删除后无法恢复。"):
            return
        ai_manager.remove_agent(str(agent.get("id") or ""))
        self._append_log(f"已删除 Agent「{name}」。", "info")
        self._rebuild_agents_list()

    def _agent_test(self, agent: dict[str, Any], button: ctk.CTkButton) -> None:
        """后台线程测试 Agent（走统一调用口 run_agent），结果经队列回主线程。"""
        try:
            button.configure(state="disabled", text="测试中…")
        except tk.TclError:
            return
        results: queue.Queue = queue.Queue()

        def run() -> None:
            try:
                results.put(("ok", ai_manager.run_agent(agent, "连接测试，请只回复四个字：连接成功", timeout=30.0, max_tokens=16)))
            except Exception as exc:
                results.put(("error", str(exc)))

        threading.Thread(target=run, name="agent-test", daemon=True).start()

        def poll() -> None:
            try:
                state, text = results.get_nowait()
            except queue.Empty:
                try:
                    button.after(120, poll)
                except tk.TclError:
                    pass
                return
            try:
                if not button.winfo_exists():
                    return
                button.configure(state="normal", text="测试")
            except tk.TclError:
                return
            if state == "ok":
                self._append_log(f"Agent 测试成功：{text}", "success")
                messagebox.showinfo("Agent 管理", f"连接成功，Agent 回复：{text}")
            else:
                self._append_log(f"Agent 测试失败：{text}", "error")
                messagebox.showerror("Agent 管理", f"测试失败：{text}")

        button.after(120, poll)

    def _open_agent_dialog(self, agent: dict[str, Any] | None) -> None:
        """新增 / 编辑 Agent 的模态对话框；模型接入缺失时引导先建接入。"""
        providers = ai_manager.list_providers()
        if not providers:
            messagebox.showinfo("Agent 管理", "请先在页面下方「模型接入」新增 AI 配置，再创建 Agent。")
            return
        editing = agent is not None
        provider_by_label = {
            f"{item.get('name') or item.get('id')}（{item.get('model') or '--'}）": str(item.get("id"))
            for item in providers
        }
        current_provider_id = str((agent or {}).get("provider_id") or "")
        if current_provider_id not in set(provider_by_label.values()):
            current_provider_id = str(providers[0].get("id") or "")
        provider_var = tk.StringVar(
            value=next((label for label, pid in provider_by_label.items() if pid == current_provider_id), "")
        )
        skills = ai_manager.list_skills()
        skill_by_label = {"（不载入模板）": ""}
        for skill in skills:
            prefix = "内置" if str(skill.get("id") or "").startswith("builtin_") else "自定义"
            skill_by_label[f"{prefix} · {skill.get('name') or skill.get('id')}"] = str(skill.get("id") or "")
        style_values = {"保守（0.1）": 0.1, "均衡（0.4）": 0.4, "发散（0.8）": 0.8}
        try:
            current_temperature = float((agent or {}).get("temperature", 0.1))
        except (TypeError, ValueError):
            current_temperature = 0.1
        style_var = tk.StringVar(value=min(style_values, key=lambda label: abs(style_values[label] - current_temperature)))

        dialog = ctk.CTkToplevel(self)
        dialog.title("编辑 Agent" if editing else "新增 Agent")
        dialog.geometry("660x780")
        dialog.configure(fg_color=COLORS["panel"])
        dialog.transient(self)
        dialog.grab_set()
        dialog.after(150, dialog.lift)

        body = ctk.CTkFrame(dialog, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=18)
        ctk.CTkLabel(
            body, text="编辑 Agent" if editing else "新增 Agent",
            text_color=COLORS["text"], font=sans_font(15, "bold"), anchor="w",
        ).pack(fill="x")

        name_var = tk.StringVar(value=str((agent or {}).get("name") or ""))
        desc_var = tk.StringVar(value=str((agent or {}).get("description") or ""))
        template_var = tk.StringVar(value="（不载入模板）")

        def field_row(label: str) -> ctk.CTkFrame:
            row = ctk.CTkFrame(body, fg_color="transparent")
            row.pack(fill="x", pady=(10, 0))
            ctk.CTkLabel(
                row, text=label, text_color=COLORS["muted"], font=sans_font(11), width=92, anchor="w",
            ).pack(side="left")
            return row

        entry_style = {
            "height": 34,
            "corner_radius": 8,
            "fg_color": "#fafaf8",
            "border_color": COLORS["border"],
            "text_color": COLORS["text"],
            "font": sans_font(12),
        }
        menu_style = {
            "height": 34,
            "fg_color": "#f2f2f0",
            "button_color": "#e7e7e4",
            "button_hover_color": "#d6d6d2",
            "text_color": COLORS["text"],
            "font": sans_font(12),
            "anchor": "w",
        }

        name_row = field_row("名称")
        ctk.CTkEntry(name_row, textvariable=name_var, placeholder_text="例如：作业批改助手", **entry_style).pack(
            side="left", fill="x", expand=True
        )
        desc_row = field_row("描述")
        ctk.CTkEntry(desc_row, textvariable=desc_var, placeholder_text="一句话说明用途（可选）", **entry_style).pack(
            side="left", fill="x", expand=True
        )
        provider_row = field_row("模型接入")
        ctk.CTkOptionMenu(provider_row, values=list(provider_by_label), variable=provider_var, **menu_style).pack(
            side="left", fill="x", expand=True
        )

        prompt_label_row = ctk.CTkFrame(body, fg_color="transparent")
        prompt_label_row.pack(fill="x", pady=(12, 0))
        ctk.CTkLabel(
            prompt_label_row, text="系统提示词", text_color=COLORS["muted"], font=sans_font(11), width=92, anchor="w",
        ).pack(side="left")
        template_menu = ctk.CTkOptionMenu(
            prompt_label_row, values=list(skill_by_label), variable=template_var, height=30, width=230,
            fg_color="#f2f2f0", button_color="#e7e7e4", button_hover_color="#d6d6d2",
            text_color=COLORS["text"], font=sans_font(11), anchor="w",
        )
        template_menu.pack(side="left")
        prompt_box = ctk.CTkTextbox(
            body, height=170, fg_color="#fafaf8", border_color=COLORS["border"], border_width=1,
            corner_radius=8, font=mono_font(11), text_color=COLORS["text"], wrap="word",
        )
        prompt_box.pack(fill="x", padx=(92, 0), pady=(4, 0))
        default_prompt = str((agent or {}).get("system_prompt") or "").strip() or str(
            (ai_manager.get_skill(ai_manager.DEFAULT_SKILL_ID) or {}).get("content") or ""
        )
        prompt_box.insert("1.0", default_prompt)

        def load_template() -> None:
            skill = ai_manager.get_skill(skill_by_label.get(template_var.get(), ""))
            if skill is None:
                return
            prompt_box.delete("1.0", "end")
            prompt_box.insert("1.0", str(skill.get("content") or ""))

        load_template_button = self._button(prompt_label_row, "载入模板", load_template, "secondary", 86)
        load_template_button.pack(side="left", padx=(8, 0))

        tools_label = ctk.CTkLabel(
            body, text="工具", text_color=COLORS["muted"], font=sans_font(11), width=92, anchor="nw",
        )
        tools_label.pack(anchor="w", pady=(12, 0))
        tool_vars: dict[str, tk.BooleanVar] = {}
        for tool_name, tool in ai_manager.BUILT_IN_TOOLS.items():
            tool_row = ctk.CTkFrame(body, fg_color="transparent")
            tool_row.pack(fill="x", padx=(92, 0), pady=(3, 0))
            tool_vars[tool_name] = tk.BooleanVar(value=tool_name in ((agent or {}).get("tools") or []))
            ctk.CTkCheckBox(tool_row, text=tool_name, variable=tool_vars[tool_name], font=sans_font(11)).pack(side="left")
            ctk.CTkLabel(tool_row, text=tool["description"], text_color=COLORS["muted"], font=sans_font(9)).pack(
                side="left", padx=(8, 0)
            )

        style_row = field_row("回答风格")
        ctk.CTkOptionMenu(style_row, values=list(style_values), variable=style_var, **menu_style).pack(
            side="left", fill="x", expand=True
        )

        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(side="bottom", fill="x", pady=(12, 0))
        cancel_button = self._button(buttons, "取消", dialog.destroy, "secondary", 84)
        cancel_button.pack(side="right")

        def save() -> None:
            provider_id = provider_by_label.get(provider_var.get(), "")
            tools = [name for name, var in tool_vars.items() if var.get()]
            temperature = style_values.get(style_var.get(), 0.1)
            prompt = prompt_box.get("1.0", "end").strip()
            try:
                if editing:
                    ai_manager.update_agent(
                        str(agent.get("id") or ""), name=name_var.get(), description=desc_var.get(),
                        provider_id=provider_id, system_prompt=prompt, tools=tools, temperature=temperature,
                    )
                else:
                    ai_manager.add_agent(
                        name_var.get(), provider_id, prompt, desc_var.get(), tools, temperature
                    )
            except AIError as exc:
                messagebox.showerror("Agent 管理", str(exc), parent=dialog)
                return
            self._append_log(f"已{'更新' if editing else '新增'} Agent「{name_var.get().strip()}」。", "success")
            self._rebuild_agents_list()
            dialog.destroy()

        save_button = self._button(buttons, "保存", save, "primary", 84)
        save_button.pack(side="right", padx=(0, 8))

    def open_ai_config_file(self) -> None:
        config_file = ai_manager.AI_PROVIDERS_FILE
        config_file.parent.mkdir(parents=True, exist_ok=True)
        if not config_file.exists():
            ai_manager.save_ai_providers({"providers": [], "default_id": ""})
        os.startfile(config_file)

    def _platform_label(self, platform: str) -> str:
        preset = ai_manager.AI_PLATFORM_PRESETS.get(str(platform or "custom"))
        if preset is not None:
            return str(preset.get("name") or platform)
        return str(platform or "自定义")

    def _rebuild_ai_list(self) -> None:
        frame = getattr(self, "_ai_list_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        payload = ai_manager.load_ai_providers()
        providers = payload["providers"]
        default_id = str(payload.get("default_id") or "")
        if not providers:
            empty = self._card(frame, padded=True)
            ctk.CTkLabel(
                empty,
                text="还没有 AI 配置。点击右上角「＋ 新增 AI」接入一个平台，推荐先使用 DeepSeek（deepseek-chat）；"
                     "配置成功后，学习通助手页的「AI 批改作业」即可使用。所有配置只保存在本机 runtime/ai_providers.json。",
                text_color=COLORS["muted"], font=sans_font(11), justify="left", anchor="w", wraplength=780,
            ).pack(fill="x", padx=16, pady=14)
            return
        for provider in providers:
            self._build_ai_card(frame, provider, str(provider.get("id")) == default_id)

    @staticmethod
    def _ai_tag(parent: Any, text: str, background: str, foreground: str) -> None:
        ctk.CTkLabel(
            parent, text=text, fg_color=background, corner_radius=5, text_color=foreground,
            font=sans_font(9, "bold"), height=20,
        ).pack(side="left", padx=(8, 0))

    def _build_ai_card(self, parent: Any, provider: dict[str, Any], is_default: bool) -> None:
        name = str(provider.get("name") or "未命名 AI")
        card = self._card(parent, padded=True)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(12, 0))
        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True)
        title_row = ctk.CTkFrame(info, fg_color="transparent")
        title_row.pack(fill="x")
        ctk.CTkLabel(title_row, text=name, text_color=COLORS["text"], font=sans_font(13, "bold"), anchor="w").pack(side="left")
        if is_default:
            self._ai_tag(title_row, "默认", COLORS["accent"], "#ffffff")
        meta_text = (
            f"{self._platform_label(str(provider.get('platform')))} · {provider.get('model') or '--'}"
            f" · {provider.get('base_url') or '--'}"
        )
        ctk.CTkLabel(info, text=meta_text, text_color=COLORS["muted"], font=mono_font(10), anchor="w").pack(
            anchor="w", pady=(2, 0)
        )
        ctk.CTkLabel(
            info,
            text=f"API Key：{ai_manager.masked_key(str(provider.get('api_key') or ''))}",
            text_color=COLORS["muted"], font=mono_font(10), anchor="w",
        ).pack(anchor="w", pady=(1, 12))

        actions = ctk.CTkFrame(row, fg_color="transparent")
        actions.pack(side="right", padx=(14, 0), pady=(2, 0))
        default_button = self._button(
            actions, "使用中" if is_default else "设为默认",
            lambda pid=str(provider.get("id")): self._ai_set_default(pid),
            "secondary", 86,
        )
        if is_default:
            default_button.configure(state="disabled")
        default_button.pack(side="left")
        test_button = self._button(actions, "测试", lambda: None, "secondary", 64)
        test_button.configure(command=lambda: self._ai_test(provider, test_button))
        test_button.pack(side="left", padx=(8, 0))
        edit_button = self._button(actions, "编辑", lambda item=provider: self._open_ai_dialog(item), "secondary", 64)
        edit_button.pack(side="left", padx=(8, 0))
        delete_button = self._button(actions, "删除", lambda item=provider: self._ai_delete(item), "danger", 64)
        delete_button.pack(side="left", padx=(8, 0))

    def _rebuild_ai_skills(self) -> None:
        """技能列表：内置技能（只读）在前，自定义技能（可编辑/删除）在后。"""
        frame = getattr(self, "_ai_skills_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        for skill in ai_manager.list_skills():
            self._build_ai_skill_card(frame, skill, str(skill.get("id") or "").startswith("builtin_"))

    def _build_ai_skill_card(self, parent: Any, skill: dict[str, Any], built_in: bool) -> None:
        card = self._card(parent, padded=True)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(12, 0))
        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True)
        title_row = ctk.CTkFrame(info, fg_color="transparent")
        title_row.pack(fill="x")
        ctk.CTkLabel(
            title_row, text=str(skill.get("name") or "未命名技能"),
            text_color=COLORS["text"], font=sans_font(12, "bold"), anchor="w",
        ).pack(side="left")
        self._ai_tag(title_row, "内置" if built_in else "自定义", "#f2f2f0" if built_in else COLORS["accent"], COLORS["text"] if built_in else "#ffffff")
        if skill.get("description"):
            ctk.CTkLabel(
                info, text=str(skill.get("description")), text_color=COLORS["muted"], font=sans_font(10),
                anchor="w", wraplength=620, justify="left",
            ).pack(anchor="w", pady=(1, 0))
        content = str(skill.get("content") or "")
        preview = content if len(content) <= 160 else content[:160] + "…"
        ctk.CTkLabel(
            info, text=preview, text_color=COLORS["muted"], font=mono_font(9),
            anchor="w", wraplength=640, justify="left",
        ).pack(anchor="w", pady=(2, 12))

        actions = ctk.CTkFrame(row, fg_color="transparent")
        actions.pack(side="right", padx=(14, 0), pady=(2, 0))
        copy_button = self._button(actions, "复制提示词", lambda text=content: self._copy_skill_content(text), "secondary", 92)
        copy_button.pack(side="left")
        if not built_in:
            edit_button = self._button(actions, "编辑", lambda item=skill: self._open_ai_skill_dialog(item), "secondary", 60)
            edit_button.pack(side="left", padx=(8, 0))
            delete_button = self._button(actions, "删除", lambda item=skill: self._ai_skill_delete(item), "danger", 60)
            delete_button.pack(side="left", padx=(8, 0))

    def _copy_skill_content(self, content: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(content)
            self._append_log("技能提示词已复制到剪贴板。", "info")
        except tk.TclError:
            pass

    def _ai_skill_delete(self, skill: dict[str, Any]) -> None:
        name = str(skill.get("name") or skill.get("id") or "未命名技能")
        if not messagebox.askyesno("AI 管理", f"确定删除自定义技能「{name}」？删除后无法恢复。"):
            return
        try:
            ai_manager.remove_skill(str(skill.get("id") or ""))
        except AIError as exc:
            messagebox.showerror("AI 管理", str(exc))
            return
        self._append_log(f"已删除自定义技能「{name}」。", "info")
        self._rebuild_ai_skills()

    def _open_ai_skill_dialog(self, skill: dict[str, Any] | None) -> None:
        """新增 / 编辑自定义提示词技能；内置技能只读，不进入此对话框。"""
        editing = skill is not None
        dialog = ctk.CTkToplevel(self)
        dialog.title("编辑技能" if editing else "新增技能")
        dialog.geometry("600x640")
        dialog.configure(fg_color=COLORS["panel"])
        dialog.transient(self)
        dialog.grab_set()
        dialog.after(150, dialog.lift)

        body = ctk.CTkFrame(dialog, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=18)
        ctk.CTkLabel(
            body, text="编辑技能" if editing else "新增技能",
            text_color=COLORS["text"], font=sans_font(15, "bold"), anchor="w",
        ).pack(fill="x")

        name_var = tk.StringVar(value=str((skill or {}).get("name") or ""))
        desc_var = tk.StringVar(value=str((skill or {}).get("description") or ""))
        name_row = ctk.CTkFrame(body, fg_color="transparent")
        name_row.pack(fill="x", pady=(12, 0))
        ctk.CTkLabel(name_row, text="名称", text_color=COLORS["muted"], font=sans_font(11), width=64, anchor="w").pack(side="left")
        ctk.CTkEntry(
            name_row, textvariable=name_var, height=34, corner_radius=8,
            fg_color="#fafaf8", border_color=COLORS["border"], text_color=COLORS["text"], font=sans_font(12),
        ).pack(side="left", fill="x", expand=True)
        desc_row = ctk.CTkFrame(body, fg_color="transparent")
        desc_row.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(desc_row, text="描述", text_color=COLORS["muted"], font=sans_font(11), width=64, anchor="w").pack(side="left")
        ctk.CTkEntry(
            desc_row, textvariable=desc_var, height=34, corner_radius=8,
            fg_color="#fafaf8", border_color=COLORS["border"], text_color=COLORS["text"], font=sans_font(12),
            placeholder_text="一句话说明适用场景（可选）",
        ).pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            body, text="提示词内容（必须保留 JSON 输出契约，批改结果靠它解析）",
            text_color=COLORS["muted"], font=sans_font(11), anchor="w",
        ).pack(fill="x", pady=(12, 4))
        content_box = ctk.CTkTextbox(
            body, height=260, fg_color="#fafaf8", border_color=COLORS["border"], border_width=1,
            corner_radius=8, font=mono_font(11), text_color=COLORS["text"], wrap="word",
        )
        content_box.pack(fill="both", expand=True)
        if editing:
            content_box.insert("1.0", str((skill or {}).get("content") or ""))

        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", pady=(12, 0))
        cancel_button = self._button(buttons, "取消", dialog.destroy, "secondary", 84)
        cancel_button.pack(side="right")

        def save() -> None:
            content = content_box.get("1.0", "end").strip()
            try:
                if editing:
                    ai_manager.update_skill(str(skill.get("id") or ""), name=name_var.get(), content=content, description=desc_var.get())
                else:
                    ai_manager.add_skill(name=name_var.get(), content=content, description=desc_var.get())
            except AIError as exc:
                messagebox.showerror("AI 管理", str(exc), parent=dialog)
                return
            self._append_log(f"已{'更新' if editing else '新增'}提示词技能「{name_var.get().strip()}」。", "success")
            self._rebuild_ai_skills()
            dialog.destroy()

        save_button = self._button(buttons, "保存", save, "primary", 84)
        save_button.pack(side="right", padx=(0, 8))

    def _ai_set_default(self, provider_id: str) -> None:
        try:
            ai_manager.set_default_provider(provider_id)
        except AIError as exc:
            messagebox.showerror("AI 管理", str(exc))
            return
        self._append_log("已更新默认 AI。", "success")
        self._rebuild_ai_list()

    def _ai_delete(self, provider: dict[str, Any]) -> None:
        name = str(provider.get("name") or provider.get("id") or "未命名 AI")
        if not messagebox.askyesno("AI 管理", f"确定删除 AI 配置「{name}」？删除后无法恢复。"):
            return
        ai_manager.remove_provider(str(provider.get("id") or ""))
        self._append_log(f"已删除 AI 配置「{name}」。", "info")
        self._rebuild_ai_list()

    def _ai_test(self, provider: dict[str, Any], button: ctk.CTkButton) -> None:
        """后台线程做连通性测试，避免阻塞 UI；结果经队列回到主线程。"""
        try:
            button.configure(state="disabled", text="测试中…")
        except tk.TclError:
            return
        results: queue.Queue = queue.Queue()

        def run() -> None:
            try:
                results.put(("ok", ai_manager.test_provider(provider)))
            except Exception as exc:
                results.put(("error", str(exc)))

        threading.Thread(target=run, name="ai-test", daemon=True).start()

        def poll() -> None:
            try:
                state, text = results.get_nowait()
            except queue.Empty:
                try:
                    button.after(120, poll)
                except tk.TclError:
                    pass
                return
            try:
                if not button.winfo_exists():
                    return
                button.configure(state="normal", text="测试")
            except tk.TclError:
                return
            if state == "ok":
                self._append_log(f"AI 连接测试成功：{text}", "success")
                messagebox.showinfo("AI 管理", f"连接成功，AI 回复：{text}")
            else:
                self._append_log(f"AI 连接测试失败：{text}", "error")
                messagebox.showerror("AI 管理", f"连接失败：{text}")

        button.after(120, poll)

    def _open_ai_dialog(self, provider: dict[str, Any] | None) -> None:
        """新增 / 编辑 AI 配置的模态对话框；平台切换自动带出接口地址与候选模型。"""
        presets = ai_manager.AI_PLATFORM_PRESETS
        label_by_key = {key: str(preset.get("name") or key) for key, preset in presets.items()}
        key_by_label = {label: key for key, label in label_by_key.items()}
        editing = provider is not None
        current_platform = str((provider or {}).get("platform") or "deepseek")
        if current_platform not in presets:
            current_platform = "custom"

        dialog = ctk.CTkToplevel(self)
        dialog.title("编辑 AI 配置" if editing else "新增 AI 配置")
        dialog.geometry("560x640")
        dialog.configure(fg_color=COLORS["panel"])
        dialog.transient(self)
        dialog.grab_set()
        dialog.after(150, dialog.lift)

        body = ctk.CTkFrame(dialog, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=18)
        ctk.CTkLabel(
            body, text="编辑 AI 配置" if editing else "新增 AI 配置",
            text_color=COLORS["text"], font=sans_font(15, "bold"), anchor="w",
        ).pack(fill="x")

        name_var = tk.StringVar(value=str((provider or {}).get("name") or ""))
        platform_var = tk.StringVar(value=label_by_key[current_platform])
        preset = presets[current_platform]
        base_url_var = tk.StringVar(
            value=str((provider or {}).get("base_url") or preset.get("base_url") or "")
        )
        preset_models = [str(item) for item in preset.get("models") or []]
        model_var = tk.StringVar(
            value=str((provider or {}).get("model") or (preset_models[0] if preset_models else ""))
        )
        key_var = tk.StringVar(value=str((provider or {}).get("api_key") or ""))
        apply_var = tk.StringVar(value=str(preset.get("apply_url") or ""))
        status_var = tk.StringVar(value="")

        def field_row(label: str) -> ctk.CTkFrame:
            row = ctk.CTkFrame(body, fg_color="transparent")
            row.pack(fill="x", pady=(12, 0))
            ctk.CTkLabel(
                row, text=label, text_color=COLORS["muted"], font=sans_font(11), width=92, anchor="w",
            ).pack(side="left")
            return row

        entry_style = {
            "height": 34,
            "corner_radius": 8,
            "fg_color": "#fafaf8",
            "border_color": COLORS["border"],
            "text_color": COLORS["text"],
            "font": sans_font(12),
        }

        name_row = field_row("名称")
        ctk.CTkEntry(name_row, textvariable=name_var, placeholder_text="例如：DeepSeek 官方", **entry_style).pack(
            side="left", fill="x", expand=True
        )

        def on_platform(label: str) -> None:
            key = key_by_label.get(label, "custom")
            chosen = presets[key]
            base_url_var.set(str(chosen.get("base_url") or ""))
            apply_var.set(str(chosen.get("apply_url") or ""))
            models = [str(item) for item in chosen.get("models") or []]
            model_box.configure(values=models)
            if models:
                model_var.set(models[0])

        platform_row = field_row("平台")
        ctk.CTkOptionMenu(
            platform_row, values=list(label_by_key.values()), variable=platform_var, height=34,
            fg_color="#f2f2f0", button_color="#e7e7e4", button_hover_color="#d6d6d2",
            text_color=COLORS["text"], font=sans_font(12), anchor="w", command=on_platform,
        ).pack(side="left", fill="x", expand=True)
        apply_label = ctk.CTkLabel(
            body, textvariable=apply_var, text_color=COLORS["muted"], font=mono_font(10), anchor="w",
        )
        apply_label.pack(fill="x", padx=(92, 0), pady=(4, 0))

        base_url_row = field_row("接口地址")
        ctk.CTkEntry(base_url_row, textvariable=base_url_var, placeholder_text="https://api.example.com/v1", **entry_style).pack(
            side="left", fill="x", expand=True
        )
        model_row = field_row("模型")
        model_box = ctk.CTkComboBox(model_row, values=preset_models, variable=model_var, height=34, corner_radius=8, fg_color="#fafaf8", border_color=COLORS["border"], button_color="#f2f2f0", button_hover_color="#e7e7e4", text_color=COLORS["text"], font=sans_font(12), dropdown_font=sans_font(12))
        model_box.pack(side="left", fill="x", expand=True)
        key_row = field_row("API Key")
        ctk.CTkEntry(key_row, textvariable=key_var, show="*", placeholder_text="在平台控制台申请，仅保存在本机", **entry_style).pack(
            side="left", fill="x", expand=True
        )

        status_label = ctk.CTkLabel(
            body, textvariable=status_var, text_color=COLORS["muted"], font=sans_font(10),
            wraplength=460, justify="left", anchor="w",
        )
        status_label.pack(fill="x", pady=(14, 0))

        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(side="bottom", fill="x", pady=(12, 0))
        cancel_button = self._button(buttons, "取消", dialog.destroy, "secondary", 84)
        cancel_button.pack(side="right")
        save_button = self._button(buttons, "保存", lambda: None, "primary", 90)
        save_button.pack(side="right", padx=(0, 8))
        test_button = self._button(buttons, "测试连接", lambda: None, "secondary", 96)
        test_button.pack(side="right", padx=(0, 8))

        def save() -> None:
            platform_key = key_by_label.get(platform_var.get(), "custom")
            try:
                if editing:
                    ai_manager.update_provider(
                        str(provider.get("id") or ""),
                        name=name_var.get(), platform=platform_key,
                        base_url=base_url_var.get(), api_key=key_var.get(), model=model_var.get(),
                    )
                else:
                    ai_manager.add_provider(
                        name_var.get(), platform_key, base_url_var.get(), key_var.get(), model_var.get()
                    )
            except AIError as exc:
                messagebox.showerror("AI 管理", str(exc), parent=dialog)
                return
            self._append_log(f"已{'更新' if editing else '新增'} AI 配置「{name_var.get().strip()}」。", "success")
            self._rebuild_ai_list()
            dialog.destroy()

        def test_connection() -> None:
            temp = {"base_url": base_url_var.get(), "api_key": key_var.get(), "model": model_var.get()}
            try:
                status_var.set("正在测试连接…")
                test_button.configure(state="disabled")
            except tk.TclError:
                return
            results: queue.Queue = queue.Queue()

            def run() -> None:
                try:
                    results.put(("ok", ai_manager.test_provider(temp)))
                except Exception as exc:
                    results.put(("error", str(exc)))

            threading.Thread(target=run, name="ai-dialog-test", daemon=True).start()

            def poll() -> None:
                try:
                    if not dialog.winfo_exists():
                        return
                except tk.TclError:
                    return
                try:
                    state, text = results.get_nowait()
                except queue.Empty:
                    dialog.after(120, poll)
                    return
                try:
                    test_button.configure(state="normal")
                except tk.TclError:
                    return
                if state == "ok":
                    status_var.set(f"连接成功，AI 回复：{text}")
                    self._append_log(f"AI 连接测试成功：{text}", "success")
                else:
                    status_var.set(f"连接失败：{text}")
                    self._append_log(f"AI 连接测试失败：{text}", "error")

            dialog.after(120, poll)

        save_button.configure(command=save)
        test_button.configure(command=test_connection)

    def _build_logs_page(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self.content, fg_color="transparent", corner_radius=0)
        self._page_header(page, "运行日志", "浏览器启动、缓存、Rewards 与搜索任务记录")
        tools = ctk.CTkFrame(page, fg_color="transparent")
        tools.pack(fill="x", padx=24, pady=(0, 8))
        self._button(tools, "清空显示", self.clear_log, "secondary", 100).pack(side="right")
        self._button(tools, "打开日志文件", self.open_log_file, "secondary", 120).pack(side="right", padx=(0, 8))
        container = tk.Frame(page, bg=COLORS["term_bg"], highlightthickness=1, highlightbackground=COLORS["term_border"])
        container.pack(fill="both", expand=True, padx=24, pady=(0, 20))
        container.columnconfigure(0, weight=1)
        container.rowconfigure(1, weight=1)
        title_row = tk.Frame(container, bg=COLORS["term_bg"])
        title_row.grid(row=0, column=0, columnspan=2, sticky="ew")
        tk.Label(title_row, text="▌ operations — 实时事件流", bg=COLORS["term_bg"], fg=COLORS["term_muted"], font=(FONT_SANS, 11, "bold"), anchor="w", padx=12, pady=8).pack(fill="x")
        self.log_text = tk.Text(
            container,
            bg=COLORS["term_bg"],
            fg=COLORS["term_fg"],
            insertbackground=COLORS["term_fg"],
            selectbackground=COLORS["term_select"],
            relief="flat",
            borderwidth=0,
            wrap="word",
            padx=12,
            pady=4,
            font=(FONT_MONO, 11),
            state="disabled",
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")
        scrollbar = ctk.CTkScrollbar(container, command=self.log_text.yview, button_color="#3f3f3c", button_hover_color="#565652", fg_color=COLORS["term_bg"])
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.tag_configure("timestamp", foreground=COLORS["term_muted"])
        self.log_text.tag_configure("info", foreground="#b8b8b3")
        self.log_text.tag_configure("success", foreground="#4ade80")
        self.log_text.tag_configure("warning", foreground="#fbbf24")
        self.log_text.tag_configure("error", foreground="#f87171")
        return page

    # 核心页面键（热安装的插件页插到它们前面）
    _CORE_PAGE_ORDER = ("overview", "pet", "agents", "logs", "plugins")

    def _build_plugins_page(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self.content, fg_color="transparent", corner_radius=0)
        header = self._page_header(page, "插件管理", "已安装插件、启用状态与热更新")
        header_actions = ctk.CTkFrame(header, fg_color="transparent")
        header_actions.pack(side="right")
        self.rescan_button = self._icon_button(header_actions, ICON_REFRESH, self._rescan_plugins, "扫描新插件（新放入 plugins/ 的立即生效）")
        self.rescan_button.pack(side="left", padx=(0, 6))
        restart_button = self._icon_button(header_actions, ICON_RESTART, self._restart_application, "重启程序")
        restart_button.pack(side="left", padx=(0, 6))
        open_dir_button = self._icon_button(header_actions, ICON_FOLDER, self.open_plugins_directory, "打开插件目录")
        open_dir_button.pack(side="left")

        list_frame = ctk.CTkFrame(page, fg_color="transparent", corner_radius=0)
        list_frame.pack(fill="both", expand=True)
        self._plugin_list_frame = list_frame
        self._rebuild_plugin_list()
        return page

    def _rebuild_plugin_list(self) -> None:
        """按当前发现/加载状态重建插件列表（开关、热安装、扫描后都会调用）。"""
        list_frame = getattr(self, "_plugin_list_frame", None)
        if list_frame is None:
            return
        for child in list_frame.winfo_children():
            child.destroy()
        manager = self.plugin_manager
        if not manager.manifests and not manager.errors:
            empty_card = self._card(list_frame, padded=True)
            ctk.CTkLabel(
                empty_card,
                text="还没有安装插件。把插件文件夹（含 manifest.json 与入口模块）放入 plugins/ 目录，点击页头「扫描新插件」即会出现在左侧菜单。",
                text_color=COLORS["muted"], font=sans_font(11), justify="left", anchor="w", wraplength=760,
            ).pack(fill="x", padx=16, pady=14)
        for manifest in manager.manifests:
            self._build_plugin_card(list_frame, manifest)
        manifest_names = {str(m.get("_folder")) for m in manager.manifests} | {str(m.get("id")) for m in manager.manifests}
        for folder, error in manager.errors.items():
            if folder not in manifest_names:
                self._build_plugin_error_card(list_frame, folder, error)

        hint_card = self._card(list_frame, padded=True)
        ctk.CTkLabel(
            hint_card,
            text="安装：插件文件夹放入 plugins/ 后点「扫描新插件」，立即上栏；卸载：移除文件夹再扫描，或直接关闭开关。停用/重新启用即时生效，关闭再打开开关还会重新读取插件代码；任务运行中的插件暂不能停用。",
            text_color=COLORS["muted"], font=sans_font(10), justify="left", anchor="w", wraplength=760,
        ).pack(fill="x", padx=16, pady=(2, 12))

    def _build_plugin_card(self, parent: Any, manifest: dict[str, Any]) -> None:
        manager = self.plugin_manager
        plugin_id = str(manifest.get("id"))
        name = str(manifest.get("name") or plugin_id)
        version = str(manifest.get("version") or "?")
        description = str(manifest.get("description") or "")
        error_text = manager.errors.get(plugin_id) or manager.errors.get(str(manifest.get("_folder")))

        card = self._card(parent, padded=True)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(12, 4 if (description or error_text) else 12))
        controls = ctk.CTkFrame(row, fg_color="transparent")
        controls.pack(side="right")
        ctk.CTkLabel(controls, text=self._plugin_status_text(manifest), text_color=COLORS["muted"], font=sans_font(11)).pack(side="left", padx=(0, 12))
        switch_var = tk.BooleanVar(value=manager.is_enabled(plugin_id))
        ctk.CTkSwitch(
            controls,
            text="",
            width=54,
            progress_color=COLORS["accent"],
            variable=switch_var,
            command=lambda pid=plugin_id, var=switch_var: self._toggle_plugin_setting(pid, var),
        ).pack(side="left")

        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(info, text=name, text_color=COLORS["text"], font=sans_font(13, "bold"), anchor="w").pack(anchor="w")
        ctk.CTkLabel(info, text=f"{plugin_id}  ·  v{version}", text_color=COLORS["muted"], font=mono_font(10), anchor="w").pack(anchor="w", pady=(2, 0))
        if description:
            ctk.CTkLabel(card, text=description, text_color=COLORS["muted"], font=sans_font(11), anchor="w", justify="left", wraplength=760).pack(fill="x", padx=16, pady=(0, 4 if error_text else 12))
        if error_text:
            ctk.CTkLabel(card, text=f"加载失败：{error_text}", text_color=COLORS["danger"], font=sans_font(10), anchor="w", justify="left", wraplength=760).pack(fill="x", padx=16, pady=(0, 12))

    def _build_plugin_error_card(self, parent: Any, folder: str, error: str) -> None:
        """manifest 解析失败等没有清单可展示的插件目录，用单独的错误卡片呈现。"""
        card = self._card(parent, padded=True)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(12, 12))
        ctk.CTkLabel(row, text=folder, text_color=COLORS["text"], font=sans_font(13, "bold"), anchor="w").pack(side="left")
        ctk.CTkLabel(row, text="已禁用", text_color=COLORS["muted"], font=sans_font(11)).pack(side="right")
        ctk.CTkLabel(card, text=f"加载失败：{error}", text_color=COLORS["danger"], font=sans_font(10), anchor="w", justify="left", wraplength=760).pack(fill="x", padx=16, pady=(0, 12))

    def _plugin_status_text(self, manifest: dict[str, Any]) -> str:
        manager = self.plugin_manager
        plugin_id = str(manifest.get("id"))
        if not manager.is_enabled(plugin_id):
            return "已禁用"
        if manager.errors.get(plugin_id) or manager.errors.get(str(manifest.get("_folder"))):
            return "加载失败"
        if manager.is_loaded(plugin_id):
            return "已启用"
        return "未加载"

    def _toggle_plugin_setting(self, plugin_id: str, var: tk.BooleanVar) -> None:
        """开关切换即热更新：启用立刻挂上侧栏，禁用立刻卸载页面。"""
        manager = self.plugin_manager
        if bool(var.get()):
            manager.set_enabled(plugin_id, True)
            manifest = next((m for m in manager.manifests if str(m.get("id")) == plugin_id), None)
            if manifest is not None:
                self._hot_install_plugin(manifest)
            self._append_log(f"插件 {plugin_id} 已启用并即时加载。", "success")
        else:
            error = self._hot_unload_plugin(plugin_id)
            if error:
                self._append_log(f"无法停用插件 {plugin_id}：{error}", "warning")
                manager.set_enabled(plugin_id, True)  # 保持原状，开关会在列表重建后弹回
            else:
                manager.set_enabled(plugin_id, False)
                self._append_log(f"插件 {plugin_id} 已停用并即时卸载。", "info")
        self._rebuild_plugin_list()

    def _hot_install_plugin(self, manifest: dict[str, Any]) -> bool:
        """加载单个插件并立即注册页面、侧栏与事件流；失败只记录，不影响宿主。"""
        manager = self.plugin_manager
        plugin_id = str(manifest.get("id"))
        if manager.is_loaded(plugin_id):
            self._hot_unload_plugin(plugin_id)  # 幂等：重复安装先卸载旧实例
        plugin = manager.load_one(manifest)
        if plugin is None:
            self._rebuild_plugin_list()
            return False
        try:
            pages = plugin.build_pages(self.content)
            if not pages:
                raise ValueError("build_pages 未返回任何页面")
        except Exception as exc:
            manager.errors[plugin_id] = str(exc)
            manager.drop(plugin)
            self._append_log(f"插件页面构建失败 {plugin_id}: {exc}", "warning")
            self._rebuild_plugin_list()
            return False
        page_keys: list[str] = []
        before_key = next((key for key in self._nav_buttons if key in self._CORE_PAGE_ORDER), None)
        for key, label, frame in pages:
            self.pages[key] = frame
            frame.grid(row=0, column=0, sticky="nsew")
            if key != self._current_page:
                frame.grid_remove()
            self._add_nav_item(key, str(label), before_key)
            page_keys.append(key)
        manager.plugins.append(plugin)
        self._plugin_page_keys[plugin_id] = page_keys
        self._set_plugin_status(plugin_id, "idle", "未启动")
        self._rebuild_plugin_list()
        return True

    def _hot_unload_plugin(self, plugin_id: str) -> str | None:
        """卸载插件页面与独立进程；返回拒绝/失败原因，None 表示成功。"""
        manager = self.plugin_manager
        plugin = next((p for p in manager.plugins if p.id == plugin_id), None)
        if plugin is None:
            return None
        if plugin.is_running():
            return f"{plugin.display_name}正在运行，暂时不能停用插件"
        removed_keys = set(self._plugin_page_keys.get(plugin_id, []))
        plugin.on_unload()
        if self._current_page in removed_keys:
            fallback = next((key for key in self._nav_buttons if key not in removed_keys), None)
            if fallback is not None:
                self._show_page(fallback)
        for key in removed_keys:
            frame = self.pages.pop(key, None)
            if frame is not None:
                frame.grid_remove()
                frame.destroy()
            self._remove_nav_item(key)
        self._plugin_page_keys.pop(plugin_id, None)
        self._plugin_status.pop(plugin_id, None)
        manager.drop(plugin)
        self._purge_dead_buttons()
        return None

    def _rescan_plugins(self) -> None:
        """重新扫描 plugins/ 目录：新插件立即装载，被移除的立即卸载（热更新）。"""
        manager = self.plugin_manager
        manager.discover()
        removed = 0
        for plugin in list(manager.plugins):
            if all(str(m.get("id")) != plugin.id for m in manager.manifests):
                if self._hot_unload_plugin(plugin.id) is None:
                    removed += 1
        added = 0
        for manifest in manager.manifests:
            plugin_id = str(manifest.get("id"))
            if manager.is_enabled(plugin_id) and not manager.is_loaded(plugin_id):
                if self._hot_install_plugin(manifest):
                    added += 1
        self._rebuild_plugin_list()
        self._append_log(f"插件扫描完成：新装载 {added} 个，移除 {removed} 个。", "info")

    def _purge_dead_buttons(self) -> None:
        """清理忙状态注册表里已销毁的插件按钮，防止 command_done 触发 TclError。"""
        for command, buttons in list(self._command_buttons.items()):
            alive = []
            for button in buttons:
                try:
                    if button.winfo_exists():
                        alive.append(button)
                except tk.TclError:
                    pass
            if alive:
                self._command_buttons[command] = alive
            else:
                self._command_buttons.pop(command, None)

    def open_plugins_directory(self) -> None:
        try:
            PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(PLUGINS_DIR)
        except OSError as exc:
            self._append_log(f"无法打开插件目录：{exc}", "warning")

    def _restart_application(self) -> None:
        """以相同的脚本与启动参数拉起新实例，再走正常关闭流程完成重启。"""
        try:
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            subprocess.Popen(
                [sys.executable, str(APP_DIR / "edge_workbench.py"), *sys.argv[1:]],
                cwd=str(APP_DIR),
                creationflags=flags,
                shell=False,
            )
        except OSError as exc:
            self._append_log(f"重启失败：{exc}", "warning")
            return
        self._append_log("已启动新的程序实例，当前窗口即将关闭…", "info")
        self._on_close()

    def _append_log(self, message: str, level: str = "info") -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{now_text()}] ", "timestamp")
        self.log_text.insert("end", f"{message}\n", level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def open_log_file(self) -> None:
        OPERATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        OPERATIONS_LOG.touch(exist_ok=True)
        os.startfile(OPERATIONS_LOG)

    def open_profile_directory(self) -> None:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(PROFILE_DIR)

    def _request_stats(self) -> None:
        self.worker.submit("stats")

    # 会长时间占用浏览器/影响数据的命令，缓存清理类操作在它们运行期间应被拒绝
    _TASK_COMMANDS = {
        "search_task": "搜索任务",
        "auto_run": "一键自动化",
        "trigger_tasks": "开始每日任务",
        "simulate": "模拟真实浏览",
        "rewards_data": "刷新全部数据",
        "test_click": "测试点击",
    }

    def _running_task_label(self) -> str | None:
        return self._TASK_COMMANDS.get(str(self.worker.current_command or ""))

    def clean_browser_cache(self) -> None:
        running = self._running_task_label()
        if running:
            self._append_log(f"{running}正在运行，为避免中断任务已跳过「清理缓存」；请等任务结束，或用「停止浏览器」先停掉任务。", "warning")
            return
        self._disable_for("clean_cache")
        self._set_busy("正在清理可回收缓存…")
        self.worker.submit("clean_cache")

    def restart_browser(self) -> None:
        running = self._running_task_label()
        if running:
            self._append_log(f"{running}正在运行，「重启浏览器」会先取消任务再重启。", "warning")
            self.worker.cancel_search_task()
        self._disable_for("restart")
        self._set_busy("正在重启浏览器…")
        self.worker.submit("restart")

    def start_browser(self) -> None:
        self._disable_for("start")
        self._set_busy("正在启动…")
        self.worker.submit("start")

    def auto_run(self) -> None:
        self._set_busy("一键自动化：正在执行完整流程…")
        self.worker.submit("auto_run")

    def set_browser_window_visible(self, visible: bool) -> None:
        """Set the shared browser-window mode used by the host and plugins."""
        self._set_headless_mode(not bool(visible))
        if self.worker.context is not None:
            self._append_log("总览页浏览器正在运行；新窗口模式将在它下次重启时应用，可点击「重启浏览器」立即生效。", "info")

    def _set_headless_mode(self, value: bool) -> None:
        value = bool(value)
        self.worker.headless = value
        for plugin in self.plugin_manager.plugins:
            plugin.runtime.headless = value
            sync = getattr(plugin, "sync_browser_window_visibility", None)
            if callable(sync):
                try:
                    sync(not value)
                except tk.TclError:
                    pass

        button = getattr(self, "headless_button", None)
        if button is not None:
            if value:
                button.configure(fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], text_color="#ffffff")
                self._append_log("隐藏模式已开启：浏览器启动时将在后台运行，自动化照常执行。", "info")
            else:
                button.configure(fg_color="#f2f2f0", hover_color="#e7e7e4", text_color=COLORS["text"])
                self._append_log("隐藏模式已关闭：浏览器启动时将显示窗口。", "info")

    def _toggle_headless(self) -> None:
        if self.worker.current_command == "restart":
            return  # 自动重启已在进行中，避免连点造成反复重启
        self._set_headless_mode(not self.worker.headless)
        if self.worker.context is not None:
            self._append_log("正在自动重启浏览器，使新的窗口显示模式立即生效。", "info")
            self.restart_browser()

    def _sync_spider_button(self) -> None:
        button = getattr(self, "spider_button", None)
        if button is not None:
            if self._spider_overlay_visible:
                button.configure(fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], text_color="#ffffff")
            else:
                button.configure(fg_color="#f2f2f0", hover_color="#e7e7e4", text_color=COLORS["text"])
        switch_var = getattr(self, "_pet_overlay_var", None)
        if switch_var is not None:
            try:
                switch_var.set(1 if self._spider_overlay_visible else 0)
            except Exception:
                pass
        self._sync_pet_status()

    def toggle_spider_overlay(self) -> None:
        """开关操作可视化（蜘蛛爬虫特效）：worker 持久化设置并立即应用到已打开页面。"""
        self._spider_overlay_visible = not self._spider_overlay_visible
        self._sync_spider_button()
        self.worker.submit("spider_overlay", enabled=self._spider_overlay_visible)

    def _apply_auto_status(self, event: dict[str, Any]) -> None:
        state = str(event.get("state") or "")
        message = str(event.get("message") or "")
        if state == "started":
            self._set_busy("一键自动化：正在执行…")
        elif state == "running":
            self._set_busy(message)
        elif state in {"done", "cancelled", "error"}:
            self._set_busy("")
            if state == "done":
                self._append_log("一键自动化全部完成。", "success")
            elif state == "cancelled":
                self._append_log("一键自动化已取消。", "warning")

    def open_url(self) -> None:
        self._disable_for("navigate")
        self.worker.submit("navigate", url=self._url_var.get())

    def bing_search(self) -> None:
        self._disable_for("bing_search")
        self._set_busy("正在必应搜索…")
        self.worker.submit("bing_search", query=self._url_var.get())

    def new_tab(self) -> None:
        self._disable_for("new_tab")
        self.worker.submit("new_tab", url=HOME_URL)

    def simulate(self) -> None:
        self._disable_for("simulate")
        self._set_busy("正在模拟真实浏览…")
        self.worker.submit("simulate")

    def stop_browser_and_task(self) -> None:
        self._disable_for("stop")
        self.worker.cancel_search_task()
        self.worker.submit("stop")

    def save_snapshot(self) -> None:
        self._disable_for("snapshot")
        self.worker.submit("snapshot")

    def _set_status(self, state: str, detail: str = "") -> None:
        status_dot = getattr(self, "status_dot", None)
        if state == "running":
            self._status_var.set("运行中")
            if status_dot is not None:
                status_dot.configure(text_color=COLORS["success"])
            self._set_busy("")
        elif state == "starting":
            self._status_var.set("启动中")
            if status_dot is not None:
                status_dot.configure(text_color=COLORS["warning"])
            self._set_busy(detail or "正在启动…")
        else:
            self._status_var.set("未运行")
            if status_dot is not None:
                status_dot.configure(text_color=COLORS["muted"])
            self._set_busy("")
        if detail:
            self._status_detail_var.set(detail)

    def _apply_stats(self, data: dict[str, Any]) -> None:
        self._metric_labels["profile_size"].configure(text=format_bytes(int(data.get("profile_size", 0))))
        self._metric_labels["cache_size"].configure(text=format_bytes(int(data.get("cache_size", 0))))
        self._metric_labels["site_data_size"].configure(text=format_bytes(int(data.get("site_data_size", 0))))
        self._metric_labels["cookie_count"].configure(text=str(data.get("cookie_count", 0)))
        self._metric_labels["history_count"].configure(text=str(data.get("history_count", 0)))
        self._metric_labels["page_count"].configure(text=str(data.get("page_count", 0)))
        self._metric_labels["history_size"].configure(text=format_bytes(int(data.get("history_size", 0))))
        self._cache_cleanup_var.set(f"可回收缓存：{format_bytes(int(data.get('removable_cache', 0)))}")
        profile_text = str(data.get("profile_dir", ""))
        if len(profile_text) > 20:
            profile_text = "…" + profile_text[-19:]
        self._metric_labels["profile_dir"].configure(text=profile_text)

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        plugin_id = str(event.get("plugin_id") or "")

        if event_type == "log":
            self._append_log(str(event.get("message", "")), str(event.get("level", "info")))
            return

        if event_type == "plugin_process_error":
            message = str(event.get("message") or "插件进程异常")
            if plugin_id:
                self._set_plugin_status(plugin_id, "error", "异常", message)
            self._append_log(message, "error")
            if plugin_id:
                self.plugin_manager.dispatch(event, plugin_id)
            return

        if event_type in {"auto_status", "search_task_status"} and plugin_id:
            state = str(event.get("state") or "")
            message = str(event.get("message") or "")
            if event_type == "auto_status":
                if state in {"started", "running"}:
                    self._set_plugin_status(plugin_id, "running", "运行中", message)
                elif state == "done":
                    self._set_plugin_status(plugin_id, "done", "已完成", message)
                elif state == "cancelled":
                    self._set_plugin_status(plugin_id, "cancelled", "已取消", message)
                elif state == "error":
                    self._set_plugin_status(plugin_id, "error", "异常", message)
            else:
                if state in {"planning", "planned", "news", "searching", "waiting", "scanning", "syncing"}:
                    self._set_plugin_status(plugin_id, "running", "运行中", message)
                elif state == "done":
                    self._set_plugin_status(plugin_id, "done", "已完成", message)
                elif state == "cancelled":
                    self._set_plugin_status(plugin_id, "cancelled", "已取消", message)
                elif state == "error":
                    self._set_plugin_status(plugin_id, "error", "异常", message)
            self.plugin_manager.dispatch(event, plugin_id)
            return

        if event_type == "status" and plugin_id:
            state = str(event.get("state") or "")
            detail = str(event.get("detail") or "")
            if state == "starting":
                self._set_plugin_status(plugin_id, "starting", "启动中", detail)
            elif state == "running":
                self._set_plugin_status(plugin_id, "running", "运行中", detail)
            elif state == "stopped":
                self._set_plugin_status(plugin_id, "stopped", "未启动", detail)
            self.plugin_manager.dispatch(event, plugin_id)
            return

        if event_type == "command_done":
            command = str(event.get("command") or "")
            for button in self._command_buttons.get(command, []):
                try:
                    button.configure(state="normal")
                except tk.TclError:
                    pass
            if plugin_id:
                current = self._plugin_status.get(plugin_id, {}).get("state", "")
                if current not in {"done", "cancelled", "error"}:
                    if command == "stop":
                        self._set_plugin_status(plugin_id, "stopped", "未启动")
                    elif current == "running":
                        self._set_plugin_status(plugin_id, "ready", "就绪")
                self.plugin_manager.dispatch(event, plugin_id)
            return

        if event_type == "status":
            self._set_status(str(event.get("state", "stopped")), str(event.get("detail", "")))
        elif event_type == "stats" and not plugin_id:
            self._apply_stats(event.get("data", {}))
            self._last_stats_refresh = time.time()
        elif event_type == "url" and not plugin_id:
            url = str(event.get("url", ""))
            if url and url != "about:blank":
                self._url_var.set(url)
        elif event_type == "page_count" and not plugin_id:
            self._metric_labels["page_count"].configure(text=str(event.get("count", 0)))
        elif event_type == "snapshot_saved" and not plugin_id:
            self._append_log(f"快照路径: {event.get('path')}", "success")
        elif event_type == "auto_status":
            self._apply_auto_status(event)
            self.plugin_manager.dispatch(event)
        elif event_type in PluginManager.PLUGIN_EVENT_TYPES:
            self.plugin_manager.dispatch(event, plugin_id or None)
        elif event_type == "focus_ui" and not plugin_id:
            self._bring_to_front()

    def _poll_events(self) -> None:
        if self._closing:
            return
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass

        for event in self.plugin_manager.poll_events():
            try:
                self._handle_event(event)
            except Exception as exc:
                self._append_log(f"插件事件处理失败: {exc}", "warning")

        if time.time() - self._last_stats_refresh > 25:
            self._last_stats_refresh = time.time()
            self._request_stats()
        self.after(120, self._poll_events)

    def _bring_to_front(self) -> None:
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(1200, lambda: self.attributes("-topmost", False))
            self.focus_force()
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._set_busy("正在保存并退出…")
        self.worker.cancel_search_task()
        self.worker.submit("shutdown")
        for plugin in list(self.plugin_manager.plugins):
            plugin.runtime.cancel()
        threading.Thread(target=self.plugin_manager.shutdown_all, name="plugin-shutdown", daemon=True).start()
        self._close_deadline = time.time() + 4.0
        self.after(100, self._finish_close)

    def _finish_close(self) -> None:
        if self.worker.is_alive() and time.time() < self._close_deadline:
            self.after(100, self._finish_close)
            return
        self.destroy()


def enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            # Per-monitor DPI aware：高分屏缩放下窗口按原生分辨率渲染，字体不发虚
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Microsoft Edge persistent browser workbench powered by Playwright")
    parser.add_argument("--autostart", action="store_true", help="Start Edge and open Bing immediately")
    args = parser.parse_args()

    ensure_directories()
    enable_windows_dpi_awareness()
    app = EdgeWorkbenchApp()
    if args.autostart:
        if app.plugin_manager.plugins:
            app._append_log("检测到进程隔离插件：不自动启动全局浏览器，请在插件页启动独立浏览器。", "info")
        else:
            app.after(900, app.start_browser)
    app.mainloop()
    return 0
