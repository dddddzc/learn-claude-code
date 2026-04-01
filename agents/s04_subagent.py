#!/usr/bin/env python3
# Harness: context isolation -- protecting the model's clarity of thought.
"""
s04_subagent.py - Subagents

Spawn a child agent with fresh messages=[]. The child works in its own
context, sharing the filesystem, then returns only a summary to the parent.

    Parent agent                     Subagent
    +------------------+             +------------------+
    | messages=[...]   |             | messages=[]      |  <-- fresh
    |                  |  dispatch   |                  |
    | tool: task       | ---------->| while tool_use:  |
    |   prompt="..."   |            |   call tools     |
    |   description="" |            |   append results |
    |                  |  summary   |                  |
    |   result = "..." | <--------- | return last text |
    +------------------+             +------------------+
              |
    Parent context stays clean.
    Subagent context is discarded.

Key insight: "Process isolation gives context isolation for free."
"""

import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# 加载 .env；允许用本地配置覆盖系统环境变量，便于在不同机器快速切换。
load_dotenv(override=True)

# 使用自定义网关时，移除可能与网关鉴权机制冲突的 token 变量。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 代理可操作的根目录固定为当前工作目录，所有文件操作都将被约束在此范围。
WORKDIR = Path.cwd()
# 初始化模型客户端，可选走官方地址或自定义 base_url。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 从环境变量读取模型标识；缺失时让程序尽早失败，便于排查配置问题。
MODEL = os.environ["MODEL_ID"]

# 父代理系统提示：允许调用 task，把复杂任务拆分给子代理。
SYSTEM = f"You are a coding agent at {WORKDIR}. Use the task tool to delegate exploration or subtasks."
# 子代理系统提示：聚焦执行被委派任务，并返回简要结论给父代理。
SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."


# -- Tool implementations shared by parent and child --
def safe_path(p: str) -> Path:
    # 将相对路径解析到绝对路径，并阻断越界访问（如 ../）。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    # 对高危命令做最小拦截，降低误操作风险。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # 在工作目录执行命令，统一捕获 stdout/stderr，避免阻塞设置超时。
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        # 限制最大返回体积，防止把超长输出注入模型上下文。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        # 按行读取文本文件，支持通过 limit 控制返回行数。
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            # 返回截断提示，明确还有多少行未展示。
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        # 自动创建父目录，允许一次性写入深层新路径。
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        # 仅允许精确替换已存在文本，避免无意改动。
        if old_text not in content:
            return f"Error: Text not found in {path}"
        # 只替换第一个匹配项，控制改动范围。
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


TOOL_HANDLERS = {
    # 统一工具分发表：根据工具名调用对应实现。
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

# Child gets all base tools except task (no recursive spawning)
CHILD_TOOLS = [
    # 子代理仅保留基础工具能力，不包含 task，避免递归派生子代理造成失控。
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


# -- Subagent: fresh context, filtered tools, summary-only return --
def run_subagent(prompt: str) -> str:
    # 子代理从全新消息历史开始，只接收父代理传入的任务提示。
    sub_messages = [{"role": "user", "content": prompt}]  # fresh context
    for _ in range(30):  # safety limit
        # 在独立上下文里循环执行：请求模型 -> 执行工具 -> 回填 tool_result。
        response = client.messages.create(
            model=MODEL,
            system=SUBAGENT_SYSTEM,
            messages=sub_messages,
            tools=CHILD_TOOLS,
            max_tokens=8000,
        )
        # 保存子代理本轮输出，供下一轮继续推理。
        sub_messages.append({"role": "assistant", "content": response.content})
        # 模型不再请求工具时，说明子任务已产出最终文本。
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # 与父代理共享同一套本地工具实现与文件系统访问能力。
                handler = TOOL_HANDLERS.get(block.name)
                output = (
                    handler(**block.input) if handler else f"Unknown tool: {block.name}"
                )
                # 工具结果按 Anthropic 协议回传给对应 tool_use_id。
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output)[:50000],
                    }
                )
        # 以 user 角色追加工具结果，驱动子代理进入下一轮决策。
        sub_messages.append({"role": "user", "content": results})
    # Only the final text returns to the parent -- child context is discarded
    return (
        "".join(b.text for b in response.content if hasattr(b, "text"))
        or "(no summary)"
    )


# -- Parent tools: base tools + task dispatcher --
PARENT_TOOLS = CHILD_TOOLS + [
    # 父代理额外暴露 task 工具：把复杂工作拆成子任务并交由子代理执行。
    {
        "name": "task",
        "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "description": {
                    "type": "string",
                    "description": "Short description of the task",
                },
            },
            "required": ["prompt"],
        },
    },
]


def agent_loop(messages: list):
    while True:
        # 父代理主循环：与模型对话并根据 tool_use 结果持续推进任务。
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=PARENT_TOOLS,
            max_tokens=8000,
        )
        # 记录 assistant 原始返回，保证上下文完整可追溯。
        messages.append({"role": "assistant", "content": response.content})
        # 无工具调用即视为当前用户请求处理完成。
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "task":
                    # task 工具触发子代理：父代理只接收摘要，不继承子代理中间过程。
                    desc = block.input.get("description", "subtask")
                    print(f"> task ({desc}): {block.input['prompt'][:80]}")
                    output = run_subagent(block.input["prompt"])
                else:
                    # 普通工具直接由父代理执行。
                    handler = TOOL_HANDLERS.get(block.name)
                    output = (
                        handler(**block.input)
                        if handler
                        else f"Unknown tool: {block.name}"
                    )
                print(f"  {str(output)[:200]}")
                # 将每个工具结果关联回原始 tool_use_id，供模型继续推理。
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    }
                )
        # 以 user 角色回注工具结果，形成下一轮输入。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # history 存放父代理会话历史（用户输入、assistant 输出与工具结果）。
    history = []
    while True:
        try:
            # 使用彩色前缀区分当前示例脚本的输入提示。
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        # 支持空输入或退出指令快速结束程序。
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 先追加用户消息，再交给父代理循环处理。
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 取最后一条 assistant 内容并打印可见文本块。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
