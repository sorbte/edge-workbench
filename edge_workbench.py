from __future__ import annotations

import argparse
import importlib.util
import json
import math
import multiprocessing as mp
import os
import queue
import random
import re
import shutil
import sqlite3
import subprocess
import sys

sys.dont_write_bytecode = True

# 插件通过 `from edge_workbench import ...` 复用宿主的 UI 套件与常量；
# 以脚本方式运行时本模块名是 __main__，提前注册别名，避免插件加载时把本文件二次执行一遍
sys.modules.setdefault("edge_workbench", sys.modules[__name__])

import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


APP_DIR = Path(__file__).resolve().parent
PROFILE_DIR = APP_DIR / "edge_profile"
RUNTIME_DIR = APP_DIR / "runtime"
PLUGINS_DIR = APP_DIR / "plugins"
PLUGIN_SETTINGS_FILE = RUNTIME_DIR / "plugins.json"
STATE_DIR = RUNTIME_DIR / "state_snapshots"
DOWNLOAD_DIR = APP_DIR / "downloads"
OPERATIONS_LOG = RUNTIME_DIR / "operations.jsonl"
REWARDS_DATA_FILE = RUNTIME_DIR / "rewards_data.json"
REWARDS_ACCOUNT_FILE = RUNTIME_DIR / "rewards_account.json"
REWARDS_ABOUT_URL = "https://rewards.bing.com/about"
TEST_CLICK_SCREENSHOT = RUNTIME_DIR / "test_click.png"
REWARDS_EARN_URL = "https://rewards.bing.com/earn"
REWARDS_DASHBOARD_URL = "https://rewards.bing.com/dashboard"
SEARCH_TASK_LOG = RUNTIME_DIR / "search_task.jsonl"
NEWS_CACHE_FILE = RUNTIME_DIR / "news_cache.json"
TOUTIAO_URL = "https://www.toutiao.com/"
SEARCH_POINTS_PER_ACTION = 3
SEARCH_NEWS_EXTRA = 5
SEARCH_TASK_MAX_ACTIONS = 35
SEARCH_INTERVAL_MIN_SECONDS = 18
SEARCH_INTERVAL_MAX_SECONDS = 42
SEARCH_NO_INCREASE_LIMIT = 3
SEARCH_MAX_COOLDOWNS = 1
SEARCH_COOLDOWN_SECONDS = 15 * 60
SEARCH_CALIBRATION_EVERY = 10
SAFE_CACHE_RELATIVE_PATHS = (
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/DawnGraphiteCache",
    "Default/DawnWebGPUCache",
    "Default/Shared Dictionary",
    "GrShaderCache",
    "ShaderCache",
    "BrowserMetrics",
    "Default/settings_diagnostic.log",
    "Default/favorites_diagnostic.log",
    "Default/load_statistics.db",
    "Default/load_statistics.db-wal",
    "Default/load_statistics.db-shm",
)

HOME_URL = "https://cn.bing.com/"
EDGE_EXE_CANDIDATES = (
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
)

COLORS = {
    "bg": "#f7f7f5",
    "panel": "#ffffff",
    "panel_alt": "#f1f1ef",
    "border": "#d7d7d1",
    "border_strong": "#c5c5bf",
    "text": "#0d0d0d",
    "muted": "#62625e",
    "accent": "#0d0d0d",
    "accent_hover": "#2b2b28",
    "success": "#16a34a",
    "warning": "#d97706",
    "danger": "#dc2626",
    "term_bg": "#161616",
    "term_border": "#2b2b2b",
    "term_fg": "#d6d6d6",
    "term_muted": "#8a8a85",
    "term_select": "#3a3a38",
    "log_bg": "#161616",
}

FONT_MONO = "Cascadia Mono"
FONT_SANS = "Microsoft YaHei UI"
UI_TEXT_MIN_SIZE = 12

# Segoe MDL2 Assets 图标字形（Windows 自带图标字体），用于总览页图标按钮
ICON_PLAY = "\uE768"        # 启动浏览器
ICON_STOP = "\uE71A"        # 停止浏览器
ICON_RESTART = "\uE777"     # 重启浏览器
ICON_WINDOW = "\uE7F4"      # 隐藏/显示浏览器窗口
ICON_REFRESH = "\uE72C"     # 刷新统计
ICON_SNAPSHOT = "\uE787"    # 保存状态快照
ICON_CLEAN = "\uE74D"       # 清理缓存
ICON_FOLDER = "\uE8B7"      # 打开资料目录
ICON_NEW_TAB = "\uE710"     # 新建标签页
ICON_OPEN_FILE = "\uE8E5"   # 打开数据 JSON
ICON_TEST_CLICK = "\uE7C9"  # 测试点击

CONTENT_MARGIN_L = 10
CONTENT_MARGIN_T = 8
CONTENT_MARGIN_R = 0  # 内容卡片贴右、下窗口边缘
CONTENT_MARGIN_B = 0
CARD_CORNER_RADIUS = 12


def mono_font(size: int, weight: str = "normal") -> tuple[str, int, str]:
    return (FONT_MONO, size, weight)


def sans_font(size: int, weight: str = "normal") -> tuple[str, int, str]:
    return (FONT_SANS, max(size, UI_TEXT_MIN_SIZE), weight)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_directories() -> None:
    for path in (PROFILE_DIR, RUNTIME_DIR, STATE_DIR, DOWNLOAD_DIR):
        path.mkdir(parents=True, exist_ok=True)


