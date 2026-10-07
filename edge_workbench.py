"""Microsoft Edge 持久化浏览器工作台 · 启动入口。

实现按职责拆分在 workbench/ 包内：
  workbench.config          路径、常量、设置与工具函数
  workbench.spider_scripts  宠物（蜘蛛）页面注入脚本
  workbench.page_scripts    Rewards/账户等页面提取脚本
  workbench.worker          Playwright 浏览器工作线程
  workbench.plugin_runtime  插件进程与插件管理
  workbench.app             桌面 UI 与 main()

旧的启动命令 `py -3.12 edge_workbench.py --autostart` 保持不变；
插件沿用 `from edge_workbench import ...`（workbench 包已在 sys.modules
里把自身注册为该别名，主进程与插件子进程都适用）。
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

from workbench import (  # noqa: F401  宿主公共 API 再导出，兼容旧导入路径
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
    AIError,
    BrowserWorker,
    PluginManager,
    PluginProcessRuntime,
    TaskCancelled,
    WorkbenchPlugin,
    ai_chain,
    ai_manager,
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
    structured_available,
)
from workbench.app import main

if __name__ == "__main__":
    raise SystemExit(main())
