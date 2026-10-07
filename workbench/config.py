"""全局配置：路径、外观常量、任务参数与进程间共用的小工具函数。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


# 仓库根：edge_profile/ runtime/ plugins/ downloads/ 等数据目录的锚点。
# 拆分后 config.py 在 workbench/ 包内，必须回退一级才与旧版单文件行为一致；
# 若调整包层级，需同步更新这里（数据目录始终在仓库根）。
APP_DIR = Path(__file__).resolve().parent.parent
PROFILE_DIR = APP_DIR / "edge_profile"
RUNTIME_DIR = APP_DIR / "runtime"
PLUGINS_DIR = APP_DIR / "plugins"
PLUGIN_SETTINGS_FILE = RUNTIME_DIR / "plugins.json"
WORKBENCH_SETTINGS_FILE = RUNTIME_DIR / "workbench.json"
STATE_DIR = RUNTIME_DIR / "state_snapshots"
DOWNLOAD_DIR = APP_DIR / "downloads"
OPERATIONS_LOG = RUNTIME_DIR / "operations.jsonl"
AI_PROVIDERS_FILE = RUNTIME_DIR / "ai_providers.json"
AI_AGENTS_FILE = RUNTIME_DIR / "agents.json"
AI_SKILLS_FILE = RUNTIME_DIR / "ai_skills.json"
REWARDS_DATA_FILE = RUNTIME_DIR / "rewards_data.json"
REWARDS_ACCOUNT_FILE = RUNTIME_DIR / "rewards_account.json"
REWARDS_ABOUT_URL = "https://rewards.bing.com/about"
REWARDS_USERINFO_URL = "https://rewards.bing.com/api/getuserinfo?type=1"
ALLOWED_POINTS_SYNC_HOSTS = {"rewards.bing.com"}
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
ICON_VISUALIZE = "\uE7B3"   # 操作可视化（宠物联动特效）
ICON_GRADE = "\uE70F"       # 批改作业（铅笔）
ICON_SETTINGS = "\uE713"    # 设置（齿轮）

# ---------------------------------------------------------------- 宠物（操作可视化小生物）
# 宠物共用同一套「漫游 + 指向性采集」引擎，仅造型、配色与速度不同；
# 词采/联动 API 完全一致（window.__wbs），切换宠物即刻生效。
# 当前只上架蜘蛛；后续新增宠物时在 PETS 里加一项即可（脚本已支持
# body: spider/beetle/bee 造型与任意主色，无需再改注入逻辑）。
PETS: tuple[dict[str, Any], ...] = (
    {
        "id": "spider",
        "name": "蜘蛛",
        "glyph": "\U0001F577",
        "body": "spider",
        "accent": "#54d7e8",
        "accent_dim": "#155e6b",
        "body_fill": "#0a0a12",
        "body_edge": "#f2f2fa",
        "speed": 1.0,
        "description": "八足漫游者：分级 IK 步态，路过绝不抓取，指向性采集的元老。",
    },
)
DEFAULT_PET_ID = "spider"


def load_active_pet() -> dict[str, Any]:
    """读取当前选择的宠物配置（设置缺省/无效时回落到蜘蛛）。"""
    pet_id = str(load_workbench_settings().get("spider_pet") or DEFAULT_PET_ID)
    return next((pet for pet in PETS if pet["id"] == pet_id), PETS[0])

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


def load_workbench_settings() -> dict[str, Any]:
    """读取工作台自身设置（与插件设置 plugins.json 分开存放）。"""
    try:
        payload = json.loads(WORKBENCH_SETTINGS_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def save_workbench_settings(settings: dict[str, Any]) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        WORKBENCH_SETTINGS_FILE.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


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