def find_edge_executable() -> Path | None:
    for candidate in EDGE_EXE_CANDIDATES:
        try:
            if candidate and candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def normalize_url(raw: str) -> str:
    text = raw.strip()
    if not text:
        return HOME_URL
    if text.startswith(("http://", "https://", "edge://", "about:")):
        return text
    if " " in text or "." not in text:
        from urllib.parse import quote_plus
        return f"{HOME_URL.rstrip('/')}/search?q={quote_plus(text)}"
    return "https://" + text


def copy_locked_sqlite(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(destination) + suffix))
    return destination


def count_history_entries(profile_dir: Path = PROFILE_DIR, runtime_dir: Path = RUNTIME_DIR) -> int:
    history = profile_dir / "Default" / "History"
    if not history.exists():
        return 0
    temp = runtime_dir / "_history_readonly.sqlite"
    try:
        copy_locked_sqlite(history, temp)
        connection = sqlite3.connect(f"file:{temp.as_posix()}?mode=ro", uri=True, timeout=3)
        try:
            return int(connection.execute("SELECT COUNT(*) FROM urls").fetchone()[0])
        finally:
            connection.close()
    except Exception:
        return 0
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(temp) + suffix).unlink(missing_ok=True)
            except OSError:
                pass


def count_cookies_database(profile_dir: Path = PROFILE_DIR, runtime_dir: Path = RUNTIME_DIR) -> int:
    cookie_paths = (
        profile_dir / "Default" / "Network" / "Cookies",
        profile_dir / "Default" / "Cookies",
    )
    for source in cookie_paths:
        if not source.exists():
            continue
        temp = runtime_dir / "_cookies_readonly.sqlite"
        try:
            copy_locked_sqlite(source, temp)
            connection = sqlite3.connect(f"file:{temp.as_posix()}?mode=ro", uri=True, timeout=3)
            try:
                return int(connection.execute("SELECT COUNT(*) FROM cookies").fetchone()[0])
            finally:
                connection.close()
        except Exception:
            continue
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(str(temp) + suffix).unlink(missing_ok=True)
                except OSError:
                    pass
    return 0


def profile_breakdown(profile_dir: Path = PROFILE_DIR, download_dir: Path = DOWNLOAD_DIR) -> dict[str, int]:
    default = profile_dir / "Default"
    components = {
        "HTTP 缓存": [default / "Cache", default / "Code Cache", default / "GPUCache"],
        "站点数据": [
            default / "Local Storage",
            default / "Session Storage",
            default / "IndexedDB",
            default / "Service Worker",
        ],
        "Cookies": [default / "Network" / "Cookies", default / "Cookies"],
        "历史记录": [default / "History", default / "History-journal"],
        "下载与偏好": [default / "Preferences", DOWNLOAD_DIR],
        "可回收缓存": [PROFILE_DIR / relative for relative in SAFE_CACHE_RELATIVE_PATHS],
    }
    result: dict[str, int] = {}
    for name, paths in components.items():
        total = 0
        for path in paths:
            if path.is_dir():
                total += directory_size(path)
            elif path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        result[name] = total
    result["全部用户数据"] = directory_size(PROFILE_DIR)
    return result


def append_operation(entry: dict[str, Any], log_path: Path = OPERATIONS_LOG) -> None:
    payload = {"timestamp": now_text(), **entry}
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


@dataclass
class PageSnapshot:
    url: str
    title: str
    localStorage_keys: int
    sessionStorage_keys: int
    document_cookie_names: list[str]


