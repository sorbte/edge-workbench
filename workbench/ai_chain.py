"""LangChain 结构化输出通道（可选增强）：用 Pydantic schema 强制模型按固定格式作答。

- 依赖 langchain-openai（requirements.txt 已锁定；未安装时 structured_available() 返回
  False，调用方自动回落 ai_manager.run_agent 的 JSON 模式与文本兜底解析）；
- 供应商接入沿用 ai_manager 的模型接入配置（base_url / api_key / model），入口处
  复用 validate_endpoint 做 SSRF 校验（与 ai_manager 同一安全边界）；
- 结构化输出经 with_structured_output（function calling）实现，字段缺失或类型不符
  会在框架层被拒绝，从根上避免「回复无法解析为答案」的问题。
"""

from __future__ import annotations

from typing import Any

from .ai_manager import AIError, resolve_agent_runtime, validate_endpoint

try:  # LangChain 为可选依赖：未安装时整体降级，不影响宿主启动
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI
    from pydantic import BaseModel, Field, field_validator

    _AVAILABLE = True
except Exception:  # pragma: no cover - 环境未安装 LangChain
    _AVAILABLE = False

if _AVAILABLE:

    class AnswerItem(BaseModel):
        """单题答案：结构化输出的最小单元。"""

        index: int = Field(description="题目编号 index，必须与提问一致")
        answer: str = Field(min_length=1, description="答案：选项字母/判断词/答案内容，禁止留空或答「无法确定」")
        explain: str = Field(default="", description="一句话解释，不超过 40 字")

        @field_validator("answer")
        @classmethod
        def _answer_must_not_be_empty(cls, value: str) -> str:
            text = str(value or "").strip()
            if not text or text in {"无法确定", "不知道", "无"}:
                raise ValueError("答案不能为空或「无法确定」，必须给出最可能的答案")
            return text

    class AnswerSheet(BaseModel):
        """整卷答案列表。"""

        answers: list[AnswerItem] = Field(default_factory=list)


def structured_available() -> bool:
    """LangChain 依赖是否可用（可用时优先走结构化输出通道）。"""
    return _AVAILABLE


def run_structured(
    agent_or_snapshot: Any,
    prompt: str,
    *,
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> list[dict[str, Any]]:
    """用 LangChain 结构化输出作答：强制返回 [{index, answer, explain}, ...]。

    agent_or_snapshot 与 ai_manager.run_agent 的入参相同（agent id / agent dict /
    运行时快照 dict）。任何失败抛 AIError，由调用方降级到普通 JSON 模式。
    """
    if not _AVAILABLE:
        raise AIError("LangChain 未安装，结构化输出通道不可用。")
    cfg = resolve_agent_runtime(agent_or_snapshot)
    provider = cfg["provider"]
    base_url = str((provider or {}).get("base_url") or "").strip()
    api_key = str((provider or {}).get("api_key") or "").strip()
    model = str((provider or {}).get("model") or "").strip()
    if not base_url or not model:
        raise AIError("AI 配置不完整：缺少接口地址或模型名称。")
    # SSRF 边界与 ai_manager 同源校验；ChatOpenAI 会自行追加 /chat/completions
    url = validate_endpoint(base_url).rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=url,
        temperature=float(cfg["temperature"] or 0.2),
        timeout=float(timeout or cfg["timeout"] or 90.0),
        max_tokens=max_tokens,
    )
    messages = []
    if cfg["system_prompt"]:
        messages.append(SystemMessage(content=cfg["system_prompt"]))
    messages.append(HumanMessage(content=str(prompt or "")))
    try:
        # method="function_calling" 走工具调用通道（兼容性最广）；json_schema 方法
        # 依赖供应商支持 response_format，部分 OpenAI 兼容端点（如本项目的 DeepSeek
        # 接入）会返回 400，因此不采用。
        sheet = llm.with_structured_output(AnswerSheet, method="function_calling").invoke(messages)
    except Exception as exc:
        raise AIError(f"结构化输出失败：{exc}") from exc
    items: list[dict[str, Any]] = []
    for item in (sheet.answers if sheet else []) or []:
        try:
            items.append({
                "index": int(item.index),
                "answer": str(item.answer or "").strip(),
                "explain": str(item.explain or "").strip(),
            })
        except (TypeError, ValueError):
            continue
    return items
