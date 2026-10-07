"""抽取注入页面的 JS 字符串常量并用 node --check 校验语法。

覆盖工作台蜘蛛特效脚本与各插件里的页面注入脚本（字符串拼接常量同样支持）。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile

# (相对仓库根的文件, 需要校验的常量名)
TARGETS = {
    os.path.join("workbench", "spider_scripts.py"): (
        "SPIDER_OVERLAY_SCRIPT",
        "SPIDER_COLLECTOR_HARVEST",
        "SPIDER_COLLECTOR_DESTROY",
    ),
    os.path.join("plugins", "chaoxing_assistant", "ai_homework.py"): (
        "QUESTION_EXTRACTION_SCRIPT",
        "ANNOTATE_SCRIPT",
        "AI_MENU_SCRIPT",
        "AI_MENU_STATE_SCRIPT",
        "AI_MENU_DESTROY_SCRIPT",
        "AI_MENU_TAKE_COMMAND_SCRIPT",
    ),
}


def _const_value(node: ast.AST, env: dict[str, str] | None = None):
    """解出赋值右侧的字符串常量（兼容 r-string、字符串拼接、常量名引用与 .strip()）。"""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _const_value(node.left, env)
        right = _const_value(node.right, env)
        if left is not None and right is not None:
            return left + right
        return None
    if isinstance(node, ast.Name) and env is not None and node.id in env:
        return env[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "strip":
        node = node.func.value
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _collect_constants(src_path: str, names: tuple[str, ...]) -> dict[str, str]:
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    wanted = set(names)
    env: dict[str, str] = {}
    consts: dict[str, str] = {}
    # 多轮解析：先收直连常量，再解开「常量名 + 字符串」这类拼接引用
    for _round in range(3):
        progress = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                if not isinstance(t, ast.Name) or t.id in env:
                    continue
                value = _const_value(node.value, env)
                if value is None:
                    continue
                env[t.id] = value
                if t.id in wanted:
                    consts[t.id] = value
                progress = True
        if not progress:
            break
    return consts


def main() -> int:
    root = os.path.join(os.path.dirname(__file__), "..")
    failed = False
    for relative, names in TARGETS.items():
        src_path = os.path.normpath(os.path.join(root, relative))
        if not os.path.exists(src_path):
            print(f"{relative}: SKIP（文件不存在）")
            continue
        consts = _collect_constants(src_path, names)
        missing = [name for name in names if name not in consts]
        if missing:
            print(f"{relative} MISSING: " + ", ".join(missing))
            failed = True
        for name, js in consts.items():
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
                f.write(js)
                path = f.name
            try:
                r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
                status = "OK" if r.returncode == 0 else "FAIL"
                if r.returncode:
                    failed = True
                    print(r.stderr[:4000])
                print(f"{relative}::{name}: {status}")
            finally:
                os.unlink(path)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