REWARDS_EXTRACTION_SCRIPT = r"""
() => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const uniq = (items) => {
    const seen = new Set();
    return items.filter((item) => {
      const key = JSON.stringify(item);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  };
  const bodyText = clean(document.body.innerText);
  const numberAfterLabel = (label, maxDistance = 220) => {
    const index = bodyText.indexOf(label);
    if (index < 0) return null;
    const snippet = bodyText.slice(index, index + maxDistance);
    const match = snippet.match(/(\d{1,3}(?:,\d{3})+|\d{1,7})/);
    return match ? parseInt(match[1].replace(/,/g, ''), 10) : null;
  };
  const getHeading = (headingText) => {
    const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,div,span,p'));
    let best = null;
    for (const node of nodes) {
      const ownText = clean(Array.from(node.childNodes).filter((child) => child.nodeType === 3).map((child) => child.textContent).join(''));
      const fullText = clean(node.textContent);
      const isExact = ownText === headingText || (node.children.length === 0 && fullText === headingText);
      if (!isExact) continue;
      if (!best || node.querySelectorAll('*').length < best.querySelectorAll('*').length) best = node;
    }
    return best;
  };
  const getSection = (headingText) => {
    const heading = getHeading(headingText);
    if (!heading) return null;
    let node = heading;
    let best = heading.parentElement;
    for (let depth = 0; depth < 7 && node && node.parentElement; depth += 1) {
      node = node.parentElement;
      const text = clean(node.innerText);
      const statusCount = (text.match(/已完成|未完成/g) || []).length;
      if (statusCount >= 1 && text.length < 12000) best = node;
      if (statusCount >= 2 || (statusCount >= 1 && text.length < 2500)) return node;
    }
    return best;
  };
  const getCard = (pointNode) => {
    let node = pointNode;
    let best = null;
    for (let depth = 0; depth < 8 && node && node.parentElement; depth += 1) {
      node = node.parentElement;
      const text = clean(node.innerText);
      const statusCount = (text.match(/已完成|未完成/g) || []).length;
      if (statusCount === 1 && text.length >= 4 && text.length <= 420) best = node;
      if (statusCount > 1 || text.length > 420) break;
    }
    return best;
  };
  const extractTasks = (sectionName) => {
    const section = getSection(sectionName);
    if (!section) return [];
    const pointNodes = Array.from(section.querySelectorAll('div,p,span')).filter((node) => {
      const value = clean(node.innerText);
      const className = typeof node.className === 'string' ? node.className : '';
      const rewardBadge = /statusSuccess|statusInformative|statusWarning|reward/i.test(className);
      return rewardBadge && (/^\+\d{1,4}$/.test(value) || /^\d{1,4}$/.test(value) || /^\d{1,4}\s*积分$/.test(value));
    });
    const tasks = [];
    for (const pointNode of pointNodes) {
      const pointText = clean(pointNode.innerText);
      const pointMatch = pointText.match(/(\d{1,4})/);
      if (!pointMatch) continue;
      const points = parseInt(pointMatch[1], 10);
      if (!points) continue;
      const card = getCard(pointNode);
      if (!card) continue;
      const cardText = clean(card.innerText);
      if (!/已完成|未完成/.test(cardText)) continue;
      const completed = !cardText.includes('未完成') && cardText.includes('已完成');
      const status = completed ? '已完成' : '未完成';
      const lines = card.innerText.split(/\n+/).map(clean).filter(Boolean);
      const ignored = (line) => {
        const normalized = line.replace(/\s/g, '');
        return !line || normalized === '+' + points || normalized === String(points) || normalized === String(points) + '积分' || normalized === '已完成' || normalized === '未完成' || normalized === '积分' || /^[+*]?\d+$/.test(normalized);
      };
      const contentLines = lines.filter((line) => !ignored(line));
      const title = contentLines[0] || '';
      const description = contentLines.slice(1).join(' ').slice(0, 160);
      tasks.push({ title, description, points, status, completed });
    }
    const seen = new Set();
    return tasks.filter((task) => {
      const key = task.title + '|' + task.points + '|' + task.status;
      if (!task.title || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  };
  const progressPairs = uniq(Array.from(bodyText.matchAll(/(\d{1,4})\s*\/\s*(\d{1,4})/g)).map((match) => ({
    text: match[0].replace(/\s+/g, ''),
    current: parseInt(match[1], 10),
    target: parseInt(match[2], 10),
  })));
  const progressByTarget = (target) => progressPairs.find((item) => item.target === target) || null;
  const edgeMatch = bodyText.match(/(?:Microsoft\s*)?Edge[^0-9]{0,80}?(\d{1,4})\s*\/\s*(\d{1,4})/i);
  const dailyMatch = bodyText.match(/日常任务\s*(\d{1,4})\s*\/\s*(\d{1,4})/);
  const searchRow = bodyText.match(/必应搜索\s+(\d{1,5})(?:\s|\/|$)/);
  const searchProgressMatch = bodyText.match(/必应搜索\s+(\d{1,5})\s*\/\s*(\d{1,5})/);
  const offerRow = bodyText.match(/优惠\s+(\d{1,5})(?:\s|$)/);
  const accountMatch = bodyText.slice(0, 1000).match(/\b\d{1,3}(?:,\d{3})+\b/);
  const loginRequired = /登录|Sign in|Sign-in/i.test(bodyText.slice(0, 1000)) && !/今日积分|积分明细|日常任务/.test(bodyText);
  const dailyTasks = extractTasks('日常任务');
  const dailyActivities = extractTasks('每日活动');
  return {
    url: location.href,
    title: document.title,
    captured_at: new Date().toISOString(),
    login_required: loginRequired,
    account_points: accountMatch ? parseInt(accountMatch[0].replace(/,/g, ''), 10) : null,
    today_points: numberAfterLabel('今日积分'),
    daily_task_progress: dailyMatch ? { current: parseInt(dailyMatch[1], 10), target: parseInt(dailyMatch[2], 10), text: dailyMatch[1] + '/' + dailyMatch[2] } : null,
    today_points_progress: progressByTarget(60),
    edge_browsing: edgeMatch ? { current: parseInt(edgeMatch[1], 10), target: parseInt(edgeMatch[2], 10), text: edgeMatch[1] + '/' + edgeMatch[2] } : null,
    search_row_points: searchRow ? parseInt(searchRow[1], 10) : null,
    search_progress: searchProgressMatch ? { current: parseInt(searchProgressMatch[1], 10), target: parseInt(searchProgressMatch[2], 10), text: searchProgressMatch[1] + '/' + searchProgressMatch[2] } : null,
    offer_row_points: offerRow ? parseInt(offerRow[1], 10) : null,
    progress_pairs: progressPairs,
    daily_tasks: dailyTasks,
    daily_activities: dailyActivities,
    daily_tasks_completed: dailyTasks.filter((task) => task.completed).length,
    daily_tasks_pending: dailyTasks.filter((task) => !task.completed).length,
    raw_text_preview: bodyText.slice(0, 1500),
  };
}
"""


