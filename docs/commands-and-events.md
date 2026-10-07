# 命令与事件参考（按需加载）

主 worker 与插件 worker 共用同一套 BrowserWorker（workbench/worker.py）命令循环。改命令/事件时先读本文，改完同步更新本文件与 README。

## 内置命令（worker.run() 分发，禁止插件覆盖）

| 命令 | 载荷 | 行为 |
|---|---|---|
| start | headless | 启动持久化 Edge（已运行则跳过并提示）；emit status starting→running |
| navigate | url | 当前页跳转（自动补 https、搜索词走必应） |
| bing_search | query | 打开必应并输入搜索 |
| new_tab | url | 新建标签页 |
| simulate | — | 模拟真实浏览（随机滚动/移动） |
| rewards_data | — | 拉取 Rewards 全量数据（earn + dashboard + 账户信息） |
| account_info | — | 同 rewards_data（账户信息口径） |
| test_click | — | 测试点击 + 截图 runtime/test_click.png |
| search_task | — | 搜索积分任务（计划→逐条搜索→校准→冷却），可取消 |
| auto_run | — | 一键流程：rewards_data → search_task → trigger_tasks → 最终同步 |
| trigger_tasks | — | 触发未完成每日任务 |
| stats | — | emit stats（缓存体积、Cookie、页数等指标） |
| snapshot | — | 保存状态快照 → snapshot_saved |
| stop | — | 停止浏览器（emit status stopped、page_count 0） |
| restart | — | cancel_search_task → stop → start（按当前 headless） |
| clean_cache | — | 停浏览器后按 SAFE_CACHE_RELATIVE_PATHS 白名单清理并对比体积 |
| spider_overlay | enabled | 开关宠物注入（对已开页面即时生效） |
| spider_pet | pet | 切换宠物并重注入 |
| pet_play | action | 宠物互动（召唤/庆祝/撒粒子） |
| shutdown | — | 子进程/程序退出前停止浏览器并结束循环 |

规则：
- 命令串行执行；payload 含 `headless` 键时先更新 self.headless 再执行（窗口模式经此传递）。
- 新内置命令必须：run() 加分支 → app._bind_busy 注册按钮 → command_done 自动恢复；涉及任务的加进 app._TASK_COMMANDS。

## 插件命令

- 命名必须 `<plugin_prefix>_<action>`（如 chaoxing_refresh / chaoxing_study / chaoxing_ai_menu），经 `worker.command_handlers` 注册。
- 学习通助手现有命令：`chaoxing_start`（仅启动）/ `chaoxing_refresh` / `chaoxing_open_space` / `chaoxing_open_course` / `chaoxing_study` / `chaoxing_ai_menu`（页面批改菜单开关：worker 记住开关状态；开启期间由宿主空闲轮询 `worker.idle_pollers` 检测作业 / 章节测验类页面并注入可拖动的「AI 批改助手」菜单，离开测验页自动移除，处理菜单命令——开始批改 / 停止 / 重新批改——批改在题目下方逐题批注 AI 参考答案与解释，不自动填写、不提交，不占用任务闸门，Agent 由 worker 依据本机 `ai_settings.json` 经 ai_manager 解析；载荷仅含 enabled）。
- 插件进程的 `PluginProcessRuntime.submit` 自动注入 `headless`；仅空闲时 clear 取消标志（保证"先取消再提交重启"能打断任务）。

## 事件类型

宿主消费（app._handle_event，plugin_id 为空时走 UI 分支）：

| 事件 | 载荷要点 | UI 行为 |
|---|---|---|
| status | state: starting/running/stopped + detail | 状态灯与运行详情 |
| command_done | command | 恢复忙按钮、清 busy 文案 |
| stats | data（profile_size/cache_size/cookie_count/page_count…） | 指标卡 |
| url | url | 地址栏回显 |
| page_count | count | 活动标签页指标 |
| snapshot_saved | path | 日志提示 |
| log | level + message | 日志面板 |
| focus_ui | — | 置顶一次便于提醒 |
| search_task_status | state/plan/current/total/gained/cooldown_remaining/flow | 搜索任务进度卡 |
| auto_status | state: started/running/done/cancelled/error + message | 一键自动化状态与按钮联动 |
| account_info_status | state + message | 账户信息状态行 |

插件消费（经 plugin_manager.dispatch，需登记 PluginManager.PLUGIN_EVENT_TYPES）：
- rewards_data / rewards_account：微软积分数据。
- account_info_status：账户信息状态。
- chaoxing_profile_status / chaoxing_profile_data / chaoxing_courses_data / chaoxing_run_status / chaoxing_grade_status / chaoxing_grade_done / chaoxing_page / chaoxing_course_detail_status / chaoxing_course_detail_data。
- chaoxing_run_status 的 mode 字段区分流程：study=章节学习（其余为同步/打开类）。
- chaoxing_grade_status：页面批改菜单的批改进度（state: running/done/error + message），只更新插件页头部详情，不改变任务状态。
- plugin_process_error：插件子进程崩溃（含 traceback），UI 显示异常态。

事件通用规则：worker.emit 自动补 plugin_id=namespace；新增事件必须三处同步——emit 调用、PLUGIN_EVENT_TYPES、消费端 handle_event。

## 取消与重启语义

- `cancel_search_task()`（worker）= set 取消事件 + emit search_task_status cancelling；`PluginProcessRuntime.cancel()` 仅在 busy 时 set。
- 长任务开头 clear 取消事件（清残留），等待点用 `_wait_with_cancel` / `_check_cancel`；TaskCancelled 就地捕获输出"已取消"事件。
- "restart" = 取消任务 → stop → start；窗口显示切换在浏览器运行中自动触发它（总览页与插件页均已实现，别改回手动提示）。

## 排障

1. UI 日志面板（_append_log，带时间戳）。
2. runtime/operations.jsonl：browser_start/browser_stop/命令审计。
3. 插件子进程静默消失 → poll_events 产出 plugin_process_error（含 exit code 与 traceback）。
4. 注入 JS 语法问题 → `python tools/js_syntax_check.py`；蜘蛛行为调试 → tools/spider_*_probe.py 与 docs/spider-api.md。
5. 批改菜单没出现在页面上 → 先看运行日志里「已在标签页注入…菜单 / 注入失败」几行；再用 `.venv/Scripts/python.exe tools/ai_menu_debug.py [课程名]` 无头实测菜单注入与题目提取（截图存 runtime/plugins/chaoxing_assistant/debug_shots/）。
