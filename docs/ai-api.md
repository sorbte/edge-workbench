# Agent 能力（Agent 管理 / 模型接入 / 统一调用）

面向两类读者：使用者（Agent 管理页、学习通 AI 批改）与插件开发者（在自己的插件里调用 Agent）。
硬性规则见根目录 AGENTS.md；本文件解释能力边界、数据文件与接入方式。

## 组成

| 部分 | 位置 | 说明 |
|---|---|---|
| Agent（智能体） | `runtime/agents.json` | 独立 Agent：绑定模型接入 + 系统提示词 + 工具 + 温度/超时；UI 在「Agent 管理」页上半区 |
| 模型接入（providers） | `runtime/ai_providers.json` | OpenAI 兼容接口与 API Key，Agent 通过 `provider_id` 引用；UI 在「Agent 管理」页「MODELS / 模型接入」区 |
| 提示词模板（技能） | 代码内置（`builtin_*`）+ `runtime/ai_skills.json` | 创建 Agent 时一键载入的提示词模板；UI 在「Agent 管理」页「TEMPLATES / 提示词模板」区 |
| 统一调用口 | `workbench/ai_manager.py` | `run_agent` / `run_agent_json`：自动解析 Agent → 组装消息 → 内置工具循环 → 返回文本/JSON；`json_mode=True` 启用接口级 JSON 模式 |
| 结构化输出（可选） | `workbench/ai_chain.py` | LangChain `with_structured_output`（Pydantic schema）强制固定格式；依赖 `langchain-openai`（requirements.txt 已锁定），未安装自动降级 |
| 消费方 | `plugins/chaoxing_assistant/` | 「批改作业」页面菜单：在题目下方逐题批注 AI 参考答案与解释（不自动填写、不提交） |

## 用户视角速查

- **Agent 管理页**（侧栏「Agent 管理」）：
  - 「AGENTS / 智能体」：新增 / 编辑 / 删除 / 测试 / 设为默认。Agent = 模型接入 + 系统提示词 + 工具勾选 + 回答风格（温度 0.1/0.4/0.8）；系统提示词可从「提示词模板」一键载入。
  - 「MODELS / 模型接入」：OpenAI 兼容接口与密钥统一管理（平台预设推荐 DeepSeek，另支持 OpenAI、Kimi、智谱 GLM、通义千问与自定义网关），第一条自动成为默认接入。
  - 「TEMPLATES / 提示词模板」：内置「通用作业批改 / 阅读理解材料精答 / 计算题精确作答 / 简答题要点化」（只读）+ 自定义模板（增删改，内容必须保留 JSON 输出契约）。
- **学习通助手页**：只有一枚「批改 Agent」下拉（选择即保存），模型 / 密钥 / 提示词配置全部收在 Agent 管理页。
- **工具**：Agent 可勾选宿主内置工具（`get_current_time` 当前时间、`calculate` 安全四则运算）；调用时走 OpenAI function calling，最多 `MAX_TOOL_ROUNDS=3` 轮，超限后强制产出最终回答。

## 数据文件

### runtime/agents.json

```json
{
  "agents": [
    {
      "id": "agent_xxxxxxxxxx",
      "name": "作业批改助手",
      "description": "批改学习通未提交作业",
      "provider_id": "ai_xxxxxxxxxx",
      "system_prompt": "你是学习通作业批改助手……（必须保留 JSON 输出契约）",
      "tools": ["get_current_time", "calculate"],
      "temperature": 0.1,
      "timeout": 0,
      "created_at": "…",
      "updated_at": "…"
    }
  ],
  "default_id": "agent_xxxxxxxxxx"
}
```

### runtime/ai_providers.json（模型接入，结构不变）

```json
{
  "providers": [
    {"id": "ai_xxxxxxxxxx", "name": "DeepSeek 官方", "platform": "deepseek",
     "base_url": "https://api.deepseek.com", "api_key": "sk-…（仅保存在本机，勿分享）",
     "model": "deepseek-chat", "created_at": "…", "updated_at": "…"}
  ],
  "default_id": "ai_xxxxxxxxxx"
}
```

## 插件接入 API（对外统一调用口，主进程与插件子进程均可用）