REWARDS_ACCOUNT_EXTRACTION_SCRIPT = r"""
() => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const bodyText = clean(document.body.innerText);
  const lines = String(document.body.innerText || '').split(/\n+/).map(clean).filter(Boolean);
  const numberAfter = (label, maxDistance = 220) => {
    const index = bodyText.indexOf(label);
    if (index < 0) return null;
    const snippet = bodyText.slice(index, index + maxDistance);
    const match = snippet.match(/(\d{1,3}(?:,\d{3})+|\d{1,7})/);
    return match ? parseInt(match[1].replace(/,/g, ''), 10) : null;
  };
  const badgeNodes = Array.from(document.querySelectorAll('p,span,div')).filter((node) => {
    const text = clean(node.innerText);
    return /^(会员|银牌会员|金牌会员|铜牌会员)$/.test(text);
  });
  const badgeText = badgeNodes.length ? clean(badgeNodes[0].innerText) : (bodyText.match(/(金牌会员|银牌会员|铜牌会员|会员)/) || [])[1] || null;
  let username = null;
  if (badgeNodes.length) {
    let node = badgeNodes[0];
    for (let depth = 0; depth < 7 && node && node.parentElement; depth += 1) {
      node = node.parentElement;
      const candidateLines = clean(node.innerText).split(/\n+/).map(clean).filter(Boolean);
      const index = candidateLines.findIndex((line) => line === badgeText);
      if (index > 0) {
        const candidate = candidateLines[index - 1];
        if (candidate && candidate.length <= 80 && !/可用积分|可领取|了解详细信息|Microsoft Rewards/i.test(candidate)) {
          username = candidate;
          break;
        }
      }
    }
  }
  if (!username && badgeText) {
    const index = lines.findIndex((line) => line === badgeText);
    if (index > 0) username = lines[index - 1];
  }
  const upgradeMatch = bodyText.match(/还需完成活动\s*[:：]?\s*(\d{1,4})/);
  const streakMatch = bodyText.match(/每日连续打卡\s*(\d{1,4})\s*天/);
  return {
    url: location.href,
    title: document.title,
    captured_at: new Date().toISOString(),
    username,
    membership_level: badgeText,
    available_points: numberAfter('可用积分'),
    claimable_points: numberAfter('可领取'),
    next_level_activities: upgradeMatch ? parseInt(upgradeMatch[1], 10) : null,
    streak_days: streakMatch ? parseInt(streakMatch[1], 10) : null,
    raw_text_preview: bodyText.slice(0, 1600),
  };
}
"""


