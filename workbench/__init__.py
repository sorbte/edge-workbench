"""Edge 持久化浏览器工作台（workbench 包）。

插件通过 `from edge_workbench import ...` 复用宿主常量与基类；
这里注册模块别名并再导出公共 API，保证旧导入路径继续可用。
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True
# 插件与插件子进程以旧模块名导入宿主：把本包注册为 edge_workbench 别名，
# 旧导入路径无需任何改动（主进程与 mp spawn 子进程都会经过这里）。
sys.modules.setdefault("edge_workbench", sys.modules[__name__])

from . import ai_manager as ai_manager  # noqa: E402
from .ai_manager import AIError, AI_PLATFORM_PRESETS  # noqa: E402
from . import ai_chain as ai_chain  # noqa: E402
from .ai_chain import structured_available  # noqa: E402
from .config import (  # noqa: E402
    APP_DIR,
    COLORS,
    DEFAULT_PET_ID,
    DOWNLOAD_DIR,
    FONT_MONO,
    FONT_SANS,
    HOME_URL,
    ICON_CLEAN,
    ICON_FOLDER,
    ICON_GRADE,
    ICON_NEW_TAB,
    ICON_OPEN_FILE,
    ICON_PLAY,
    ICON_REFRESH,
    ICON_RESTART,
    ICON_SETTINGS,
    ICON_SNAPSHOT,
    ICON_STOP,
    ICON_TEST_CLICK,
    ICON_VISUALIZE,
    ICON_WINDOW,
    PLUGINS_DIR,
    PROFILE_DIR,
    PETS,
    RUNTIME_DIR,
    UI_TEXT_MIN_SIZE,
    ensure_directories,
    find_edge_executable,
    format_bytes,
    load_active_pet,
    load_workbench_settings,
    mono_font,
    normalize_url,
    now_text,
    sans_font,
    save_workbench_settings,
)
from .plugin_runtime import PluginManager, PluginProcessRuntime, WorkbenchPlugin  # noqa: E402
from .worker import BrowserWorker, TaskCancelled  # noqa: E402

__all__ = [name for name in dir() if not name.startswith("_")]