```python
from edge_workbench import ai_manager, AIError

# 一行调用：自动解析 Agent（id / agents.json 的 dict / 含 provider 的快照 dict 均可）
text = ai_manager.run_agent("agent_xxx", "用户问题", history=[{"role": "user", "content": "…"}])
data = ai_manager.run_agent_json("agent_xxx", "请输出 JSON：…")   # + extract_json，解析失败抛 AIError

# Agent 与模型接入的查询（选择 UI 用）
agents = ai_manager.list_agents()
agent = ai_manager.get_agent("agent_xxx")            # None 时回落 default_agent()
cfg = ai_manager.resolve_agent_runtime("agent_xxx")  # 运行时快照（含解析后的 provider）

# 技能（提示词模板）
skills = ai_manager.list_skills()      # 内置在前（builtin_ 前缀，只读）+ 自定义
```

- **错误契约**：所有失败抛 `AIError`（中文消息可直接展示）；HTTP 错误带状态码与响应摘要。
- **快照语义**：`agent_or_id` 传「含 provider 键的 dict」时直接使用、不回读文件——插件把
  `resolve_agent_runtime()` 的结果放进命令载荷，任务期间配置修改不影响正在跑的任务。
- **工具循环**：Agent 启用了内置工具时，`run_agent` 自动执行 function calling 循环
  （工具结果以 JSON 回传，最多 3 轮，之后强制最终回答）；工具执行异常也以
  `{"error": …}` 回传给模型而不是中断任务。

## 结构化输出（LangChain 通道，可选增强）

- `workbench/ai_chain.py` 的 `run_structured(agent, prompt)`：用 LangChain
  `with_structured_output` + Pydantic schema 强制模型按 `{index, answer, explain}`
  返回，字段缺失/类型不符在框架层被拒绝；供应商接入复用 ai_manager 的配置，
  入口同样过 `validate_endpoint` 的 SSRF 校验。
- 依赖 `langchain-openai`（requirements.txt 已锁定 1.6.7；未安装时
  `structured_available()` 返回 False，调用方降级）。
- 方法固定为 `function_calling`（工具调用通道，兼容性最广）：部分 OpenAI 兼容
  端点不支持 `response_format`（本项目 DeepSeek 接入实测返回 400），不采用
  json_schema 方法。
- `run_agent(..., json_mode=True)` 提供接口级 JSON 模式
  （`response_format=json_object`），供应商不支持该参数时自动降级普通调用。
- 学习通批改的作答链为三级降级：LangChain 结构化 → JSON 模式 → 普通调用 +
  文本兜底解析（单题回复未按 JSON 时从纯文字提取「答案：X」；填空/简答直接
  采用回复正文）。

## 安全边界（SSRF 防护，与模型接入共用）

- 接口地址只允许 http/https；保存与每次调用前都会校验。
- 请求前解析目标主机，拒绝私网（10/172.16/192.168）、运营商级 NAT（100.64.0.0/10）、
  链路本地、保留与组播地址；仅放行公网与本机环回（127.0.0.1，供本地大模型网关）。
- 禁用 HTTP 重定向（Authorization 不跟随 302）。
- API Key 只存本机 `runtime/`（已在 .gitignore），请勿分享该目录下的 JSON 文件。

## 学习通「AI 批改作业」的接线方式（供其他插件参考）

- UI 侧（学习通助手页的「学习通设置」视图，工具栏齿轮按钮进入）只保存批改 Agent 的 id；工具栏「批改作业」开关把
  `menu_enabled` 写入 `runtime/plugins/chaoxing_assistant/ai_settings.json` 并提交轻量命令
  `chaoxing_ai_menu`（仅含 enabled）。
- worker 侧在批改时依据该文件里的 agent_id 经 `ai_manager.get_agent / resolve_agent_runtime`
  解析运行时快照，再调 `ai_manager.run_agent(快照, 单题提示词)`——不 import UI。
- 批改由宿主空闲轮询驱动（`worker.idle_pollers`）：开启期间自动向各页面注入「AI 批改助手」
  悬浮菜单并处理菜单命令；用户打开作业/章节测验页面点「开始批改」即逐页批注，不自动填写，
  不占用任务闸门。
- 进度经 `chaoxing_grade_status` 事件回报（只刷新插件页头部详情）。批改中用户关闭或
  跳转页面会安全中断本页批改。命令全集见 [docs/commands-and-events.md](commands-and-events.md)。
- 题目提取 / 批注 / 悬浮菜单的注入脚本见 `plugins/chaoxing_assistant/ai_homework.py`
  （JS 常量已纳入 `tools/js_syntax_check.py` 校验）。
