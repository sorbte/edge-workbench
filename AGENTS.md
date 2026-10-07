# AGENTS.md — edge-workbench 代理协作规则

Microsoft Edge 持久化浏览器工作台：CustomTkinter 桌面 UI + Playwright 驱动本机 Edge（持久化资料目录）+ 插件化自动化（微软积分助手、学习通助手）。Windows 专用，Python 3.12。

本文件只保留硬性规则；模块细节、流程图、命令/事件全集与示例**按需加载**，不要凭记忆猜测实现：

| 任务场景 | 动手前先读 |
|---|---|
| 新写/修改插件 | [docs/plugin-development.md](docs/plugin-development.md) |
| 改 worker 命令、事件流、排障 | [docs/commands-and-events.md](docs/commands-and-events.md) |
| 改 UI 结构、忙状态机制、宠物注入、整体分层 | [docs/architecture.md](docs/architecture.md) |
| 蜘蛛等宠物页面注入 API | [docs/spider-api.md](docs/spider-api.md) |
| 给插件接入 AI（Agent 统一调用）、改 Agent 管理页 / 提示词模板、AI 批改流程 | [docs/ai-api.md](docs/ai-api.md) |

用户视角的功能说明在 README.md：**任何用户可见行为变化必须同步更新 README 对应条目**。

## 运行与验证

- 启动：`run.bat`（自动建 .venv 并执行 `edge_workbench.py --autostart`）。依赖锁定 `playwright==1.63.0`、`customtkinter==6.0.0`，不得擅自升级。
- 每次改动后必须通过：`python -m py_compile <所有改动的 .py>`；改动注入页面的 JS 另跑 `python tools/js_syntax_check.py`。
- 本项目无测试套件、无 CI：py_compile + 对照 docs 走查受影响流程是最低门槛；不得声称"测试通过"。

## 架构边界（禁止跨越）

1. 三层结构：`workbench/app.py`（Tk 主线程 UI）→ `workbench/worker.py` 的 `BrowserWorker`（主进程后台线程，**Playwright 唯一入口**）→ `plugins/<id>/worker.py`（每插件独立 spawn 子进程，内含各自的 BrowserWorker 线程）。
2. UI 线程禁止执行任何 Playwright/浏览器操作；只允许 `worker.submit(cmd, **payload)`、`worker.cancel_search_task()`，以及读取轻量状态字段（`context` / `current_command` / `headless`）。结果一律经事件回 UI。
3. worker 命令**串行**执行（run() 循环一次一条）。新命令不能"插队"打断任务；打断 = 先 cancel 再提交，等待事件，不要轮询。
4. 插件 UI（plugin.py，主进程）与插件 worker（worker.py，子进程）只能通过 `runtime.submit()` / `emit()` 通信；禁止跨进程 import、禁止直接调用对方方法、子进程内禁止 import 任何 Tk。
5. `edge_workbench.py` 只是启动入口 + `from workbench import *` 兼容别名（插件沿用旧导入路径）；新实现一律写进 workbench/ 包对应模块，禁止把逻辑堆回入口文件。
6. 路径常量（PROFILE_DIR / RUNTIME_DIR / PLUGINS_DIR / DOWNLOAD_DIR / OPERATIONS_LOG 等）一律从 `workbench.config` 导入；数据目录锚定仓库根，调整包层级必须同步 config.py 的 APP_DIR 注释约定。

## 长任务与取消

7. 可耗时的任务方法：开头必须 `self._cancel_event.clear()`（清上次残留标志）；等待用 `self._wait_with_cancel()`，循环内用 `self._check_cancel()`。
8. `TaskCancelled` 必须在任务内捕获：写"已取消"日志并 emit 对应 `*_status` 的 cancelled 事件；禁止让它落到 run() 的兜底 except 变成错误栈。
9. 任务运行中提交的命令不得清除取消标志：`PluginProcessRuntime.submit` 仅在**空闲时** `cancel_event.clear()`——这是刻意设计（保证"先取消、再提交重启"能打断任务），不要"简化"回无条件 clear。
10. `"restart"` 命令语义 = 取消任务 → stop → 按当前 headless 启动。总览页与插件页的窗口显示切换在浏览器运行中**自动触发重启**（app._toggle_headless、各插件 toggle_browser_window）；浏览器未运行时仅记录设置。不要改回"提示用户手动重启"。

## UI 忙状态

11. 触发 worker 命令的按钮必须 `app._bind_busy(command, button)` 注册：提交时 `_disable_for` 统一禁用，`command_done` 事件统一恢复；禁止手工 state 切换不配套。
12. 插件页按钮可能被热卸载销毁：UI 回调里所有 `button.configure` 必须 try/except tk.TclError。
13. 任务运行集合 = `app._TASK_COMMANDS`。破坏性操作（清理缓存等）在任务运行时必须拒绝并写日志（参考 clean_browser_cache），禁止静默执行。

## 窗口模式（headless）同步

14. 共享开关唯一入口：`app.set_browser_window_visible(visible)` → `_set_headless_mode(not visible)`，一次同步三处——主 `worker.headless`、所有 `plugin.runtime.headless`、各插件 `sync_browser_window_visibility()`（按钮黑/灰态）。插件侧不得发明第二份窗口设置。
15. 插件重启只准 `runtime.submit("restart")`：submit 自动注入 headless，worker 在 run() 顶部从 payload 更新后执行。

## 数据目录安全

16. 清缓存只允许删除 `config.SAFE_CACHE_RELATIVE_PATHS` 白名单内路径，且必须走 `_safe_remove_inside_profile`（含防符号链接/防逃逸校验）；禁止新增删除 profile 其它内容的代码路径。
17. 浏览器启停等关键操作必须 `_append_operation` 写入 `runtime/operations.jsonl` 审计；排障先看该文件与 UI 日志。
18. 主浏览器资料 `edge_profile/` 与插件资料 `edge_profile/plugins/<id>/` 严格隔离；插件不得读写彼此或主目录。

## 插件开发

19. manifest.json 必含 `id` / `name` / `entry` / `plugin_class`；有后台自动化的插件加 `worker_entry`，worker 模块实现 `register(worker)` 往 `worker.command_handlers` 注册命令。细节与骨架见 docs/plugin-development.md。
20. 插件命令必须带插件前缀（如 `chaoxing_refresh`），禁止覆盖内置命令（全集见 docs/commands-and-events.md）；新事件类型必须登记进 `PluginManager.PLUGIN_EVENT_TYPES`，且 emit → 登记 → handle_event 三处同步。
21. 插件运行状态词汇固定：未启动 / 启动中 / 运行中 / 就绪 / 等待登录 / 已完成 / 取消中 / 已取消 / 异常。

## 风格

22. 用户可见文案、日志、注释、commit message 一律中文；标识符用英文；文案引号用「」。
23. 保留 `from __future__ import annotations`；沿用现有类型注解与命名风格（私有方法下划线前缀；控件统一用 `app._icon_button` / `_button` / `_card` 构建）。
24. 颜色只用 `COLORS`、图标只用 `ICON_*` 常量；现有硬编码配对（如按钮激活态 `COLORS["accent"]`/`COLORS["accent_hover"]`，未激活 `#f2f2f0`/`#e7e7e4`）保持原样，不新增散落色值。

## 提交

25. 仅在用户明确要求时 commit / push。commit message 用中文一句话概括行为变化（参考 `git log` 现有风格，如「完善插件隔离并新增学习通助手」）。