REWARDS_TASK_CLICK_SCRIPT = r"""
(config) => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const getHeading = (headingText) => {
    const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,div,span,p'));
    let best = null;
    for (const node of nodes) {
      const ownText = clean(Array.from(node.childNodes).filter((child) => child.nodeType === 3).map((child) => child.textContent).join(''));
      const fullText = clean(node.textContent);
      const isExact = ownText === headingText || (node.children.length === 0 && fullText === headingText);
      if (!isExact) continue;
      if (!best || node.querySelectorAll('*').length < best.querySelectorAll('*').length) best = node;
    }
    return best;
  };
  const getSection = (headingText) => {
    const heading = getHeading(headingText);
    if (!heading) return null;
    let node = heading;
    let best = heading.parentElement;
    for (let depth = 0; depth < 7 && node && node.parentElement; depth += 1) {
      node = node.parentElement;
      const text = clean(node.innerText);
      const statusCount = (text.match(/已完成|未完成/g) || []).length;
      if (statusCount >= 1 && text.length < 12000) best = node;
      if (statusCount >= 2 || (statusCount >= 1 && text.length < 2500)) return node;
    }
    return best;
  };
  const getCard = (statusNode) => {
    let node = statusNode;
    let best = null;
    for (let depth = 0; depth < 8 && node && node.parentElement; depth += 1) {
      node = node.parentElement;
      const text = clean(node.innerText);
      const statusCount = (text.match(/已完成|未完成/g) || []).length;
      if (statusCount === 1 && text.length >= 4 && text.length <= 500) best = node;
      if (statusCount > 1 || text.length > 500) break;
    }
    return best;
  };
  const section = getSection(config.section);
  if (!section) return { found: false, missing_section: true, total: 0, completed: 0, pending: 0 };
  const statusNodes = Array.from(section.querySelectorAll('span,div,p')).filter((node) => {
    const text = clean(node.innerText);
    return text === '已完成' || text === '未完成';
  });
  const seen = new Set();
  const tasks = [];
  for (const statusNode of statusNodes) {
    const card = getCard(statusNode);
    if (!card || seen.has(card)) continue;
    seen.add(card);
    const text = clean(card.innerText);
    const status = text.includes('未完成') ? '未完成' : '已完成';
    const lines = card.innerText.split(/\n+/).map(clean).filter(Boolean);
    const ignored = (line) => {
      const normalized = line.replace(/\s/g, '');
      return !line || normalized === '已完成' || normalized === '未完成' || normalized === '积分' || /^[+*]?\d+$/.test(normalized);
    };
    const contentLines = lines.filter((line) => !ignored(line));
    const title = contentLines[0] || '';
    const pointMatch = text.match(/(\d{1,4})\s*积分|(?:\+|\s)(\d{1,4})(?=\s|$)/);
    const points = pointMatch ? parseInt(pointMatch[1] || pointMatch[2], 10) : null;
    tasks.push({ card, title, status, points });
  }
  const completed = tasks.filter((task) => task.status === '已完成').length;
  const pending = tasks.filter((task) => task.status === '未完成');
  const available = pending.find((task) => !config.processed.includes(task.title));
  if (!available) {
    return { found: false, total: tasks.length, completed, pending: pending.length, titles: tasks.map((task) => ({ title: task.title, status: task.status })) };
  }
  const id = config.section + '-' + String(config.sequence || 0) + '-' + String(Date.now());
  available.card.setAttribute('data-codex-reward-click', id);
  try { available.card.scrollIntoView({ block: 'center', behavior: 'instant' }); } catch (error) {}
  return {
    found: true,
    id,
    title: available.title,
    status: available.status,
    points: available.points,
    total: tasks.length,
    completed,
    pending: pending.length,
    titles: tasks.map((task) => ({ title: task.title, status: task.status })),
  };
}
"""


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
        self.current_command: str | None = None
        self.namespace = namespace
        self.command_handlers: dict[str, Any] = {}
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
            except Exception:
                pass

        def page_closed(_page: Page) -> None:
            self.emit("page_count", count=self._page_count())
            self.log("已关闭一个标签页。", "info")

        page.on("framenavigated", frame_navigated)
        page.on("close", page_closed)
        self.emit("url", url=page.url)
        self.emit("page_count", count=self._page_count())

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
        self.emit("url", url=page.url)
        self._append_operation({"event": "simulation_complete", "url": page.url, "title": title})
        self.log(f"模拟浏览完成，当前页面: {title or page.url}", "success")
        self.emit("status", state="running", detail="模拟浏览完成，新增缓存已写入磁盘")
        self.publish_stats()

    def _wait_for_rewards_ready(self, page: Page) -> None:
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
                        candidate.click(timeout=5000)
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
        daily_tasks = earn.get("daily_tasks") or dashboard.get("daily_activities") or []
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
                f"日常任务={summary.get('daily_task_progress')}，"
                f"已完成={summary.get('daily_tasks_completed')}，"
                f"未完成={summary.get('daily_tasks_pending')}，"
                f"Edge={summary.get('edge_browsing')}",
                "success",
            )
            if task_preview:
                self.log(f"带积分日常任务: {task_preview}")
            if summary.get("login_required"):
                self.log("Rewards 页面要求登录；请先在此 Edge 窗口登录 Microsoft 账号后重试。", "warning")
            self.emit("rewards_data", data=summary)
            self.emit("status", state="running", detail="Rewards 基本数据已获取")

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
                f"日常任务={summary.get('daily_task_progress')}，"
                f"已完成={summary.get('daily_tasks_completed')}，"
                f"未完成={summary.get('daily_tasks_pending')}，"
                f"Edge={summary.get('edge_browsing')}",
                "success",
            )
            if task_preview:
                self.log(f"带积分日常任务: {task_preview}")
            if summary.get("login_required"):
                self.log("Rewards 页面要求登录；请先在此 Edge 窗口登录 Microsoft 账号后重试。", "warning")
            self.emit("rewards_data", data=summary)
            self.emit("status", state="running", detail="Rewards 基本数据已获取")
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
        earn = self._extract_rewards_page(page)
        earn.pop("raw_text_preview", None)
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass
        page.goto(REWARDS_DASHBOARD_URL, wait_until="domcontentloaded", timeout=45_000)
        self._wait_for_rewards_ready(page)
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
        for _ in range(random.randint(2, 4)):
            page.mouse.wheel(0, random.randint(350, 900))
            page.wait_for_timeout(random.randint(500, 1200))
            self._human_move(page, random.randint(180, 1050), random.randint(160, 650), steps=random.randint(6, 12))
        return query

    def _read_bing_total_points(self, page: Page) -> int | None:
        try:
            raw = page.evaluate(
                r"""() => {
                    const selectors = [
                        '#rh_rwm .points-container',
                        '#rh_rwm span.points-container',
                        '#rh_rwm .b_clickarea'
                    ];
                    for (const selector of selectors) {
                        const element = document.querySelector(selector);
                        if (element && element.innerText) return element.innerText;
                    }
                    const root = document.querySelector('#rh_rwm');
                    return root && root.innerText ? root.innerText : '';
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
        latest = previous
        for _ in range(3):
            self._check_cancel()
            current = self._read_bing_total_points(page)
            if current is not None:
                latest = current
                if previous is None or current > previous:
                    return current
            page.wait_for_timeout(random.randint(2500, 4200))
        return latest

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
        try:
            if self.context is None:
                self.emit("search_task_status", state="planning", message="浏览器未运行，正在自动启动 Edge…", current=0, total=0)
            page = self._current_page()
            self.emit("search_task_status", state="planning", message="正在计算今日搜索积分进度…", current=0, total=0)
            plan = self._build_search_plan(page)
            self.emit("search_task_status", state="planned", message="搜索计划已生成", current=0, total=plan["news_needed"], plan=plan)
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
                )
                query = self._search_news_item(page, title)
                points_before = last_points
                current_points = self._read_bing_points_with_retry(page, last_points)
                increased = current_points is not None and (last_points is None or current_points > last_points)
                if current_points is not None:
                    last_points = current_points
                no_increase_count = 0 if increased else no_increase_count + 1
                self._search_task_log({
                    "event": "search",
                    "index": index,
                    "total": total,
                    "title": title,
                    "query": query,
                    "points_before": points_before,
                    "points_after": current_points,
                    "increased": increased,
                    "no_increase_count": no_increase_count,
                })
                self.emit(
                    "search_task_status",
                    state="searching",
                    message=f"[{index}/{total}] 已完成：积分 {current_points if current_points is not None else '--'}",
                    current=index,
                    total=total,
                    points=current_points,
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
            self.emit("search_task_status", state="syncing", message="正在同步 Rewards 数据…", current=total, total=total)
            self.fetch_rewards_data()
            final_message = "搜索任务完成，积分已同步校准" if not stopped_due_to_no_increase else "积分未继续增长，已停止并完成校准"
            self.emit("search_task_status", state="done", message=final_message, current=total, total=total)
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
            page = self._current_page()
            clicked_total = 0
            self.log("开始扫描“每日活动”和“日常任务”：未完成项将逐个点击，已完成项跳过。")
            sections = [("每日活动", REWARDS_DASHBOARD_URL), ("日常任务", REWARDS_EARN_URL)]
            for section_name, section_url in sections:
                self._check_cancel()
                self.emit("search_task_status", state="scanning", message=f"正在扫描{section_name}…", current=clicked_total, total=0)
                page.goto(section_url, wait_until="domcontentloaded", timeout=45_000)
                self._wait_for_rewards_ready(page)
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
                    self._search_task_log({
                        "event": "task_trigger",
                        "section": section_name,
                        "title": title,
                        "points": points,
                        "clicked": clicked,
                        "sequence": sequence,
                    })
                    self.emit(
                        "search_task_status",
                        state="clicking",
                        message=f"{section_name}: 已触发 {clicked_total} 项（已完成自动跳过）",
                        current=clicked_total,
                        total=max(clicked_total, int(result.get("total") or 0)),
                    )

                    if clicked_total % 5 == 0:
                        self.log(f"已触发 {clicked_total} 项，进入 Rewards 详细页同步校准。")
                        self._calibrate_rewards_points(page, return_url=section_url)

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

            self.emit("search_task_status", state="syncing", message="未完成任务触发结束，正在同步 Rewards 数据…", current=clicked_total, total=clicked_total)
            if clicked_total > 0:
                self.fetch_rewards_data()
                self.log(f"未完成任务触发结束：共点击 {clicked_total} 项，已完成项均已跳过。", "success")
            else:
                self.log("没有发现需要点击的未完成任务。", "success")
                self._calibrate_rewards_points(page)
            self.emit("search_task_status", state="done", message=f"未完成任务处理完成：点击 {clicked_total} 项", current=clicked_total, total=clicked_total)
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


def _plugin_process_main(
    plugin_id: str,
    profile_dir: str,
    download_dir: str,
    runtime_dir: str,
    worker_entry: str,
    command_queue: Any,
    event_queue: Any,
    cancel_event: Any,
) -> None:
    """Run one isolated Playwright worker inside a dedicated plugin process."""
    try:
        worker = BrowserWorker(
            event_queue,
            commands=command_queue,
            name=f"plugin-{plugin_id}",
            profile_dir=Path(profile_dir),
            download_dir=Path(download_dir),
            runtime_dir=Path(runtime_dir),
            operations_log=Path(runtime_dir) / "operations.jsonl",
            cancel_event=cancel_event,
            namespace=plugin_id,
        )
        if worker_entry:
            worker_path = Path(worker_entry)
            spec = importlib.util.spec_from_file_location(
                f"workbench_plugin_worker_{plugin_id}", worker_path
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load plugin worker: {worker_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if not callable(register):
                raise AttributeError(f"Plugin worker missing register(worker): {worker_path}")
            register(worker)
        worker.run()
    except BaseException as exc:
        try:
            event_queue.put({
                "type": "plugin_process_error",
                "plugin_id": plugin_id,
                "message": str(exc),
                "traceback": traceback.format_exc(limit=8),
            })
        except Exception:
            pass


class PluginProcessRuntime:
    """主进程侧代理：把插件命令和事件绑定到独立子进程。"""

    def __init__(
        self,
        plugin_id: str,
        *,
        profile_dir: Path,
        download_dir: Path,
        runtime_dir: Path,
        worker_entry: Path | None = None,
        status_callback: Any | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.profile_dir = profile_dir
        self.download_dir = download_dir
        self.runtime_dir = runtime_dir
        self.worker_entry = worker_entry
        self.status_callback = status_callback
        self.headless = False
        self._ctx = mp.get_context("spawn")
        self.commands = self._ctx.Queue()
        self.events = self._ctx.Queue()
        self.cancel_event = self._ctx.Event()
        self.process: Any | None = None
        self.current_command: str | None = None
        self._busy = False
        self._closing = False
        self._exit_reported = False

    @property
    def process_id(self) -> int | None:
        return self.process.pid if self.process is not None and self.process.is_alive() else None

    def _notify(self, state: str, text: str, detail: str = "") -> None:
        if self.status_callback is None:
            return
        try:
            self.status_callback(self.plugin_id, state, text, detail)
        except Exception:
            pass

    def start(self) -> None:
        if self.process is not None and self.process.is_alive():
            return
        for path in (self.runtime_dir, self.profile_dir, self.download_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.process = self._ctx.Process(
            target=_plugin_process_main,
            args=(
                self.plugin_id,
                str(self.profile_dir),
                str(self.download_dir),
                str(self.runtime_dir),
                str(self.worker_entry or ""),
                self.commands,
                self.events,
                self.cancel_event,
            ),
            name=f"plugin-{self.plugin_id}",
            daemon=True,
        )
        self.process.start()
        self._exit_reported = False

    def submit(self, command: str, **payload: Any) -> None:
        self.start()
        self.current_command = command
        self._busy = True
        self.cancel_event.clear()
        self._notify("running", "运行中", command)
        self.commands.put((command, {"headless": self.headless, **payload}))

    def cancel(self) -> None:
        if self._busy:
            self.cancel_event.set()
            self._notify("cancelling", "取消中", self.current_command or "")

    def cancel_search_task(self) -> None:
        self.cancel()

    def is_running(self) -> bool:
        return self._busy

    def poll_events(self, limit: int = 300) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while len(events) < limit:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event.get("type") == "command_done":
                self._busy = False
                self.current_command = None
            events.append(event)

        if self.process is not None and not self.process.is_alive() and not self._closing and not self._exit_reported:
            self._exit_reported = True
            self._busy = False
            exit_code = self.process.exitcode
            events.append({
                "type": "plugin_process_error",
                "plugin_id": self.plugin_id,
                "message": f"插件进程已退出（exit code={exit_code}）",
            })
            self._notify("error", "异常", f"进程退出: {exit_code}")
        return events

    def close(self, timeout: float = 2.0) -> None:
        if self.process is None:
            return
        self._closing = True
        if self.process.is_alive():
            try:
                self.commands.put(("shutdown", {}))
                self.process.join(timeout)
            except Exception:
                pass
        if self.process.is_alive():
            try:
                self.process.terminate()
                self.process.join(1.0)
            except Exception:
                pass
        self._busy = False
        self.current_command = None


class WorkbenchPlugin:
    """Workbench plugin base with an isolated browser process."""

    def __init__(self, manifest: dict[str, Any], app: Any) -> None:
        self.manifest = manifest
        self.app = app
        plugin_id = str(manifest.get("id") or manifest.get("_folder") or "plugin")
        self.runtime_dir = RUNTIME_DIR / "plugins" / plugin_id
        self.profile_dir = PROFILE_DIR / "plugins" / plugin_id
        self.download_dir = DOWNLOAD_DIR / plugin_id
        self.state_dir = self.runtime_dir / "state_snapshots"
        self.rewards_data_file = self.runtime_dir / "rewards_data.json"
        self.rewards_account_file = self.runtime_dir / "rewards_account.json"
        self.search_task_log = self.runtime_dir / "search_task.jsonl"
        self.news_cache_file = self.runtime_dir / "news_cache.json"
        self.test_click_screenshot = self.runtime_dir / "test_click.png"
        for path in (self.runtime_dir, self.profile_dir, self.download_dir, self.state_dir):
            path.mkdir(parents=True, exist_ok=True)
        if plugin_id == "microsoft_points_assistant":
            for source, target in (
                (REWARDS_DATA_FILE, self.rewards_data_file),
                (REWARDS_ACCOUNT_FILE, self.rewards_account_file),
            ):
                if source.exists() and not target.exists():
                    try:
                        shutil.copy2(source, target)
                    except OSError:
                        pass
        folder = str(manifest.get("_folder") or plugin_id)
        worker_entry_name = str(manifest.get("worker_entry") or "")
        worker_entry = PLUGINS_DIR / folder / worker_entry_name if worker_entry_name else None
        self.runtime = PluginProcessRuntime(
            plugin_id,
            profile_dir=self.profile_dir,
            download_dir=self.download_dir,
            runtime_dir=self.runtime_dir,
            worker_entry=worker_entry,
            status_callback=getattr(app, "_set_plugin_status", None),
        )
        self.worker = self.runtime
        self._active = True

    @property
    def id(self) -> str:
        return str(self.manifest.get("id") or "")

    @property
    def display_name(self) -> str:
        return str(self.manifest.get("name") or self.id)

    def submit(self, command: str, **payload: Any) -> None:
        self.runtime.submit(command, **payload)

    def is_running(self) -> bool:
        return self.runtime.is_running()

    def on_unload(self) -> None:
        """Stop this plugin's isolated process before unloading."""
        self._active = False
        self.runtime.close()

    def build_pages(self, parent: Any) -> list[tuple[str, str, Any]]:
        """Return [(page_key, sidebar_label, frame)]."""
        return []

    def handle_event(self, _event: dict[str, Any]) -> None:
        """Handle events emitted by this plugin's isolated worker process."""

    def log(self, message: str, level: str = "info") -> None:
        self.app._append_log(message, level)


class PluginManager:
    """扫描 plugins/ 目录，按 manifest 加载插件，并把插件页面注册进侧栏导航。

    新发现的插件默认启用（安装即显示）；在 runtime/plugins.json 的
    {"disabled": [id...]} 中登记的插件会被跳过。单个插件失败只写日志，不影响宿主启动。
    """

    # 分发给插件的事件类型：与功能插件相关的 worker 事件流
    PLUGIN_EVENT_TYPES = {
        "rewards_data", "rewards_account", "account_info_status", "search_task_status",
        "auto_status", "status", "command_done", "plugin_process_error",
        "chaoxing_page", "chaoxing_run_status", "chaoxing_grade_status", "chaoxing_grade_done",
        "chaoxing_profile_status", "chaoxing_profile_data", "chaoxing_courses_data",
        "chaoxing_course_detail_status", "chaoxing_course_detail_data",
    }

    def __init__(self, app: Any) -> None:
        self.app = app
        self.manifests: list[dict[str, Any]] = []
        self.plugins: list[WorkbenchPlugin] = []
        self.disabled_ids: set[str] = set()
        self.errors: dict[str, str] = {}

    def discover(self) -> None:
        """读取启用设置并扫描插件清单；只登记，不导入。"""
        self.manifests = []
        self.disabled_ids = set()
        try:
            payload = json.loads(PLUGIN_SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self.disabled_ids = {str(item) for item in payload.get("disabled", [])}
        except (OSError, ValueError):
            pass
        if not PLUGINS_DIR.is_dir():
            return
        for manifest_path in sorted(PLUGINS_DIR.glob("*/manifest.json")):
            folder = manifest_path.parent.name
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.errors[folder] = f"manifest.json 解析失败：{exc}"
                continue
            if not isinstance(manifest, dict):
                self.errors[folder] = "manifest.json 内容不是对象"
                continue
            manifest.setdefault("id", folder)
            manifest["_folder"] = folder
            self.manifests.append(manifest)

    def load_plugins(self) -> None:
        """导入每个已启用插件的入口模块并实例化；失败记录到 errors 供管理页展示。"""
        self.plugins.clear()
        for manifest in self.manifests:
            plugin = self.load_one(manifest)
            if plugin is not None:
                self.plugins.append(plugin)

    def load_one(self, manifest: dict[str, Any]) -> WorkbenchPlugin | None:
        """导入并实例化单个插件；失败记录 errors 并返回 None。

        每次都用新的模块对象执行入口文件（不进 sys.modules 缓存），
        因此热更新时「关闭再打开开关」就会重新读取最新插件代码。
        """
        plugin_id = str(manifest.get("id"))
        if plugin_id in self.disabled_ids:
            return None
        try:
            folder = str(manifest.get("_folder") or plugin_id)
            entry = PLUGINS_DIR / folder / str(manifest.get("entry") or "plugin.py")
            spec = importlib.util.spec_from_file_location(f"workbench_plugin_{plugin_id}", entry)
            if spec is None or spec.loader is None:
                raise ImportError(f"无法创建导入规格：{entry}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            cls = getattr(module, str(manifest.get("plugin_class") or ""), None)
            if cls is None:
                raise AttributeError(f"入口模块缺少插件类 {manifest.get('plugin_class')}")
            self.errors.pop(plugin_id, None)
            return cls(manifest, self.app)
        except Exception as exc:
            self.errors[plugin_id] = str(exc)
            self.app._append_log(f"插件加载失败 {manifest.get('name') or plugin_id}: {exc}", "warning")
            return None

    def drop(self, plugin: WorkbenchPlugin) -> None:
        """移除已加载的插件实例（热卸载时调用）。"""
        if plugin in self.plugins:
            self.plugins.remove(plugin)
        self.errors.pop(plugin.id, None)

    def is_enabled(self, plugin_id: str) -> bool:
        return plugin_id not in self.disabled_ids

    def is_loaded(self, plugin_id: str) -> bool:
        return any(plugin.id == plugin_id for plugin in self.plugins)

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        """写入启用设置（立即持久化，重启程序后生效）。"""
        if enabled:
            self.disabled_ids.discard(plugin_id)
        else:
            self.disabled_ids.add(plugin_id)
        try:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            PLUGIN_SETTINGS_FILE.write_text(
                json.dumps({"disabled": sorted(self.disabled_ids)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            self.app._append_log(f"无法写入插件设置：{exc}", "warning")

    def poll_events(self) -> list[dict[str, Any]]:
        """Drain events from every isolated plugin process."""
        events: list[dict[str, Any]] = []
        for plugin in list(self.plugins):
            try:
                events.extend(plugin.runtime.poll_events())
            except Exception as exc:
                events.append({
                    "type": "plugin_process_error",
                    "plugin_id": plugin.id,
                    "message": f"读取插件进程事件失败: {exc}",
                })
        return events

    def shutdown_all(self) -> None:
        for plugin in list(self.plugins):
            try:
                plugin.runtime.close()
            except Exception:
                pass

    def dispatch(self, event: dict[str, Any], plugin_id: str | None = None) -> None:
        target_id = plugin_id or event.get("plugin_id")
        for plugin in self.plugins:
            if target_id and plugin.id != target_id:
                continue
            try:
                plugin.handle_event(event)
            except Exception as exc:
                self.app._append_log(f"插件事件处理异常 {plugin.id}: {exc}", "warning")

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
        self.pages["logs"] = self._build_logs_page()
        self.pages["plugins"] = self._build_plugins_page()
        nav_items += [("overview", "总览"), ("logs", "运行日志"), ("plugins", "插件管理")]

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
    _CORE_PAGE_ORDER = ("overview", "logs", "plugins")

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

        if self.worker.context is not None:
            self._append_log("浏览器当前正在运行；切换需重启后生效，可点击「重启浏览器」立即应用。", "warning")

    def _toggle_headless(self) -> None:
        self._set_headless_mode(not self.worker.headless)

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


if __name__ == "__main__":
    raise SystemExit(main())
