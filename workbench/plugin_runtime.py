"""插件体系：插件子进程入口、进程代理、插件基类与插件管理器。"""
from __future__ import annotations

import importlib.util
import json
import multiprocessing as mp
import queue
import shutil
import traceback
from pathlib import Path
from typing import Any

from .config import (
    DOWNLOAD_DIR,
    PLUGINS_DIR,
    PLUGIN_SETTINGS_FILE,
    PROFILE_DIR,
    REWARDS_ACCOUNT_FILE,
    REWARDS_DATA_FILE,
    RUNTIME_DIR,
)
from .worker import BrowserWorker


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
        busy = self._busy
        self.current_command = command
        self._busy = True
        if not busy:
            # 任务运行中提交的命令（如自动重启）不能清掉刚设置的取消标志，
            # 否则正在运行的任务感知不到取消，重启会一直排队等待。
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
