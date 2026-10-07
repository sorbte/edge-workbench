"""AI 功能管理：多平台 AI 配置的增删改查与 OpenAI 兼容对话调用。

配置持久化在 runtime/ai_providers.json（runtime/ 已在 .gitignore 内，
API Key 只保存在本机）。全部平台走 OpenAI 兼容 /chat/completions 接口，
因此 DeepSeek、OpenAI、Kimi、智谱 GLM、通义千问以及任何兼容网关都能
直接接入，无需引入 langchain 之类的重依赖。

安全边界：接口地址由用户在本机配置，仍按 SSRF 防御处理——
1. 只允许 http/https 协议；
2. 请求前解析域名，私网（10/172.16/192.168 等）、链路本地与云元数据
   地址一律拒绝，仅放行公网地址与本机环回（127.0.0.1 本地大模型网关）；
3. 禁用 HTTP 重定向，防止带 Authorization 头被 302 带走。

本模块同时被主进程（AI 管理页）与插件子进程（学习通 AI 作业批改）使用，
不允许 import 任何 Tk。
"""
from __future__ import annotations

import ast
import ipaddress
import json
import operator
import socket
import time
import urllib.error
import urllib.request
import uuid
from typing import Any
from urllib.parse import urlsplit

from .config import AI_AGENTS_FILE, AI_PROVIDERS_FILE, AI_SKILLS_FILE


