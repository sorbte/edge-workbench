# 插件开发指南（按需加载）

写新插件或改现有插件前先读本文。硬性规则（前缀命名、事件登记等）见 AGENTS.md。

## 文件布局

```
plugins/<plugin_id>/
├─ manifest.json   # id / name / version / description / entry / plugin_class / worker_entry(可选)
├─ plugin.py       # UI 侧：跑在主进程，继承 WorkbenchPlugin，可以碰 Tk
└─ worker.py       # 自动化侧：跑在独立 spawn 子进程，实现 register(worker)（可选）
```

- `id` 同时用作进程名、资料目录名（edge_profile/plugins/<id>/、runtime/plugins/<id>/）、事件路由键，取稳定的小写下划线串。
- 有后台自动化的插件才加 `worker_entry`；纯展示型插件（只读缓存 JSON）可以省略。

## plugin.py（UI 侧）必须/可以做

继承 `WorkbenchPlugin`（workbench/plugin_runtime.py），基类已提供：

| 成员 | 说明 |
|---|---|
| `self.runtime` / `self.worker` | 同一个 PluginProcessRuntime：`submit(cmd, **payload)`、`cancel()`、`is_running()`、`current_command`、`headless` |
| `self.app` | 宿主：`_append_log`、`_icon_button`、`_card`、`_button`、`_bind_busy`、`_attach_tooltip`、`set_browser_window_visible` |
| `submit(cmd, **payload)` | 提交命令到本插件子进程（自动带 headless） |
| `log(msg, level)` | 写主日志面板 |
| `profile_dir` / `runtime_dir` / `download_dir` | 本插件专属目录（已自动创建） |

必须实现：
- `build_pages(parent) -> [(page_key, 侧栏标题, frame), ...]`：构建页面并返回注册表。
- `handle_event(event)`：处理本插件子进程 emit 的事件（带 plugin_id 自动路由进来）。

可选覆盖：
- `sync_browser_window_visibility(visible)`：与总览页共享的窗口显示设置同步（按钮黑/灰态）。
- `on_unload`：热卸载清理（基类默认已 close 进程，一般不用动）。

约定：窗口显示开关的 `toggle_browser_window` 必须走"浏览器运行中 → 自动重启"路径（参考 microsoft_points_assistant / chaoxing_assistant 的同名方法）：
```python
def toggle_browser_window(self) -> None:
    visible = not self._browser_window_visible
    self.app.set_browser_window_visible(visible)          # 同步全局共享设置
    if not self._browser_running or self.runtime.current_command == "restart":
        return                                            # 未运行→下次启动生效；重启中→忽略连点
    self.log("窗口显示模式已切换，正在自动重启独立浏览器…", "info")
    self.runtime.cancel()                                 # 有任务先取消（仅 busy 时生效）
    self.submit("restart")
```

## worker.py（子进程侧）

模块级 `register(worker)` 把命令处理函数挂进 `worker.command_handlers`：
```python
def register(worker: Any) -> None:
    MyWorkerLogic(worker).install()   # install() 内 self.worker.command_handlers.update({...})
```

- 命令名必须带插件前缀：`chaoxing_refresh`、`chaoxing_study`…（内置命令名保留，见 docs/commands-and-events.md）。
- 长任务：开头 `self.worker._cancel_event.clear()`；等待用 `self.worker._wait_with_cancel()`；`TaskCancelled` 就地捕获并 emit `*_status` cancelled 事件。
- 事件用 `worker.emit(type, **payload)`：自动带 plugin_id；新事件类型要登记进 `PluginManager.PLUGIN_EVENT_TYPES`（workbench/plugin_runtime.py）。
- 任务进度/状态用成对事件：`*_status`（state: starting/running/cancelled/done/error + message）+ `*_data`（数据）。
- 插件 worker 内不要 import 任何 Tk / plugin.py 内容；只依赖 `from edge_workbench import ...` 的宿主公共 API。

## UI 侧配套

- 触发命令的按钮全部 `self.app._bind_busy(command, button)` 注册，让主程序统一禁用/恢复。
- 所有 button.configure 包 try/except tk.TclError（页面可能被热卸载）。
- 运行状态标签词汇固定：未启动 / 启动中 / 运行中 / 就绪 / 等待登录 / 已完成 / 取消中 / 已取消 / 异常。
- 数据卡片读取本机缓存（runtime/plugins/<id>/*.json），页面打开时不自动启动浏览器。

## 上线前检查清单

1. manifest 字段齐全；id 与目录名一致。
2. `python -m py_compile plugins/<id>/*.py` 通过。
3. 命令名带前缀、不与内置冲突；新事件已登记 PLUGIN_EVENT_TYPES。
4. 开关按钮禁用/恢复配套（_bind_busy），cancel 路径可打断长任务。
5. README.md 增加了对应插件页说明条目。
