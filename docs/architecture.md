# 架构与运行模型（按需加载）

面向代理的模块地图与机制说明。硬性规则见根目录 AGENTS.md，本文件解释"为什么"与"怎么运作"。

## 分层与线程/进程模型

```
主进程
├─ workbench/app.py        WorkbenchApp(ctk.CTk)  ← 唯一的 Tk 主线程
│   ├─ after(120ms) _poll_events(): 轮询 worker 事件队列 + 各插件进程事件队列
│   ├─ self.worker = BrowserWorker(self.events); worker.start()   ← 后台线程
│   └─ PluginManager: 扫描 plugins/*/manifest.json，实例化插件 UI（主进程内）
│        └─ 每插件 WorkbenchPlugin.runtime = PluginProcessRuntime
│             └─ spawn 子进程 "plugin-<id>"
│                  ├─ workbench/plugin_runtime.py::_plugin_process_main
│                  │    创建子进程自己的 BrowserWorker（带 commands/events 队列 + cancel_event）
│                  │    再 exec 插件的 worker_entry，register(worker) 注册扩展命令
│                  └─ worker.run() 命令循环（与主 worker 同一套代码）
└─ edge_workbench.py       启动入口：只做 `from workbench import *` 兼容别名 + main()
```

关键点：
- Playwright 只在 BrowserWorker 线程里触碰（主进程 1 个 + 每插件子进程各 1 个）。
- UI 与 worker 之间只有两条通道：`submit(command, **payload)` 下行、事件队列上行。
- 插件 UI 代码跑在主进程（可以碰 Tk），插件 worker 代码跑在子进程（绝对不能碰 Tk）。

## 模块职责

| 模块 | 职责 |
|---|---|
| workbench/config.py | 全部路径常量（锚定仓库根）、COLORS、任务参数、SAFE_CACHE_RELATIVE_PATHS 白名单、小工具函数 |
| workbench/worker.py | BrowserWorker：Playwright 持久化 Edge（channel="msedge"、no_viewport、防自动化参数）、命令循环、搜索/积分/宠物任务 |
| workbench/page_scripts.py | Rewards/账户页面的 JS 提取脚本 |
| workbench/spider_scripts.py | 宠物（蜘蛛等）页面注入脚本，API 见 docs/spider-api.md |
| workbench/ai_manager.py | Agent（runtime/agents.json：模型接入 + 系统提示词 + 内置工具 + 参数）、模型接入（runtime/ai_providers.json）、提示词模板（内置 + runtime/ai_skills.json）与统一调用口 run_agent / run_agent_json（含工具循环、SSRF 边界校验），主进程与插件子进程共用，详见 docs/ai-api.md |
| workbench/plugin_runtime.py | _plugin_process_main、PluginProcessRuntime（进程代理）、WorkbenchPlugin 基类、PluginManager |
| workbench/app.py | 全部 UI：总览页、宠物页、Agent 管理页、插件页装配、事件分发、忙状态注册表 |

## 命令生命周期

1. UI 回调 → `app._disable_for(command)`（按注册表禁用按钮）→ `worker.submit(cmd, payload)`。
2. worker 线程 run() 循环取命令；payload 若含 `headless` 键则先更新 `self.headless`（窗口模式经此生效）。
3. 内置命令在 run() 里 if/elif 分发；未命中走 `command_handlers`（插件注册表）。
4. 任务方法内部：`_cancel_event.clear()` → 业务 → TaskCancelled 就地捕获。
5. 无论成败 finally 清 current_command，然后 `emit("command_done", command=cmd)`。
6. UI 收到 command_done：恢复该 command 注册的所有按钮 → `_set_busy("")`。

## 事件流

- worker `emit(type, **payload)` → 队列；BrowserWorker.emit 会自动补 `plugin_id=namespace`（插件进程内）。
- app._poll_events 每 120ms 排干主 worker 队列 + PluginManager.poll_events()（各插件进程队列）。
- app._handle_event 按 event_type 分发；插件相关事件经 `plugin_manager.dispatch(event, plugin_id)` 路由到对应插件 handle_event。
- 事件类型全集见 docs/commands-and-events.md。

## 忙状态注册表

- `_bind_busy(command, *buttons)`：登记"提交该命令时要禁用的按钮"，可多次追加（主页面 + 各插件页共用）。
- `_disable_for(command)`：提交命令时统一禁用；command_done 统一恢复。
- `_purge_dead_buttons()`：热卸载插件后清理已销毁按钮，防 TclError。
- 插件热装卸：开关切换 → set_enabled 写 runtime/plugins.json → load_one 重新 exec 入口模块（不进 sys.modules，即改即生效）→ drop 卸载旧实例（runtime.close() 先 shutdown 后 terminate）。

## 窗口模式（headless）与自动重启

- 共享设置唯一入口：`app.set_browser_window_visible(visible)` → `_set_headless_mode(not visible)`。
- `_set_headless_mode` 同步三处：主 worker.headless、所有 plugin.runtime.headless、所有插件的 `sync_browser_window_visibility()`（按钮黑/灰态）。
- 浏览器运行中切换 → 自动重启立即生效：
  - 总览页：`_toggle_headless` → `restart_browser()`（submit "restart"）。
  - 插件页：`toggle_browser_window` → `runtime.cancel()`（若有任务）→ `submit("restart")`。
  - worker 的 restart_browser = cancel_search_task → stop → start（start 读 self.headless 决定无头参数）。
- 浏览器未运行时仅记录设置，下次 start 生效。

## 数据目录

| 目录 | 内容 |
|---|---|
| edge_profile/ | 主浏览器持久化资料（Cookies、缓存、历史），插件不得写入 |
| edge_profile/plugins/<id>/ | 各插件独立 Edge 资料，彼此隔离 |
| runtime/ | workbench.json、plugins.json、ai_providers.json（多平台 AI 配置，含 API Key，勿外传）、operations.jsonl（审计日志）、state_snapshots/、各插件 runtime/plugins/<id>/ |
| downloads/ | 浏览器下载（每插件一个子目录） |

排障顺序：UI 日志面板 → runtime/operations.jsonl → 插件子进程崩溃看 plugin_process_error 事件（含 traceback）。