class AIError(RuntimeError):
    """AI 配置或调用失败（中文消息，直接面向用户展示）。"""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁用重定向：AI 请求携带 Authorization，不允许被 302 转发到别处。"""

    def redirect_request(self, _req: Any, _fp: Any, _code: Any, _msg: Any, _headers: Any, _newurl: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


# 平台预设：新增/编辑时一键填好接口地址与候选模型；custom 供任意 OpenAI 兼容网关。
AI_PLATFORM_PRESETS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "name": "DeepSeek（推荐）",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "apply_url": "https://platform.deepseek.com",
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "apply_url": "https://platform.openai.com",
    },
    "moonshot": {
        "name": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "kimi-k2-0711-preview"],
        "apply_url": "https://platform.moonshot.cn",
    },
    "zhipu": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-flash", "glm-4-air", "glm-4-plus"],
        "apply_url": "https://open.bigmodel.cn",
    },
    "dashscope": {
        "name": "阿里云百炼（通义千问）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-turbo", "qwen-plus", "qwen-max"],
        "apply_url": "https://bailian.console.aliyun.com",
    },
    "custom": {
        "name": "自定义 OpenAI 兼容",
        "base_url": "",
        "models": [],
        "apply_url": "",
    },
}

PROVIDER_FIELDS = ("id", "name", "platform", "base_url", "api_key", "model", "created_at", "updated_at")


# ---------------------------------------------------------------- 提示词技能（Skill）
# 技能 = 可复用的系统提示词模板；所有技能都必须保持同一 JSON 输出契约（answers[index]/answer），
# 否则学习通批改的解析会失败。内置技能随代码发布（id 以 builtin_ 开头，不可改删），
# 自定义技能持久化在 runtime/ai_skills.json。

_GRADING_PROMPT_GENERAL = (
    "你是学习通作业批改助手。请根据题目与选项作答，只输出 JSON，不要输出任何解释、推理过程或多余文本。\n"
    'JSON 格式：{"answers":[{"index":1,"answer":"B"}]}\n'
    "规则：\n"
    "- 单选：answer 为一个选项字母，如 \"B\"；\n"
    "- 多选：answer 为字母数组，如 [\"A\",\"C\"]；\n"
    "- 判断：answer 为「正确」或「错误」；\n"
    "- 填空：answer 为字符串数组，按空的数量逐空给出；\n"
    "- 简答/问答：answer 为不超过 120 字的要点文本；\n"
    "- 阅读理解/完形填空等材料题的小题按单选处理，答案必须依据所给材料；\n"
    "- 无法完全确定的题目按最可能的选择作答，不要留空。"
)

BUILT_IN_SKILLS: tuple[dict[str, str], ...] = (
    {
        "id": "builtin_general",
        "name": "通用作业批改",
        "description": "适合大多数作业：单选 / 多选 / 判断 / 填空 / 简答与阅读理解材料题统一作答（默认技能）。",
        "content": _GRADING_PROMPT_GENERAL,
    },
    {
        "id": "builtin_reading",
        "name": "阅读理解材料精答",
        "description": "严格依据材料作答，材料未覆盖时才按常识选最优；适合英语/语文阅读理解。",
        "content": (
            "你是学习通作业批改助手，当前使用「阅读理解材料精答」技能。请只依据所给材料作答；材料未覆盖时按常识选择最可能正确的选项。"
            "只输出 JSON，不要输出解释或推理过程。\n"
            'JSON 格式：{"answers":[{"index":1,"answer":"B"}]}\n'
            "规则：\n"
            "- 单选 answer 为选项字母；多选为字母数组；判断为「正确」或「错误」；填空为字符串数组；简答为不超过 80 字的要点文本；\n"
            "- 阅读理解/完形填空的小题按单选处理，答案必须与材料内容一致；\n"
            "- 材料与选项冲突时以材料为准；\n"
            "- 无法完全确定的题目按最可能的选择作答，不要留空。"
        ),
    },
    {
        "id": "builtin_calc",
        "name": "计算题精确作答",
        "description": "理工科作业：填空/计算题给出精确数值、单位与有效数字。",
        "content": (
            "你是学习通作业批改助手，当前使用「计算题精确作答」技能。请按理工科规范计算，只输出 JSON，不要输出推理过程。\n"
            'JSON 格式：{"answers":[{"index":1,"answer":"B"}]}\n'
            "规则：\n"
            "- 单选 answer 为选项字母；多选为字母数组；判断为「正确」或「错误」；\n"
            "- 填空/计算：answer 为字符串数组，逐空给出最终结果，带单位与合理有效数字（如 \"9.8 m/s²\"），必要时附一步公式要点；\n"
            "- 简答：给出关键公式、代入与结论，不超过 120 字；\n"
            "- 结果不确定时按最接近的标准值作答，不要留空。"
        ),
    },
    {
        "id": "builtin_concise",
        "name": "简答题要点化",
        "description": "简答/论述/名词解释按分条要点作答，控制字数。",
        "content": (
            "你是学习通作业批改助手，当前使用「简答题要点化」技能。请用要点式作答，只输出 JSON，不要输出解释。\n"
            'JSON 格式：{"answers":[{"index":1,"answer":"B"}]}\n'
            "规则：\n"
            "- 单选 answer 为选项字母；多选为字母数组；判断为「正确」或「错误」；填空为字符串数组；\n"
            "- 简答/论述：answer 为「①…②…③…」分条要点，每条不超过 30 字，总计不超过 120 字；\n"
            "- 名词解释：一句话定义加一个特征；\n"
            "- 只答题目所问，不扩展背景，不要留空。"
        ),
    },
)

DEFAULT_SKILL_ID = "builtin_general"
SKILL_FIELDS = ("id", "name", "description", "content", "created_at", "updated_at")


def _empty_payload() -> dict[str, Any]:
    return {"providers": [], "default_id": ""}


def load_ai_providers() -> dict[str, Any]:
    try:
        payload = json.loads(AI_PROVIDERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_payload()
    if not isinstance(payload, dict):
        return _empty_payload()
    providers = [item for item in payload.get("providers", []) if isinstance(item, dict)]
    default_id = str(payload.get("default_id") or "")
    return {"providers": providers, "default_id": default_id}


def save_ai_providers(payload: dict[str, Any]) -> None:
    AI_PROVIDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = AI_PROVIDERS_FILE.with_name(f".{AI_PROVIDERS_FILE.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(AI_PROVIDERS_FILE)


def list_providers() -> list[dict[str, Any]]:
    return list(load_ai_providers()["providers"])


def get_provider(provider_id: str) -> dict[str, Any] | None:
    target = str(provider_id or "")
    return next((item for item in list_providers() if str(item.get("id")) == target), None)


def default_provider() -> dict[str, Any] | None:
    """当前默认 AI；默认项缺失时回落到列表第一个。"""
    payload = load_ai_providers()
    providers = payload["providers"]
    default_id = str(payload.get("default_id") or "")
    for item in providers:
        if str(item.get("id")) == default_id:
            return item
    return providers[0] if providers else None


def resolve_provider(provider_id: str = "") -> dict[str, Any] | None:
    """按 ID 取指定 AI，取不到（或传空）时回落到默认 AI。"""
    found = get_provider(provider_id) if provider_id else None
    return found or default_provider()


def _normalize(values: dict[str, Any]) -> dict[str, Any]:
    return {key: str(values.get(key) or "").strip() for key in ("name", "platform", "base_url", "api_key", "model")}


def _validate(fields: dict[str, str]) -> None:
    if not fields["name"]:
        raise AIError("AI 名称不能为空。")
    if not fields["base_url"]:
        raise AIError("接口地址（Base URL）不能为空。")
    if not fields["api_key"]:
        raise AIError("API Key 不能为空。")
    if not fields["model"]:
        raise AIError("模型名称不能为空。")
    validate_endpoint(fields["base_url"])


def validate_endpoint(base_url: str) -> str:
    """校验接口地址：协议白名单 + 目标 IP 边界（防 SSRF），返回规范化 URL。"""
    text = str(base_url or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AIError("接口地址必须是以 http:// 或 https:// 开头的完整 URL。")
    try:
        parsed.port
    except ValueError:
        raise AIError("接口地址格式无效：端口或 IPv6 写法不正确（IPv6 需用方括号包裹）。") from None
    try:
        infos = socket.getaddrinfo(parsed.hostname, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise AIError(f"接口地址域名解析失败：{exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]))
        if address.is_loopback:
            # 仅本机环回放行（127.0.0.1 本地大模型网关）；其余私网/链路本地/内网一律拒绝
            continue
        if (
            address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
            or address in ipaddress.ip_network("100.64.0.0/10")  # 运营商级 NAT 段，ipaddress 不算私网
        ):
            raise AIError(
                f"接口地址指向内网或保留地址（{address}），已拒绝；请填写公网 OpenAI 兼容接口地址。"
            )
    return text


def add_provider(
    name: str,
    platform: str,
    base_url: str,
    api_key: str,
    model: str,
) -> dict[str, Any]:
    fields = _normalize(
        {"name": name, "platform": platform or "custom", "base_url": base_url, "api_key": api_key, "model": model}
    )
    _validate(fields)
    payload = load_ai_providers()
    provider = {
        "id": f"ai_{uuid.uuid4().hex[:10]}",
        **fields,
        "created_at": now_stamp(),
        "updated_at": now_stamp(),
    }
    payload["providers"].append(provider)
    # 第一条配置自动成为默认，保证「新增即可用」
    if not payload.get("default_id"):
        payload["default_id"] = provider["id"]
    save_ai_providers(payload)
    return provider


def update_provider(provider_id: str, **fields: Any) -> dict[str, Any]:
    payload = load_ai_providers()
    target = next((item for item in payload["providers"] if str(item.get("id")) == str(provider_id)), None)
    if target is None:
        raise AIError("未找到要编辑的 AI 配置。")
    merged = {key: target.get(key, "") for key in ("name", "platform", "base_url", "api_key", "model")}
    merged.update({key: str(value or "").strip() for key, value in fields.items() if key in merged})
    _validate(_normalize(merged))
    target.update(merged, updated_at=now_stamp())
    save_ai_providers(payload)
    return target


def remove_provider(provider_id: str) -> None:
    payload = load_ai_providers()
    target_id = str(provider_id or "")
    payload["providers"] = [item for item in payload["providers"] if str(item.get("id")) != target_id]
    if payload.get("default_id") == target_id:
        payload["default_id"] = str(payload["providers"][0]["id"]) if payload["providers"] else ""
    save_ai_providers(payload)


def set_default_provider(provider_id: str) -> None:
    payload = load_ai_providers()
    target_id = str(provider_id or "")
    if not any(str(item.get("id")) == target_id for item in payload["providers"]):
        raise AIError("未找到要设为默认的 AI 配置。")
    payload["default_id"] = target_id
    save_ai_providers(payload)


# ---------------------------------------------------------------- 技能（Skill）存取

def load_ai_skills() -> dict[str, Any]:
    try:
        payload = json.loads(AI_SKILLS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"skills": []}
    if not isinstance(payload, dict):
        return {"skills": []}
    return {"skills": [item for item in payload.get("skills", []) if isinstance(item, dict)]}


def save_ai_skills(payload: dict[str, Any]) -> None:
    AI_SKILLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = AI_SKILLS_FILE.with_name(f".{AI_SKILLS_FILE.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(AI_SKILLS_FILE)


def list_skills() -> list[dict[str, Any]]:
    """全部可用技能：内置在前（只读），自定义在后。"""
    built_in = [dict(item) for item in BUILT_IN_SKILLS]
    return built_in + list(load_ai_skills()["skills"])


def get_skill(skill_id: str) -> dict[str, Any] | None:
    target = str(skill_id or "")
    if not target:
        return None
    for item in BUILT_IN_SKILLS:
        if item["id"] == target:
            return dict(item)
    return next((item for item in load_ai_skills()["skills"] if str(item.get("id")) == target), None)


def _validate_skill(name: str, content: str) -> tuple[str, str]:
    name_text = str(name or "").strip()
    content_text = str(content or "").strip()
    if not name_text:
        raise AIError("技能名称不能为空。")
    if len(name_text) > 30:
        raise AIError("技能名称请控制在 30 字以内。")
    if not content_text:
        raise AIError("技能提示词内容不能为空。")
    if "answers" not in content_text or "index" not in content_text:
        raise AIError(
            "技能提示词必须保留 JSON 输出契约（含 {\"answers\":[{\"index\":..,\"answer\":..}]}），否则批改结果无法解析。"
        )
    return name_text, content_text


def add_skill(name: str, content: str, description: str = "") -> dict[str, Any]:
    name_text, content_text = _validate_skill(name, content)
    payload = load_ai_skills()
    skill = {
        "id": f"skill_{uuid.uuid4().hex[:10]}",
        "name": name_text,
        "description": str(description or "").strip()[:80],
        "content": content_text,
        "created_at": now_stamp(),
        "updated_at": now_stamp(),
    }
    payload["skills"].append(skill)
    save_ai_skills(payload)
    return skill


def update_skill(skill_id: str, name: str, content: str, description: str = "") -> dict[str, Any]:
    target = str(skill_id or "")
    if target.startswith("builtin_"):
        raise AIError("内置技能不可编辑，可复制内容后另存为自定义技能。")
    payload = load_ai_skills()
    item = next((entry for entry in payload["skills"] if str(entry.get("id")) == target), None)
    if item is None:
        raise AIError("未找到要编辑的自定义技能。")
    name_text, content_text = _validate_skill(name, content)
    item.update(
        name=name_text,
        description=str(description or "").strip()[:80],
        content=content_text,
        updated_at=now_stamp(),
    )
    save_ai_skills(payload)
    return item


def remove_skill(skill_id: str) -> None:
    target = str(skill_id or "")
    if target.startswith("builtin_"):
        raise AIError("内置技能不可删除。")
    payload = load_ai_skills()
    payload["skills"] = [item for item in payload["skills"] if str(item.get("id")) != target]
    save_ai_skills(payload)


def masked_key(api_key: str) -> str:
    key = str(api_key or "").strip()
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _chat_request(
    provider: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    timeout: float = 90.0,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    response_json: bool = False,
) -> dict[str, Any]:
    """底层对话请求：返回助手的原始 message（可能带 tool_calls）。

    response_json=True 时附带 response_format=json_object（接口级结构化输出，
    要求模型必须返回合法 JSON；供应商不支持时会报错，由调用方降级重试）。
    """
    base_url = str((provider or {}).get("base_url") or "").strip()
    api_key = str((provider or {}).get("api_key") or "").strip()
    model = str((provider or {}).get("model") or "").strip()
    if not base_url or not model:
        raise AIError("AI 配置不完整：缺少接口地址或模型名称。")
    url = validate_endpoint(base_url).rstrip("/")
    if not url.endswith("/chat/completions"):
        url = f"{url}/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if response_json:
        body["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        raise AIError(f"AI 接口返回错误（HTTP {exc.code}）：{detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIError(f"无法连接 AI 接口：{exc}") from exc

    try:
        payload = json.loads(raw)
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message")
        if isinstance(message, dict):
            return message
    except (ValueError, AttributeError, IndexError):
        pass
    raise AIError("AI 接口返回内容无法解析，请检查模型名称是否正确。")


def _message_text(message: dict[str, Any], name: str = "AI") -> str:
    """从助手 message 取最终文本（兼容 reasoning_content）。"""
    content = str(message.get("content") or "").strip()
    if not content and message.get("reasoning_content"):
        content = str(message["reasoning_content"]).strip()
    if content:
        return content
    raise AIError(f"{name}未返回文本内容，请检查模型与提示词。")


def chat_completion(
    provider: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    timeout: float = 90.0,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> str:
    """调用 OpenAI 兼容对话接口并返回助手回复文本；失败抛 AIError。"""
    message = _chat_request(provider, messages, timeout=timeout, temperature=temperature, max_tokens=max_tokens)
    return _message_text(message)


def test_provider(provider: dict[str, Any]) -> str:
    """连通性测试：发出一句固定问候，返回 AI 回复文本。"""
    return chat_completion(
        provider,
        [{"role": "user", "content": "连接测试，请只回复四个字：连接成功"}],
        timeout=30.0,
        temperature=0.0,
        max_tokens=16,
    )


def extract_json(text: str) -> Any:
    """从 AI 回复里尽力提取第一个 JSON 对象/数组（容忍代码块围栏与前后杂文）。"""
    if not text:
        return None
    content = text.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
    starts = [index for index in (content.find("{"), content.find("[")) if index >= 0]
    if not starts:
        return None
    start = min(starts)
    for end in range(len(content), start, -1):
        try:
            return json.loads(content[start:end])
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- Agent（智能体）
# Agent = 模型接入（引用 providers）+ 系统提示词 + 内置工具 + 采样参数，
# 是插件调用 AI 的统一入口：插件只认 agent id，模型/密钥/提示词细节全部收进本模块。

AGENT_FIELDS = (
    "id", "name", "description", "provider_id", "system_prompt", "tools",
    "temperature", "timeout", "created_at", "updated_at",
)

# 宿主内置工具（OpenAI function calling 协议）；executor 返回值会被包成 JSON 回传给模型
_CALC_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
    ast.Pow: operator.pow,
}
_CALC_UNARYOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}


def _calc_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _calc_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_BINOPS:
        left, right = _calc_eval(node.left), _calc_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise AIError("指数过大，已拒绝计算。")
        return _CALC_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_UNARYOPS:
        return _CALC_UNARYOPS[type(node.op)](_calc_eval(node.operand))
    raise AIError("算式包含不支持的内容。")


def _tool_get_current_time(_arguments: dict[str, Any]) -> str:
    return now_stamp()


def _tool_calculate(arguments: dict[str, Any]) -> str:
    expression = str((arguments or {}).get("expression") or "").strip()
    if not expression:
        raise AIError("算式为空。")
    value = _calc_eval(ast.parse(expression, mode="eval"))
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    return str(value)


BUILT_IN_TOOLS: dict[str, dict[str, Any]] = {
    "get_current_time": {
        "description": "获取当前本机的本地日期与时间",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "executor": _tool_get_current_time,
    },
    "calculate": {
        "description": "计算四则运算算式，支持 + - * / % // ** 与括号",
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "算式，例如 (3+4)*2/7"}},
            "required": ["expression"],
        },
        "executor": _tool_calculate,
    },
}
MAX_TOOL_ROUNDS = 3


def _execute_tool(name: Any, arguments: Any) -> str:
    tool = BUILT_IN_TOOLS.get(str(name or ""))
    if tool is None:
        return json.dumps({"error": f"未知工具：{name}"}, ensure_ascii=False)
    try:
        result = tool["executor"](arguments if isinstance(arguments, dict) else {})
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    try:
        return json.dumps({"result": result}, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({"result": str(result)}, ensure_ascii=False)


def load_agents() -> dict[str, Any]:
    try:
        payload = json.loads(AI_AGENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"agents": [], "default_id": ""}
    if not isinstance(payload, dict):
        return {"agents": [], "default_id": ""}
    return {
        "agents": [item for item in payload.get("agents", []) if isinstance(item, dict)],
        "default_id": str(payload.get("default_id") or ""),
    }


def save_agents(payload: dict[str, Any]) -> None:
    AI_AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = AI_AGENTS_FILE.with_name(f".{AI_AGENTS_FILE.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(AI_AGENTS_FILE)


def list_agents() -> list[dict[str, Any]]:
    return list(load_agents()["agents"])


def get_agent(agent_id: str) -> dict[str, Any] | None:
    target = str(agent_id or "")
    return next((item for item in list_agents() if str(item.get("id")) == target), None)


def default_agent() -> dict[str, Any] | None:
    """当前默认 Agent；默认项缺失时回落到列表第一个。"""
    payload = load_agents()
    agents = payload["agents"]
    default_id = str(payload.get("default_id") or "")
    for item in agents:
        if str(item.get("id")) == default_id:
            return item
    return agents[0] if agents else None


def resolve_agent(agent_id: str = "") -> dict[str, Any] | None:
    """按 ID 取指定 Agent，取不到（或传空）时回落到默认 Agent。"""
    found = get_agent(agent_id) if agent_id else None
    return found or default_agent()


def _validate_agent(
    name: str,
    provider_id: str,
    system_prompt: str,
    tools: Any,
    temperature: Any,
    timeout: Any,
) -> dict[str, Any]:
    name_text = str(name or "").strip()
    if not name_text:
        raise AIError("Agent 名称不能为空。")
    if len(name_text) > 30:
        raise AIError("Agent 名称请控制在 30 字以内。")
    if get_provider(str(provider_id or "")) is None:
        raise AIError("Agent 必须绑定一个已有的模型接入（AI 配置）。")
    prompt_text = str(system_prompt or "").strip()
    if not prompt_text:
        raise AIError("系统提示词不能为空。")
    tool_list = sorted({str(item) for item in (tools or []) if str(item) in BUILT_IN_TOOLS})
    try:
        temp = float(temperature)
    except (TypeError, ValueError):
        temp = 0.2
    temp = min(max(temp, 0.0), 2.0)
    try:
        tout = float(timeout)
    except (TypeError, ValueError):
        tout = 0.0
    tout = min(max(tout, 0.0), 600.0)
    return {
        "name": name_text,
        "provider_id": str(provider_id),
        "system_prompt": prompt_text,
        "tools": tool_list,
        "temperature": temp,
        "timeout": tout,
    }


def add_agent(
    name: str,
    provider_id: str,
    system_prompt: str,
    description: str = "",
    tools: Any = None,
    temperature: float = 0.2,
    timeout: float = 0.0,
) -> dict[str, Any]:
    fields = _validate_agent(name, provider_id, system_prompt, tools, temperature, timeout)
    payload = load_agents()
    agent = {
        "id": f"agent_{uuid.uuid4().hex[:10]}",
        **fields,
        "description": str(description or "").strip()[:80],
        "created_at": now_stamp(),
        "updated_at": now_stamp(),
    }
    payload["agents"].append(agent)
    if not payload.get("default_id"):
        payload["default_id"] = agent["id"]
    save_agents(payload)
    return agent


def update_agent(agent_id: str, **fields: Any) -> dict[str, Any]:
    payload = load_agents()
    target = next((item for item in payload["agents"] if str(item.get("id")) == str(agent_id)), None)
    if target is None:
        raise AIError("未找到要编辑的 Agent。")
    merged = {key: target.get(key) for key in ("name", "description", "provider_id", "system_prompt", "tools", "temperature", "timeout")}
    merged.update({key: fields[key] for key in fields if key in merged})
    validated = _validate_agent(
        merged["name"], merged["provider_id"], merged["system_prompt"], merged["tools"], merged["temperature"], merged["timeout"]
    )
    target.update(
        validated,
        description=str(merged.get("description") or "").strip()[:80],
        updated_at=now_stamp(),
    )
    save_agents(payload)
    return target


def remove_agent(agent_id: str) -> None:
    payload = load_agents()
    target_id = str(agent_id or "")
    payload["agents"] = [item for item in payload["agents"] if str(item.get("id")) != target_id]
    if payload.get("default_id") == target_id:
        payload["default_id"] = str(payload["agents"][0]["id"]) if payload["agents"] else ""
    save_agents(payload)


def set_default_agent(agent_id: str) -> None:
    payload = load_agents()
    target_id = str(agent_id or "")
    if not any(str(item.get("id")) == target_id for item in payload["agents"]):
        raise AIError("未找到要设为默认的 Agent。")
    payload["default_id"] = target_id
    save_agents(payload)


def resolve_agent_runtime(agent_or_id: Any) -> dict[str, Any]:
    """把 Agent 统一解析为运行时快照：{name, provider, system_prompt, tools, temperature, timeout}。

    - 传 id → 读取当前配置；
    - 传 agents.json 的 agent dict → 按 provider_id 解析模型接入；
    - 传含 provider 键的快照 dict（命令载荷）→ 直接使用，不回读文件。
    """
    if isinstance(agent_or_id, dict) and isinstance(agent_or_id.get("provider"), dict):
        snapshot = agent_or_id
        return {
            "name": str(snapshot.get("name") or "Agent"),
            "provider": snapshot["provider"],
            "system_prompt": str(snapshot.get("system_prompt") or ""),
            "tools": [str(item) for item in (snapshot.get("tools") or [])],
            "temperature": snapshot.get("temperature", 0.2),
            "timeout": snapshot.get("timeout", 0.0),
        }
    agent = agent_or_id if isinstance(agent_or_id, dict) else get_agent(str(agent_or_id or ""))
    if agent is None:
        raise AIError("未找到指定的 Agent，请先在「Agent 管理」页创建。")
    provider = get_provider(str(agent.get("provider_id") or ""))
    if provider is None:
        raise AIError(
            f"Agent「{agent.get('name') or agent.get('id')}」绑定的模型接入不存在，请到「Agent 管理」页重新选择。"
        )
    return {
        "name": str(agent.get("name") or "Agent"),
        "provider": provider,
        "system_prompt": str(agent.get("system_prompt") or ""),
        "tools": [str(item) for item in (agent.get("tools") or [])],
        "temperature": agent.get("temperature", 0.2),
        "timeout": agent.get("timeout", 0.0),
    }


def _tool_schemas(tool_names: list[str]) -> list[dict[str, Any]]:
    schemas: list[dict[str, Any]] = []
    for name in tool_names:
        tool = BUILT_IN_TOOLS.get(name)
        if tool is None:
            continue
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
        )
    return schemas


def run_agent(
    agent_or_id: Any,
    prompt: str,
    *,
    history: list[dict[str, str]] | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
    json_mode: bool = False,
) -> str:
    """对外统一调用口：插件用一行代码调用某个 Agent（自动处理模型解析与工具循环）。

    agent_or_id 可以是 agent id、agents.json 的 agent dict，或含 provider 键的
    运行时快照 dict（插件经命令载荷传入，保证任务期间配置不被中途修改影响）。
    json_mode=True 时启用接口级结构化输出（response_format=json_object），
    强制模型返回合法 JSON；供应商不支持该参数时自动降级为普通调用。
    """
    cfg = resolve_agent_runtime(agent_or_id)
    messages: list[dict[str, Any]] = []
    if cfg["system_prompt"]:
        messages.append({"role": "system", "content": cfg["system_prompt"]})
    for item in (history or []):
        role = str(item.get("role") or "")
        if role in {"user", "assistant"} and item.get("content"):
            messages.append({"role": role, "content": str(item["content"])})
    messages.append({"role": "user", "content": str(prompt or "")})
    schemas = _tool_schemas(cfg["tools"])
    timeout_value = float(timeout or cfg["timeout"] or 90.0)
    temperature_value = float(cfg["temperature"] or 0.2)

    def _chat(round_messages: list[dict[str, Any]], with_tools: bool) -> dict[str, Any]:
        response_json = bool(json_mode) and not with_tools
        if not response_json:
            return _chat_request(
                cfg["provider"], round_messages, tools=schemas or None,
                timeout=timeout_value, temperature=temperature_value, max_tokens=max_tokens,
            )
        try:
            return _chat_request(
                cfg["provider"], round_messages,
                timeout=timeout_value, temperature=temperature_value, max_tokens=max_tokens,
                response_json=True,
            )
        except AIError:
            # 供应商不支持 response_format 时降级为普通调用（提示词里仍有 JSON 契约）
            return _chat_request(
                cfg["provider"], round_messages, tools=schemas or None,
                timeout=timeout_value, temperature=temperature_value, max_tokens=max_tokens,
            )

    for round_index in range(MAX_TOOL_ROUNDS + 1):
        message = _chat(messages, with_tools=bool(schemas))
        calls = message.get("tool_calls") or []
        if not calls or not schemas:
            return _message_text(message, f"Agent「{cfg['name']}」")
        if round_index >= MAX_TOOL_ROUNDS:
            # 达到轮数上限后不带工具再请求一次，强制给出最终回答
            final = _chat(messages, with_tools=False)
            return _message_text(final, f"Agent「{cfg['name']}」")
        messages.append({"role": "assistant", "content": str(message.get("content") or ""), "tool_calls": calls})
        for call in calls:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            result = _execute_tool(function.get("name"), arguments)
            messages.append({"role": "tool", "tool_call_id": str(call.get("id") or ""), "content": result})
    raise AIError(f"Agent「{cfg['name']}」工具循环异常，请重试。")


def run_agent_json(
    agent_or_id: Any,
    prompt: str,
    *,
    history: list[dict[str, str]] | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> Any:
    """run_agent + extract_json：Agent 返回 JSON 时用这个；解析失败抛 AIError。"""
    text = run_agent(agent_or_id, prompt, history=history, timeout=timeout, max_tokens=max_tokens, json_mode=True)
    data = extract_json(text)
    if data is None:
        raise AIError("Agent 返回内容无法解析为 JSON，请检查提示词中的输出格式约定。")
    return data
