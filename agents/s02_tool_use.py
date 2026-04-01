#!/usr/bin/env python3
# Harness: tool dispatch -- expanding what the model can reach.
"""
s02_tool_use.py - Tools

The agent loop from s01 didn't change. We just added tools to the array
and a dispatch map to route calls.

    +----------+      +-------+      +------------------+
    |   User   | ---> |  LLM  | ---> | Tool Dispatch    |
    |  prompt  |      |       |      | {                |
    +----------+      +---+---+      |   bash: run_bash |
                          ^          |   read: run_read |
                          |          |   write: run_wr  |
                          +----------+   edit: run_edit |
                          tool_result| }                |
                                     +------------------+

Key insight: "The loop didn't change at all. I just added tools."
"""

import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# 先加载 .env，保证后续读取配置时拿到最新环境变量。
load_dotenv(override=True)

# 使用自定义网关时，清理可能冲突的认证变量。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 工作目录作为所有文件工具的安全根目录。
WORKDIR = Path.cwd()
# 初始化模型客户端与模型 ID。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# 系统提示词：要求优先通过工具行动，而非长篇解释。
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."


def safe_path(p: str) -> Path:
    # 将相对路径解析到工作区，统一转为绝对路径（依靠路径拼接 /）。
    path = (WORKDIR / p).resolve()
    # 防止路径穿越（例如 ../../）逃逸到工作区外：即 path 是否在 WORKDIR 内。
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    # 最小化风险：拦截明显破坏性命令。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # 在工作区执行命令，收集 stdout/stderr，并限制执行时长。
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        # 合并输出并做长度截断，避免上下文消息过大。
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        # 读取文本文件并按行处理，便于做行数限制。
        text = safe_path(path).read_text()
        lines = text.splitlines()
        # 指定 limit 时只返回前 N 行，并给出省略提示。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        # 最终再做一次整体长度截断，防止极端大文件输出。
        return "\n".join(lines)[:50000]
    except Exception as e:
        # 工具层统一返回字符串错误，便于直接塞回 tool_result。
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        # 写文件前确保路径安全，并自动创建父目录。
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        # 返回可观察结果，帮助模型确认写入是否成功。
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        # 精确替换：只替换首次匹配，避免全量误改。
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# -- 工具分发表：把模型请求的 tool name 映射到本地处理函数 --
# 统一使用 lambda 做轻量参数适配：
# 1) 保持 handler 接口一致（kwargs）
# 2) 将模型输入字段映射到具体函数签名
TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

# 提供给模型的工具声明（JSON Schema）。
# 这里定义了每个工具的入参结构，模型会按该契约构造 tool_use。
TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
]


def agent_loop(messages: list):
    # 核心循环：模型回合 -> 可能触发工具 -> 回灌 tool_result -> 继续。
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        # 先记录 assistant 回合（含文本与 tool_use 块）。
        messages.append({"role": "assistant", "content": response.content})
        # 没有工具调用则本轮结束，返回主交互层。
        if response.stop_reason != "tool_use":
            return
        # 聚合本轮所有工具执行结果，准备一次性回灌。
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # 根据工具名查分发函数；未知工具返回可读错误。
                handler = TOOL_HANDLERS.get(block.name)
                output = (
                    handler(**block.input) if handler else f"Unknown tool: {block.name}"
                )
                # 控制台打印简短预览，便于人类观察执行轨迹。
                print(f"> {block.name}: {output[:200]}")
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": output}
                )
        # 将工具结果作为用户回合喂回模型，驱动下一次推理。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # history 保存整个会话上下文，支持多轮连续任务。
    history = []
    while True:
        try:
            query = input("\033[36ms02 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        # 空输入或退出命令结束会话。
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 追加用户输入并启动一次完整 agent_loop。
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 读取本轮最后 assistant 内容并打印文本块。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
