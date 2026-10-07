"""学习通 AI 作业批改（页面菜单模式）：在题目下方批注 AI 参考答案与解释，绝不自动填写。

硬性边界：
- 由工作台工具栏的「批改作业」开关控制：开启后，宿主空闲轮询仅向作业 / 章节测验类
  页面（帧 URL 命中 work 体系，或存在题目节点）注入可拖动的「AI 批改助手」悬浮菜单，
  并处理菜单命令（开始批改 / 停止 / 重新批改）；其他页面不显示菜单；
- 不占用任务闸门：与「开始学习」「刷新全部数据」等命令互不阻塞（任务运行期间菜单
  不注入新页面、批改不响应，任务结束后自动恢复）；
- Agent 在页内「学习通设置」视图选择，批改时 worker 依据本机 ai_settings.json 解析
  运行时快照；
- 只在题目下方批注 AI 参考答案、一句话解释与「你的答案」状态（未作答橙色标出），
  绝不自动选择 / 填写，绝不点击「提交 / 交卷 / 保存」；
- 批改中用户关闭或跳转页面会安全中断本页批改，不影响其他页面继续批改；
- 题目提取与批注脚本基于 div.TiMu 定位；阅读理解 / 完形填空按小题逐题拆分，材料自动
  关联到每道小题，已批阅/只读视图同样逐题批注（温故知新）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from edge_workbench import ai_chain, ai_manager, now_text


MAX_QUESTIONS_PER_ASSIGNMENT = 120
AI_TIMEOUT_SECONDS = 120.0       # 单题请求超时（逐题作答，每题一次请求）
AI_MAX_TOKENS = 4096
MENU_POLL_SECONDS = 1.0          # 空闲轮询间隔（测验页检测 / 菜单注入 / 页面命令处理）

# 作业 / 章节测验类页面的帧 URL 特征（仅这类页面注入批改菜单）
WORK_URL_PATTERN = re.compile(
    r"mooc-ans/work/|/work/doHomeWork|doHomeWorkNew|selectWorkQuestion|/work/showWork|/api/work|/mooc2/work",
    re.IGNORECASE,
)


class _MenuStop(Exception):
    """用户通过页面菜单请求停止当前页的批改。"""


class _PageGone(Exception):
    """批改过程中页面被关闭或跳转，本页批改中断。"""


# ---------------------------------------------------------------- 注入脚本（共用前缀 + 提取/批注）

# 与工作台注入脚本一致：通过 tools/js_syntax_check.py 同源的 node --check 方式单独校验
JS_PRELUDE = r"""
const clean = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
const typeFromText = (text) => {
    const t = clean(text);
    if (/阅读理解|完形填空|材料题/.test(t)) return "阅读理解";
    if (/单选|单项选择/.test(t)) return "单选";
    if (/多选|多项选择/.test(t)) return "多选";
    if (/判断/.test(t)) return "判断";
    if (/填空/.test(t)) return "填空";
    if (/简答|论述|名词解释|问答|翻译|计算|写作/.test(t)) return "简答";
    return "";
};
const optionItems = (root) => {
    const items = [];
    const lis = Array.from(root.querySelectorAll("ul li, ol li")).filter(
        (li) => li.querySelector("input[type='radio'], input[type='checkbox']")
    );
    lis.forEach((li) => {
        const input = li.querySelector("input[type='radio'], input[type='checkbox']");
        const label = clean(li.innerText);
        const keyMatch = label.match(/^([A-Za-z])[\.、．:：\s]/);
        items.push({
            key: keyMatch ? keyMatch[1].toUpperCase() : "",
            text: (keyMatch ? label.replace(/^[A-Za-z][\.、．:：\s]\s*/, "") : label).slice(0, 400),
            checked: Boolean(input && input.checked),
            inputType: input ? input.type : "radio",
        });
    });
    if (!items.length) {
        Array.from(root.querySelectorAll("input[type='radio'], input[type='checkbox']")).forEach((input) => {
            const box = input.closest("li") || input.closest("label") || input.parentElement;
            const label = clean(box ? box.innerText : "");
            const keyMatch = label.match(/^([A-Za-z])[\.、．:：\s]\s*/);
            items.push({
                key: keyMatch ? keyMatch[1].toUpperCase() : "",
                text: (keyMatch ? label.replace(/^[A-Za-z][\.、．:：\s]\s*/, "") : label).slice(0, 400),
                checked: Boolean(input.checked),
                inputType: input.type,
            });
        });
    }
    if (!items.length) {
        // 已批阅/复习视图：选项是无输入框的纯文本 li（<i>A、</i><a>文本</a>），按文本解析
        Array.from(root.querySelectorAll("ul li, ol li")).forEach((li) => {
            const label = clean(li.innerText);
            const match = label.match(/^([A-Za-z])\s*[、.．:：]\s*(.+)$/);
            if (match && label.length <= 400) {
                items.push({ key: match[1].toUpperCase(), text: match[2].trim(), checked: false, inputType: "radio" });
            }
        });
    }
    return items;
};
"""

QUESTION_EXTRACTION_SCRIPT = JS_PRELUDE + r"""
() => {
    const mus = Array.from(document.querySelectorAll("div.TiMu"));
    const questions = [];
    let material = "";
    const buildQuestion = (muIndex, subIndex, root, stemText, typeHint, groupMaterial) => {
        let stem = clean(stemText);
        let type = typeHint || typeFromText(stem.slice(0, 24));
        stem = stem.replace(/^\d+\s*[、.．]\s*/, "").replace(/^[（(]\s*[^）)]{2,6}\s*[）)]\s*/, "");
        const options = optionItems(root);
        const textareas = Array.from(root.querySelectorAll("textarea"));
        if (!type) {
            if (options.length) type = options.some((item) => item.inputType === "checkbox") ? "多选" : "单选";
            else if (textareas.length) type = "填空";
        }
        const hasAnswerUi = Boolean(
            root.querySelector("input[type='radio'], input[type='checkbox'], textarea, .edui-editor, [id^='ueditor']")
        );
        const isBlankLike = (type === "填空" || type === "简答") && !options.length;
        const correctMatch = clean(root.innerText).match(/正确答案[：:]\s*([A-Za-z]{1,8}|正确|错误|对|错|√|×)/);
        questions.push({
            mu_index: muIndex,
            sub_index: subIndex,
            type,
            stem: stem.slice(0, 6000),
            material: groupMaterial || "",
            options: options.map((item) => ({ key: item.key, text: item.text, checked: item.checked })),
            selected_keys: options.filter((item) => item.checked).map((item) => item.key).join(""),
            user_answer: clean((root.querySelector(".myAnswer .answerCon") || {}).textContent).slice(0, 40),
            correct_answer: correctMatch ? correctMatch[1] : "",
            blank_count: isBlankLike ? Math.max(1, textareas.length || 1) : 0,
            blank_values: isBlankLike
                ? textareas.map((node) => clean(node.value).slice(0, 120)).filter(Boolean).join(" ｜ ")
                : "",
            readonly: !hasAnswerUi,
        });
    };
    mus.forEach((mu, muIndex) => {
        if (mu.querySelector("div.TiMu")) return; // 嵌套容器（外层为材料容器）交给最内层
        // 阅读理解/完形填空组：材料在 .Zy_TItle，每道小题是一个 .readCompreHensionItem，
        // 必须逐小题拆开（否则整篇只算一题、小题永远得不到参考答案）
        const rcItems = Array.from(mu.querySelectorAll(".readCompreHensionItem")).filter(
            (node) => node.querySelector("ul li") || node.querySelector("textarea")
        );
        if (rcItems.length) {
            const groupMaterial = clean((mu.querySelector(".Zy_TItle") || {}).innerText).slice(0, 3000);
            rcItems.forEach((item, subIndex) => {
                const indexText = clean((item.querySelector(".clear i.index") || {}).textContent);
                const stemElement = item.querySelector(".clear .clearfix") || item.querySelector(".clear");
                const typeHint = typeFromText(indexText);
                buildQuestion(muIndex, subIndex, item, stemElement ? stemElement.innerText : "", typeHint, groupMaterial);
            });
            return;
        }
        const stemElement = mu.querySelector(".Zy_TItle");
        const typeText = clean((mu.querySelector(".fontLabel, .colorShallow") || {}).textContent);
        const stemText = stemElement ? stemElement.innerText : String(mu.innerText || "").slice(0, 400);
        const type = typeFromText(typeText) || typeFromText(clean(stemText).slice(0, 24));
        const options = optionItems(mu);
        const textareas = Array.from(mu.querySelectorAll("textarea"));
        if ((type === "阅读理解" || !type) && !options.length && !textareas.length) {
            // 纯材料块：文本作为材料附给后续小题，自身不产生待作答题目
            const text = clean(mu.innerText);
            if (text.length > 40) material = text.slice(0, 2000);
            return;
        }
        buildQuestion(muIndex, null, mu, stemText, type, material);
    });
    return questions;
}
"""

ANNOTATE_SCRIPT = r"""
(notes) => {
    const mus = Array.from(document.querySelectorAll("div.TiMu"));
    let applied = 0;
    notes.forEach((note) => {
        const mu = mus[note.mu_index];
        if (!mu) return;
        // 阅读理解组的小题批注要插在小题节点内部（选项下方），而不是整篇材料的末尾
        let node = mu;
        if (note.sub_index !== null && note.sub_index !== undefined) {
            const rcItems = mu.querySelectorAll(".readCompreHensionItem");
            node = rcItems[note.sub_index] || mu;
        }
        const previous = node.querySelectorAll(".wbs-ai-note");
        previous.forEach((old) => old.remove());
        const box = document.createElement("div");
        box.className = "wbs-ai-note " + (note.cls || "ok");
        const palette = {
            ok: "background:#f2fbf4;border:1px solid #bfe8cb;color:#15803d;",
            warn: "background:#fff8ea;border:1px solid #f5dfae;color:#b45309;",
            err: "background:#fdf2f2;border:1px solid #f6caca;color:#b91c1c;",
        };
        box.style.cssText = "margin:8px 0 12px;padding:8px 12px;border-radius:8px;font-size:13px;line-height:1.7;text-align:left;white-space:pre-line;" + (palette[note.cls || "ok"] || palette.ok);
        box.textContent = note.text;
        node.appendChild(box);
        applied += 1;
    });
    return applied;
}
"""

# ---------------------------------------------------------------- 页面悬浮菜单（AI 批改助手）

AI_MENU_SCRIPT = r"""
() => {
    const VERSION = 3;
    if (window.__cxAiMenu && window.__cxAiMenu.version === VERSION) return "exists";
    const stale = document.getElementById("cx-ai-menu-root");
    if (stale) stale.remove();
    const host = document.body || document.documentElement;
    if (!host) return "no-host";

    const root = document.createElement("div");
    root.id = "cx-ai-menu-root";
    root.style.cssText = "position:fixed;top:16px;right:16px;z-index:2147483000;font-family:'Microsoft YaHei','PingFang SC',system-ui,sans-serif;text-align:left;";
    const panel = document.createElement("div");
    panel.style.cssText = "width:252px;background:#ffffff;border:1px solid #d9dfe7;border-radius:14px;box-shadow:0 12px 32px rgba(15,23,42,.20);overflow:hidden;";
    root.appendChild(panel);
    const head = document.createElement("div");
    head.style.cssText = "display:flex;align-items:center;gap:7px;padding:10px 12px;background:linear-gradient(135deg,#0f172a,#1e293b);cursor:grab;user-select:none;";
    head.title = "按住拖动调整位置；单击折叠 / 展开";
    const title = document.createElement("span");
    title.textContent = "AI 批改助手";
    title.style.cssText = "font-size:13px;font-weight:700;color:#ffffff;letter-spacing:.5px;";
    const dot = document.createElement("span");
    dot.style.cssText = "width:8px;height:8px;border-radius:50%;background:#94a3b8;margin-left:auto;flex:none;";
    const fold = document.createElement("span");
    fold.textContent = "−";
    fold.style.cssText = "color:#cbd5e1;font-size:13px;width:14px;text-align:center;flex:none;";
    head.appendChild(title);
    head.appendChild(dot);
    head.appendChild(fold);
    panel.appendChild(head);
    const body = document.createElement("div");
    panel.appendChild(body);
    const statusEl = document.createElement("div");
    statusEl.textContent = "正在连接…";
    statusEl.style.cssText = "margin:10px 12px 0;padding:8px 10px;background:#f8fafc;border-left:3px solid #94a3b8;border-radius:6px;font-size:12px;line-height:1.6;color:#475569;word-break:break-all;white-space:pre-line;";
    body.appendChild(statusEl);
    const startBtn = document.createElement("button");
    startBtn.type = "button";
    startBtn.textContent = "开始批改";
    startBtn.style.cssText = "display:block;width:calc(100% - 24px);margin:10px 12px 0;padding:8px 0;border:none;border-radius:8px;background:#16a34a;color:#ffffff;font-size:13px;font-weight:700;font-family:inherit;cursor:pointer;letter-spacing:2px;";
    body.appendChild(startBtn);
    const btnRow = document.createElement("div");
    btnRow.style.cssText = "display:flex;gap:8px;margin:8px 12px 0;";
    body.appendChild(btnRow);
    const regradeBtn = document.createElement("button");
    regradeBtn.type = "button";
    regradeBtn.textContent = "重新批改";
    regradeBtn.style.cssText = "flex:1;padding:6px 0;border:1px solid #e2e8f0;border-radius:8px;background:#f1f5f9;color:#334155;font-size:12px;font-weight:600;font-family:inherit;cursor:pointer;";
    const stopBtn = document.createElement("button");
    stopBtn.type = "button";
    stopBtn.textContent = "停止";
    stopBtn.style.cssText = "flex:1;padding:6px 0;border:1px solid #f3d4d4;border-radius:8px;background:#fef2f2;color:#b91c1c;font-size:12px;font-weight:600;font-family:inherit;cursor:pointer;";
    btnRow.appendChild(regradeBtn);
    btnRow.appendChild(stopBtn);
    const hint = document.createElement("div");
    hint.textContent = "只批注参考答案与解释，不自动填写、不提交";
    hint.style.cssText = "padding:8px 12px 12px;font-size:11px;line-height:1.5;color:#94a3b8;";
    body.appendChild(hint);

    const statusColor = { info: "#475569", ok: "#15803d", warn: "#b45309", err: "#b91c1c", busy: "#1d4ed8" };
    const dotColor = { info: "#94a3b8", ok: "#22c55e", warn: "#f59e0b", err: "#ef4444", busy: "#3b82f6" };
    const setStatus = (text, kind) => {
        const key = statusColor[kind] ? kind : "info";
        statusEl.textContent = String(text == null ? "" : text);
        statusEl.style.color = statusColor[key];
        statusEl.style.borderLeftColor = dotColor[key];
        dot.style.background = dotColor[key];
    };
    const setBusy = (busy) => {
        const on = Boolean(busy);
        buttons.start.disabled = on;
        buttons.regrade.disabled = on;
        buttons.stop.disabled = !on;
        Object.keys(buttons).forEach((key) => {
            const btn = buttons[key];
            btn.style.opacity = btn.disabled ? "0.5" : "1";
            btn.style.cursor = btn.disabled ? "default" : "pointer";
        });
    };
    const buttons = { start: startBtn, regrade: regradeBtn, stop: stopBtn };

    let dead = false;
    let drag = null;
    const setFold = (hidden) => {
        body.style.display = hidden ? "none" : "";
        fold.textContent = hidden ? "+" : "−";
    };
    head.addEventListener("mousedown", (event) => {
        if (event.button !== 0) return;
        const rect = root.getBoundingClientRect();
        drag = { sx: event.clientX, sy: event.clientY, left: rect.left, top: rect.top, moved: false };
        head.style.cursor = "grabbing";
        event.preventDefault();
    });
    document.addEventListener("mousemove", (event) => {
        if (!drag || dead) return;
        const dx = event.clientX - drag.sx;
        const dy = event.clientY - drag.sy;
        if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
        drag.moved = true;
        const width = root.offsetWidth || 252;
        const height = root.offsetHeight || 150;
        const x = Math.min(Math.max(4, drag.left + dx), Math.max(4, window.innerWidth - width - 4));
        const y = Math.min(Math.max(4, drag.top + dy), Math.max(4, window.innerHeight - height - 4));
        root.style.left = x + "px";
        root.style.top = y + "px";
        root.style.right = "auto";
    });
    document.addEventListener("mouseup", () => {
        if (dead) { drag = null; return; }
        if (drag && !drag.moved) {
            setFold(body.style.display === "none" ? false : true);
        }
        drag = null;
        head.style.cursor = "grab";
    });

    buttons.start.addEventListener("click", (event) => { event.stopPropagation(); api.command = "start"; });
    buttons.regrade.addEventListener("click", (event) => { event.stopPropagation(); api.command = "regrade"; });
    buttons.stop.addEventListener("click", (event) => { event.stopPropagation(); api.command = "stop"; });

    const destroy = () => { dead = true; root.remove(); };
    const api = { version: VERSION, command: null, setStatus: setStatus, setBusy: setBusy, destroy: destroy };
    window.__cxAiMenu = api;
    // 关键：把构建好的菜单挂载到页面（否则节点只存在于内存中，永远不可见）
    host.appendChild(root);
    setBusy(false);
    return "created";
}
"""

AI_MENU_STATE_SCRIPT = r"""
([text, kind, busy]) => {
    const api = window.__cxAiMenu;
    if (!api) return false;
    api.setStatus(text, kind);
    api.setBusy(Boolean(busy));
    return true;
}
"""

AI_MENU_DESTROY_SCRIPT = r"""
() => {
    if (window.__cxAiMenu) {
        try { window.__cxAiMenu.destroy(); } catch (_error) {}
        window.__cxAiMenu = undefined;
    }
    const stale = document.getElementById("cx-ai-menu-root");
    if (stale) stale.remove();
    return true;
}
"""

AI_MENU_TAKE_COMMAND_SCRIPT = r"""
() => {
    const api = window.__cxAiMenu;
    if (!api) return null;
    const command = api.command;
    api.command = null;
    return command || null;
}
"""


class AIHomeworkAutomation:
    """页面菜单模式的 AI 批改：不占任务闸门，由宿主空闲轮询驱动。

    开关开启后，`service_menu` 在宿主 worker 空闲时被调用：负责向各页面注入
    「AI 批改助手」菜单、处理菜单命令（开始批改 / 停止 / 重新批改 / 结束）；
    批改结果只以批注形式插入题目下方，绝不自动选择或填写。
    """

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self.worker = owner.worker
        self.report_file = owner.runtime_dir / "ai_homework_report.json"
        # 菜单状态：开关（与 owner.ai_menu_enabled 同步）/ 忙标志 / 最近状态文本 / 注入登记
        self.menu_enabled = bool(getattr(owner, "ai_menu_enabled", False))
        self._next_menu_poll = 0.0
        self._menu_busy = False
        self._last_menu_status: tuple[str, str] = ("等待开启批改页面…", "info")
        self._stop_requested = False
        self._menu_seen_pages: set[int] = set()
        self._menu_failed_pages: dict[int, str] = {}

    # ------------------------------------------------------------------ 基础设施

    def _emit_grade(self, state: str, message: str) -> None:
        """批改进度事件：只更新插件页头部详情，不改变任务状态。"""
        self.worker.emit("chaoxing_grade_status", state=state, message=message)

    def _log(self, message: str, level: str = "info") -> None:
        self.worker.log(message, level)

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    def _context_pages(self) -> list[Any]:
        context = getattr(self.worker, "context", None)
        if context is None:
            return []
        return [page for page in context.pages if not page.is_closed()]

    # ------------------------------------------------------------------ 菜单开关与空闲轮询

    def set_menu_enabled(self, enabled: bool) -> None:
        """工作台开关：开启后仅在作业 / 章节测验类页面注入菜单，关闭即全部移除。"""
        self.menu_enabled = bool(enabled)
        self.owner.ai_menu_enabled = self.menu_enabled
        if enabled:
            self._menu_seen_pages = set()
            self._menu_failed_pages = {}
            self._ensure_menus([page for page in self._context_pages() if self._page_is_quiz(page)])
            self._set_menu_status("已开启。打开作业 / 章节测验页面，菜单会自动出现。", "info")
            self._log("页面批改菜单已开启：仅在作业 / 章节测验类页面显示，浏览器空闲时自动刷新。", "info")
        else:
            self._remove_menus()
            self._log("页面批改菜单已关闭。", "info")

    def service_menu(self) -> None:
        """宿主空闲轮询入口：检测测验页并注入/移除菜单 + 处理菜单命令（自带节流）。"""
        if not self.menu_enabled:
            return
        now = time.time()
        if now < self._next_menu_poll:
            return
        self._next_menu_poll = now + MENU_POLL_SECONDS
        pages = self._context_pages()
        if not pages:
            return
        # 只有作业 / 章节测验类页面显示菜单；离开测验页后菜单自动移除
        quiz_pages = []
        for page in pages:
            if self._page_is_quiz(page):
                quiz_pages.append(page)
            else:
                self._remove_menu_on(page)
        self._ensure_menus(quiz_pages)
        for page, command in self._drain_commands(quiz_pages):
            if command in {"start", "regrade"}:
                if self._menu_busy:
                    self._menu_status_on(page, "正在批改中：请等待完成，或点「停止」中断。", "warn")
                else:
                    self.grade_page_now(page, regrade=(command == "regrade"))
            elif command == "stop":
                if not self._menu_busy:
                    self._menu_status_on(page, "当前没有正在进行的批改。", "info")

    # ------------------------------------------------------------------ 页面菜单控制

    def _ensure_menus(self, pages: list[Any]) -> int:
        """确保每个标签页都有批改菜单；页面导航后 JS 环境重置，会在此自动重建。

        返回本次新注入的菜单数。每个标签页的首次注入与失败都写一条日志，
        便于排查「页面上没有出现菜单」的问题。
        """
        created_count = 0
        for page in pages:
            key = id(page)
            try:
                created = page.evaluate(AI_MENU_SCRIPT)
            except Exception as exc:
                if key not in self._menu_failed_pages:
                    self._menu_failed_pages[key] = str(exc)
                    self._log(
                        f"标签页注入批改菜单失败：{self._page_label(page)}（{str(exc)[:160]}）",
                        "warning",
                    )
                continue
            self._menu_failed_pages.pop(key, None)
            if created == "created":
                created_count += 1
                self._apply_menu_state(page)
                if key not in self._menu_seen_pages:
                    self._menu_seen_pages.add(key)
                    self._log(f"已在标签页注入「AI 批改助手」菜单：{self._page_label(page)}", "info")
        return created_count

    def _page_label(self, page: Any) -> str:
        try:
            title = self._text(page.title())
        except Exception:
            title = ""
        label = title or self._text(getattr(page, "url", "")) or "未知页面"
        return label[:64] + "…" if len(label) > 64 else label

    def _page_is_quiz(self, page: Any) -> bool:
        """页面是否为作业 / 章节测验类页面：任意帧 URL 命中 work 体系，或存在题目节点。"""
        for frame in page.frames:
            url = self._text(getattr(frame, "url", ""))
            if url and WORK_URL_PATTERN.search(url):
                return True
        try:
            return bool(page.evaluate("() => document.querySelectorAll('div.TiMu').length > 0"))
        except Exception:
            return False

    def _apply_menu_state(self, page: Any) -> None:
        text, kind = self._last_menu_status
        try:
            page.evaluate(AI_MENU_STATE_SCRIPT, [text, kind, self._menu_busy])
        except Exception:
            pass

    def _menu_apply_all(self, text: str, kind: str, busy: bool) -> None:
        for page in self._context_pages():
            try:
                page.evaluate(AI_MENU_STATE_SCRIPT, [text, kind, bool(busy)])
            except Exception:
                continue

    def _set_menu_status(self, text: str, kind: str = "info") -> None:
        """更新菜单状态文本并广播到所有标签页。"""
        self._last_menu_status = (text, kind)
        self._menu_apply_all(text, kind, self._menu_busy)

    def _menu_status_on(self, page: Any, text: str, kind: str) -> None:
        try:
            page.evaluate(AI_MENU_STATE_SCRIPT, [text, kind, self._menu_busy])
        except Exception:
            pass

    def _sync_menu_state(self) -> None:
        self._menu_apply_all(*self._last_menu_status, self._menu_busy)

    def _remove_menu_on(self, page: Any) -> None:
        try:
            page.evaluate(AI_MENU_DESTROY_SCRIPT)
        except Exception:
            pass

    def _remove_menus(self) -> None:
        for page in self._context_pages():
            self._remove_menu_on(page)

    def _drain_commands(self, pages: list[Any]) -> list[tuple[Any, str]]:
        """取出所有页面上待处理的菜单命令（取出即清空，一次性命令）。"""
        drained: list[tuple[Any, str]] = []
        for page in pages:
            try:
                command = page.evaluate(AI_MENU_TAKE_COMMAND_SCRIPT)
            except Exception:
                continue
            if isinstance(command, str) and command:
                drained.append((page, command))
        return drained

    def _check_menu_stop(self) -> None:
        """批改过程中检查「停止」请求；收到即中断本页批改。"""
        for _page, command in self._drain_commands(self._context_pages()):
            if command == "stop":
                raise _MenuStop()

    # ------------------------------------------------------------------ Agent 解析（本机设置 → 运行时快照）

    def _resolve_agent(self) -> dict[str, Any] | None:
        agent_id = self.owner.ai_settings_agent_id()
        agent = ai_manager.get_agent(agent_id) if agent_id else None
        if agent is None:
            agent = ai_manager.default_agent()
        if not agent:
            return None
        try:
            return ai_manager.resolve_agent_runtime(agent)
        except Exception:
            return None

    # ------------------------------------------------------------------ 单页批改（由菜单发起）

    def grade_page_now(self, page: Any, regrade: bool = False) -> None:
        """批改菜单所在的那一页：逐题作答 → 在题目下方批注参考答案（不自动填写、不提交）。"""
        agent = self._resolve_agent()
        if not agent or not str(agent.get("system_prompt") or "").strip() or not agent.get("provider"):
            message = "未获取到有效的 Agent 配置：请先在「Agent 管理」页创建 Agent，并在学习通助手页的「设置」（工具栏齿轮按钮）中选择。"
            self._set_menu_status(message, "err")
            self._emit_grade("error", message)
            self._log(message, "warning")
            return
        self._menu_busy = True
        self._stop_requested = False
        self._sync_menu_state()
        action = "重新批改" if regrade else "开始批改"
        try:
            if page.is_closed():
                raise _PageGone()
            try:
                page_title = self._text(page.title()) or "作业页面"
                url_before = self._text(page.url)
            except Exception:
                page_title = "作业页面"
                url_before = ""
            questions = self._extract_questions(page)
            if page.is_closed() or (url_before and self._text(page.url) != url_before):
                raise _PageGone()
            if not questions:
                message = f"「{page_title}」未找到题目：请打开作业 / 章节测验作答页（含题目列表）后再试。"
                self._set_menu_status("当前页面未找到题目，请打开作业 / 章节测验作答页后再试。", "warn")
                self._emit_grade("error", message)
                self._log(message, "warning")
                return
            self._check_menu_stop()
            self._set_menu_status(f"已提取 {len(questions)} 道题，开始逐题作答…", "busy")
            self._emit_grade("running", f"{action}「{page_title}」：已提取 {len(questions)} 道题，逐题作答中…")
            answers, explains = self._ask_ai(questions, agent, page_title)
            self._check_menu_stop()
            if page.is_closed() or (url_before and self._text(page.url) != url_before):
                raise _PageGone()
            self._set_menu_status("正在逐题标注参考答案（不自动填写）…", "busy")
            details = self._annotate_answers(page, questions, answers, explains)
            annotated = sum(1 for item in details if item.get("ok"))
            answered_by_user = sum(1 for question in questions if self._user_answer_text(question))
            unanswered = len(questions) - answered_by_user
            try:
                page.bring_to_front()
            except Exception:
                pass
            self._append_report({
                "title": page_title,
                "url": self._text(page.url),
                "question_count": len(questions),
                "annotated": annotated,
                "user_answered": answered_by_user,
                "user_unanswered": unanswered,
                "status": "done",
                "graded_at": now_text(),
            })
            menu_summary = (
                f"批改完成：共 {len(questions)} 题（你已作答 {answered_by_user}、未作答 {unanswered}），"
                "AI 参考答案已逐题标注在题目下方。"
            )
            self._set_menu_status(menu_summary, "ok")
            message = (
                f"「{page_title}」批改完成：共 {len(questions)} 题——你已作答 {answered_by_user}、未作答 {unanswered}"
                f"（未作答的题用橙色批注标出）；标注参考答案 {annotated}/{len(questions)} 题；"
                "未自动填写任何内容，提交由你完成。"
            )
            self._emit_grade("done", message)
            self._log(message, "success")
            try:
                self.worker.spider_celebrate(page)
            except Exception:
                pass
        except _MenuStop:
            message = "已停止：本页未插入批注。"
            self._set_menu_status(message, "warn")
            self._emit_grade("error", f"「{page_title}」已手动停止批改。")
            self._log(f"学习通 AI 批改：「{page_title}」已手动停止。", "warning")
        except _PageGone:
            # 用户在批改中关闭或跳转页面：安全中断，不影响菜单在其他页面继续可用
            message = "页面已关闭或跳转，本页批改中断；打开新的作业 / 章节测验页面可继续批改。"
            self._set_menu_status(message, "warn")
            self._emit_grade("error", f"「{page_title}」批改中断：页面已关闭或跳转。")
            self._log(f"学习通 AI 批改：「{page_title}」页面已关闭或跳转，本页批改中断。", "warning")
        except Exception as exc:
            self._set_menu_status(f"本页批改失败：{exc}", "err")
            self._emit_grade("error", f"{action}「{page_title}」失败（{exc}），可在页面上重试。")
            self._log(f"学习通 AI 批改：{action}失败：{exc}", "error")
        finally:
            self._menu_busy = False
            self._stop_requested = False
            self._sync_menu_state()

    def _append_report(self, entry: dict[str, Any]) -> None:
        """把单页批改结果追加进本机报告文件（保留历史记录）。"""
        payload: dict[str, Any] = {}
        try:
            existing = json.loads(self.report_file.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                payload = existing
        except (OSError, ValueError):
            payload = {}
        payload.setdefault("assignments", []).append(entry)
        payload["updated_at"] = now_text()
        self.owner._write_json(self.report_file, payload)

    # ------------------------------------------------------------------ 题目提取

    def _extract_questions(self, page: Any) -> list[dict[str, Any]]:
        """在页面的所有帧里提取 div.TiMu 题目（含题型/选项/已选/材料/小题拆分）。"""
        questions: list[dict[str, Any]] = []
        for frame in page.frames:
            try:
                items = frame.evaluate(QUESTION_EXTRACTION_SCRIPT)
            except Exception:
                continue
            if not isinstance(items, list) or not items:
                continue
            frame_url = self._text(getattr(frame, "url", ""))
            for item in items:
                if not isinstance(item, dict):
                    continue
                questions.append({**item, "frame_url": frame_url})
                if len(questions) >= MAX_QUESTIONS_PER_ASSIGNMENT:
                    break
            if len(questions) >= MAX_QUESTIONS_PER_ASSIGNMENT:
                break
        for position, question in enumerate(questions, start=1):
            question["index"] = position
        return questions

    # ------------------------------------------------------------------ 逐题作答（AI）

    def _ask_ai(
        self,
        questions: list[dict[str, Any]],
        agent: dict[str, Any],
        title: str,
    ) -> tuple[dict[int, Any], dict[int, str]]:
        """逐题解答：每题单独请求 Agent，菜单实时显示进度；单题漏答自动补答一轮。

        作答按三级通道降级：LangChain 结构化输出（Pydantic schema 强制固定格式）→
        接口级 JSON 模式（response_format=json_object）→ 普通调用 + 文本兜底解析，
        保证每道题都能拿到参考答案。
        """
        answers: dict[int, Any] = {}
        explains: dict[int, str] = {}
        total = len(questions)
        for position, question in enumerate(questions, start=1):
            stem_head = str(question.get("stem") or "")[:24]
            message = f"逐题作答 {position}/{total}：{stem_head}…"
            self._set_menu_status(message, "busy")
            self._emit_grade("running", f"{title}：{message}")
            parsed, parsed_explains = self._ask_one(agent, question)
            if not parsed:
                # 补答：允许直接文字回答（补答提示词不再强制 JSON）
                reply = ai_manager.run_agent(
                    agent,
                    self._build_missing_prompt([question]),
                    timeout=AI_TIMEOUT_SECONDS,
                    max_tokens=AI_MAX_TOKENS,
                )
                parsed, parsed_explains = self._parse_answers(reply, [question])
            answers.update(parsed)
            explains.update(parsed_explains)
            if position < total:
                self._check_menu_stop()
        return answers, explains

    def _ask_one(self, agent: dict[str, Any], question: dict[str, Any]) -> tuple[dict[int, Any], dict[int, str]]:
        """单题作答：结构化通道优先，失败降级 JSON 模式；解析链见 _parse_answers。"""
        prompt = self._build_user_prompt([question])
        if ai_chain.structured_available():
            try:
                items = ai_chain.run_structured(
                    agent,
                    prompt,
                    timeout=AI_TIMEOUT_SECONDS,
                    max_tokens=AI_MAX_TOKENS,
                )
                parsed, explains = self._parse_structured(items, [question])
                if parsed:
                    return parsed, explains
            except Exception as exc:
                self._log(
                    f"第 {question['index']} 题结构化输出失败，降级普通调用（{str(exc)[:90]}）",
                    "warning",
                )
        reply = ai_manager.run_agent(
            agent,
            prompt,
            timeout=AI_TIMEOUT_SECONDS,
            max_tokens=AI_MAX_TOKENS,
            json_mode=True,
        )
        return self._parse_answers(reply, [question])

    def _parse_structured(
        self,
        items: list[dict[str, Any]],
        chunk: list[dict[str, Any]],
    ) -> tuple[dict[int, Any], dict[int, str]]:
        """解析 LangChain 结构化输出（已由 Pydantic 校验格式），只保留本轮题号。"""
        valid_indexes = {int(question["index"]) for question in chunk}
        answers: dict[int, Any] = {}
        explains: dict[int, str] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            if index not in valid_indexes:
                continue
            answer = str(item.get("answer") or "").strip()
            if not answer:
                continue
            answers[index] = answer
            explain = str(item.get("explain") or "").strip()
            if explain:
                explains[index] = explain
        return answers, explains

    @staticmethod
    def _question_prompt_lines(chunk: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        last_material: str | None = None
        for question in chunk:
            material = str(question.get("material") or "")
            if material:
                if material != last_material:
                    lines.append(f"【材料】{material}")
                last_material = material
            else:
                last_material = None
            head = f"第{question['index']}题（{question.get('type') or '未知'}）"
            if question.get("blank_count"):
                head += f"，共 {question['blank_count']} 空"
            lines.append(head + f"：{question.get('stem') or ''}")
            for option in question.get("options") or []:
                key = str(option.get("key") or "")
                text = str(option.get("text") or "")
                lines.append(f"{key}. {text}" if key else f"- {text}")
        return lines

    @classmethod
    def _build_user_prompt(cls, chunk: list[dict[str, Any]]) -> str:
        lines = [
            f"请作答以下 {len(chunk)} 道题。要求：每一题都必须给出答案，不确定时给出最可能的猜测"
            "（禁止留空、禁止答「无法确定」）；explain 用一句话（不超过 40 字）说明理由。"
            '只输出 JSON：{"answers":[{"index":1,"answer":"B","explain":"……"}]}',
        ]
        lines.extend(cls._question_prompt_lines(chunk))
        return "\n".join(lines)

    @classmethod
    def _build_missing_prompt(cls, chunk: list[dict[str, Any]]) -> str:
        lines = [
            f"你上一轮没有为以下 {len(chunk)} 道题给出答案。请必须给出你认为最可能的答案"
            "（禁止留空、禁止答「无法确定」）。直接回答即可：先写「答案：X」（X 为选项字母或答案内容），"
            "再用一句话（不超过 40 字）解释理由；也可以只输出 JSON："
            '{"answers":[{"index":1,"answer":"B","explain":"……"}]}',
        ]
        lines.extend(cls._question_prompt_lines(chunk))
        return "\n".join(lines)

    def _parse_answers(self, reply: str, chunk: list[dict[str, Any]]) -> tuple[dict[int, Any], dict[int, str]]:
        data = ai_manager.extract_json(reply)
        items: list[Any] = []
        if isinstance(data, dict):
            items = data.get("answers") or []
        elif isinstance(data, list):
            items = data
        valid_indexes = {int(question["index"]) for question in chunk}
        answers: dict[int, Any] = {}
        explains: dict[int, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            if index not in valid_indexes:
                continue
            answer = item.get("answer")
            if answer is None or answer == "":
                continue
            answers[index] = answer
            explain = str(item.get("explain") or item.get("why") or item.get("reason") or "").strip()
            if explain:
                explains[index] = explain
        if not answers and len(chunk) == 1:
            # 单题兜底：模型没按 JSON 回复（直接用文字作答）时，从纯文本里提取答案
            question = chunk[0]
            fallback_answers, fallback_explains = self._parse_single_fallback(reply, question)
            if fallback_answers:
                self._log(
                    f"第 {question['index']} 题的回复未按 JSON 解析，已用文本兜底提取答案"
                    f"（回复开头：{self._text(reply)[:60]}）",
                    "warning",
                )
            return fallback_answers, fallback_explains
        return answers, explains

    def _parse_single_fallback(self, reply: str, question: dict[str, Any]) -> tuple[dict[int, Any], dict[int, str]]:
        """单题文本兜底解析：尽力从纯文字回复里提取答案，主观题直接采用回复正文。"""
        index = int(question["index"])
        compact = self._text(reply).replace("\n", " ")
        if not compact:
            return {}, {}
        options = question.get("options") or []
        qtype = str(question.get("type") or "")
        answer: Any = None
        if options:
            keys = {str(option.get("key") or "").upper() for option in options if option.get("key")}
            match = re.search(r"(?:答案|正确答案|选|answer)\s*[:：]?\s*([A-Ha-h]{1,8})", compact, re.IGNORECASE)
            if match and match.group(1).upper() in keys:
                answer = match.group(1).upper()
            else:
                for candidate in re.findall(r"(?<![A-Za-z])([A-Ha-h])(?![A-Za-z])", compact):
                    if candidate.upper() in keys:
                        answer = candidate.upper()
                        break
            if answer is None and qtype == "判断":
                judge = re.search(r"(正确|错误|√|×|true|false)", compact, re.IGNORECASE)
                if judge:
                    answer = judge.group(1)
        elif qtype in {"填空", "简答"} or not options:
            body = re.sub(r"^(答案|参考答案|解释|回答)\s*[:：]\s*", "", compact).strip()
            if body and not body.startswith("{"):
                answer = body[:200]
        if not answer:
            return {}, {}
        explain = ""
        explain_match = re.search(r"解释[：:]\s*([^。；;\n]{4,60})", compact)
        if explain_match:
            explain = explain_match.group(1).strip()
        explains = {index: explain} if explain else {}
        return {index: answer}, explains

    # ------------------------------------------------------------------ 批注（不自动填写）

    def _annotate_answers(
        self,
        page: Any,
        questions: list[dict[str, Any]],
        answers: dict[int, Any],
        explains: dict[int, str] | None = None,
    ) -> list[dict[str, Any]]:
        """在每道题（阅读理解为每道小题）下方插入 AI 参考答案批注；绝不自动选择或填写。"""
        explains = explains or {}
        details: list[dict[str, Any]] = []
        by_frame: dict[str, list[dict[str, Any]]] = {}
        for question in questions:
            by_frame.setdefault(str(question.get("frame_url") or ""), []).append(question)

        for frame in page.frames:
            group = by_frame.get(self._text(getattr(frame, "url", "")))
            if not group:
                continue
            notes: list[dict[str, Any]] = []
            for question in group:
                index = int(question.get("index") or 0)
                answer = answers.get(index)
                explain = str(explains.get(index) or "").strip()
                user_value = self._user_answer_text(question)
                qtype = str(question.get("type") or "")
                if answer is None:
                    notes.append({
                        "mu_index": question.get("mu_index"),
                        "sub_index": question.get("sub_index"),
                        "cls": "warn",
                        "text": "「AI 参考答案」AI 未能给出该题答案，请自行作答。",
                    })
                    details.append({"index": index, "type": qtype, "answer": None, "ok": False, "user_answered": bool(user_value)})
                    continue
                agree = bool(user_value) and user_value.upper() == self._answer_letters(answer)
                notes.append({
                    "mu_index": question.get("mu_index"),
                    "sub_index": question.get("sub_index"),
                    "cls": "ok" if agree else "warn",
                    "text": self._note_lines(
                        self._display_answer(answer),
                        explain,
                        self._user_state_line(question, answer),
                        "仅供参考，不自动填写或提交",
                    ),
                })
                details.append({
                    "index": index,
                    "type": qtype,
                    "answer": answer,
                    "ok": True,
                    "user_answered": bool(user_value),
                    "readonly": bool(question.get("readonly")),
                })
            try:
                frame.evaluate(ANNOTATE_SCRIPT, notes)
            except Exception as exc:
                self._log(f"插入批注失败：{exc}", "warning")
        return details

    # ------------------------------------------------------------------ 你的作答状态与批注文本

    @staticmethod
    def _user_answer_text(question: dict[str, Any]) -> str:
        """用户在本题的作答内容：选项字母 / 填空内容 / 已批阅页的「我的答案」；空串表示未作答。"""
        choice = str(question.get("selected_keys") or "").strip()
        if choice:
            return choice
        blanks = str(question.get("blank_values") or "").strip()
        if blanks:
            return blanks[:80]
        user_text = str(question.get("user_answer") or "").strip()
        if user_text and user_text not in {"--", "——", "未作答", "无"}:
            return user_text[:40]
        return ""

    def _user_state_line(self, question: dict[str, Any], answer: Any) -> str:
        """「你的答案」批注行：明确区分 已作答（与 AI 一致/不一致）与 未作答。"""
        user_value = self._user_answer_text(question)
        if not user_value:
            return "你的答案：未作答"
        line = f"你的答案：{user_value}"
        try:
            if user_value.upper() == self._answer_letters(answer):
                return line + "（与 AI 一致）"
            if len(user_value) <= 8 and user_value.isalpha() and self._answer_letters(answer).isalpha():
                return line + "（与 AI 不一致）"
        except Exception:
            pass
        return line

    @staticmethod
    def _note_lines(answer_display: str, explain: str, user_state: str, tail: str) -> str:
        """批注文本（插入在题目/小题下方）：答案 + 解释 + 你的作答状态 + 提示，各占一行。"""
        lines = [f"「AI 参考答案」{answer_display}"]
        if explain:
            lines.append(f"解释：{explain}")
        if user_state:
            lines.append(user_state)
        if tail:
            lines.append(tail)
        return "\n".join(lines)

    @staticmethod
    def _display_answer(answer: Any) -> str:
        if isinstance(answer, list):
            return " ｜ ".join(str(item) for item in answer)
        return str(answer)

    @staticmethod
    def _answer_letters(answer: Any) -> str:
        if isinstance(answer, list):
            return "".join(str(item).strip().upper() for item in answer)
        return str(answer).strip().upper()
