#!/usr/bin/env python3
# Harness: compression -- clean memory for infinite sessions.
"""
s06_context_compact.py - Compact

Three-layer compression pipeline so the agent can work forever:

    Every turn:
    +------------------+
    | Tool call result |
    +------------------+
            |
            v
    [Layer 1: micro_compact]        (silent, every turn)
      Replace tool_result content older than last 3
      with "[Previous: used {tool_name}]"
            |
            v
    [Check: tokens > 50000?]
       |               |
       no              yes
       |               |
       v               v
    continue    [Layer 2: auto_compact]
                  Save full transcript to .transcripts/
                  Ask LLM to summarize conversation.
                  Replace all messages with [summary].
                        |
                        v
                [Layer 3: compact tool]
                  Model calls compact -> immediate summarization.
                  Same as auto, triggered manually.

Key insight: "The agent can forget strategically and keep working forever."
"""

import json
import os
import subprocess
import time
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# 加载环境变量（如 MODEL_ID、ANTHROPIC_BASE_URL），覆盖进程里已有同名值。
load_dotenv(override=True)

# 当使用自定义网关时，移除可能冲突的鉴权变量，避免双重认证导致请求失败。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 工作目录作为工具可访问的根目录。
WORKDIR = Path.cwd()
# 初始化 LLM 客户端，可通过环境变量动态切换官方/代理网关。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 模型名称由外部注入，便于实验不同模型而无需改代码。
MODEL = os.environ["MODEL_ID"]

# 系统提示保持精简，把“行为策略”交给代码层（压缩/工具）控制。
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."

# 触发自动压缩的上下文阈值（近似 token 计数）。
THRESHOLD = 50000
# 对话转储目录：每次大压缩前先落盘完整会话，保证可追溯。
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
# 微压缩阶段保留最近多少条 tool_result 原文。
KEEP_RECENT = 3


def estimate_tokens(messages: list) -> int:
    """Rough token count: ~4 chars per token."""
    # 这里使用经验公式做快速估算，不追求精确，只用于触发压缩决策。
    return len(str(messages)) // 4


# -- Layer 1: micro_compact - replace old tool results with placeholders --
def micro_compact(messages: list) -> list:
    # Collect (msg_index, part_index, tool_result_dict) for all tool_result entries
    # 第一步：扫描整段会话，收集所有工具结果块（tool_result）。
    # 记录消息索引和分片索引，便于后续精确就地修改。
    tool_results = []
    for msg_idx, msg in enumerate(messages):
        # 约定中，tool_result 是以 user 角色、content=list 的形式回填。
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part_idx, part in enumerate(msg["content"]):
                # 只提取结构化 tool_result，忽略普通文本片段。
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))
    # 若工具结果条数不超过保留上限，直接返回，不做压缩。
    if len(tool_results) <= KEEP_RECENT:
        return messages
    # Find tool_name for each result by matching tool_use_id in prior assistant messages
    # 第二步：建立 tool_use_id -> tool_name 映射。
    # 这样替换老结果时可以保留“当时调用了哪个工具”的关键信息。
    tool_name_map = {}
    for msg in messages:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    # assistant 的 tool_use 块里包含 id 和 name。
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name_map[block.id] = block.name
    # Clear old results (keep last KEEP_RECENT)
    # 第三步：仅压缩“较早”的 tool_result，最近 KEEP_RECENT 条保持原文。
    to_clear = tool_results[:-KEEP_RECENT]
    for _, _, result in to_clear:
        # 只压缩字符串且较长的结果；短结果保留原样，避免过度损失信息。
        if isinstance(result.get("content"), str) and len(result["content"]) > 100:
            tool_id = result.get("tool_use_id", "")
            # 若映射缺失（极端/异常情况），回退到 unknown。
            tool_name = tool_name_map.get(tool_id, "unknown")
            # 用占位符替代大段输出：降低上下文体积，同时保留行为轨迹。
            result["content"] = f"[Previous: used {tool_name}]"
    # 函数原地修改 messages 后(其实修改的是 tool_clear)返回，便于链式调用。
    return messages


# -- Layer 2: auto_compact - save transcript, summarize, replace messages --
def auto_compact(messages: list) -> list:
    # Save full transcript to disk
    # 第一步：先把完整会话落盘，防止压缩后丢失可追溯细节。
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            # default=str 用于兜底序列化非 JSON 原生对象（如 SDK block 对象）。
            f.write(json.dumps(msg, default=str) + "\n")
    print(f"[transcript saved: {transcript_path}]")
    # Ask LLM to summarize
    # 第二步：把当前会话转成可摘要文本；这里做长度截断，避免摘要请求本身过大。
    conversation_text = json.dumps(messages, default=str)[:80000]
    response = client.messages.create(
        model=MODEL,
        messages=[
            {
                "role": "user",
                # 摘要提示词固定要求：已完成事项、当前状态、关键决策。
                "content": "Summarize this conversation for continuity. Include: "
                "1) What was accomplished, 2) Current state, 3) Key decisions made. "
                "Be concise but preserve critical details.\n\n" + conversation_text,
            }
        ],
        # 控制摘要长度上限，避免“压缩结果”再次膨胀。
        max_tokens=2000,
    )
    # 读取摘要文本（约定第一个 content 块为文本）。
    summary = response.content[0].text
    # Replace all messages with compressed summary
    # 第三步：用“摘要后的最小上下文”替换原始的所有原会话。
    # 1) user 条目携带摘要正文与转储文件路径。
    # 2) assistant 条目提供确认语句，帮助下一轮对齐对话状态。
    return [
        {
            "role": "user",
            "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}",
        },
        {
            "role": "assistant",
            "content": "Understood. I have the context from the summary. Continuing.",
        },
    ]


# -- Tool implementations --
def safe_path(p: str) -> Path:
    # 先拼接到工作目录，再 resolve 成绝对路径。
    path = (WORKDIR / p).resolve()
    # 强制路径必须位于工作区内，防止 "../" 目录穿越读写到外部。
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    # 以最小黑名单拦截高风险命令；示例目的，不是完整沙箱。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # 在工作区执行命令，统一抓取 stdout/stderr 并设置 120s 超时。
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        # 限制单次输出大小，防止日志污染上下文。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        # 以行粒度读取，便于按 limit 做前 N 行截断。
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        # 仍做总长度上限保护，避免大文件一次性塞满上下文。
        return "\n".join(lines)[:50000]
    except Exception as e:
        # 工具层统一返回字符串错误，不抛异常到主循环。
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        # 自动创建父目录，减少调用方前置判断。
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        # 保持和其他工具一致的错误返回格式。
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        # 要求 old_text 精确命中，避免“猜测式”改写。
        if old_text not in content:
            return f"Error: Text not found in {path}"
        # 仅替换首个匹配，尽量把编辑范围最小化。
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


TOOL_HANDLERS = {
    # 模型工具名到本地执行函数的分发表。
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # compact 本身不直接执行压缩，真正压缩在主循环里由 manual_compact 触发。
    "compact": lambda **kw: "Manual compression requested.",
}

TOOLS = [
    # 暴露给模型的工具定义（名称、说明、输入 schema）。
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
    {
        "name": "compact",
        "description": "Trigger manual conversation compression.",
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "What to preserve in the summary",
                }
            },
        },
    },
]


def agent_loop(messages: list):
    while True:
        # Layer 1: micro_compact before each LLM call
        micro_compact(messages)
        # Layer 2: auto_compact if token estimate exceeds threshold
        if estimate_tokens(messages) > THRESHOLD:
            print("[auto_compact triggered]")
            # 原地替换会话历史，保留同一个 list 引用给外层调用者。
            messages[:] = auto_compact(messages)
        # 发起一次模型推理，允许其按需发起工具调用。
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        # 先写入 assistant 原始输出（含 text/tool_use block）。
        messages.append({"role": "assistant", "content": response.content})
        # 若不是 tool_use 停止，说明模型已直接给出最终文本回答。
        if response.stop_reason != "tool_use":
            return
        # 收集本轮所有 tool_result，随后一次性回填给模型。
        results = []
        # 标记是否触发了“手动压缩”工具。
        manual_compact = False
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compact":
                    # 不在这里立刻压缩，先回传一个占位结果，保持协议一致。
                    manual_compact = True
                    output = "Compressing..."
                else:
                    # 常规工具走分发表执行。
                    handler = TOOL_HANDLERS.get(block.name)
                    try:
                        output = (
                            handler(**block.input)
                            if handler
                            else f"Unknown tool: {block.name}"
                        )
                    except Exception as e:
                        # 任意工具异常都降级为字符串，保证主循环不中断。
                        output = f"Error: {e}"
                # 打印精简日志，便于在终端观察 agent 行为。
                print(f"> {block.name}: {str(output)[:200]}")
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    }
                )
        # 按 Anthropic 约定，以 user 角色回填工具结果。
        messages.append({"role": "user", "content": results})
        # Layer 3: manual compact triggered by the compact tool
        if manual_compact:
            print("[manual compact]")
            # 与自动压缩共用同一逻辑，减少分叉行为差异。
            messages[:] = auto_compact(messages)


if __name__ == "__main__":
    # history 持有整段会话状态，会被 agent_loop 原地更新。
    history = []
    while True:
        try:
            # 交互式输入提示，标识当前示例编号 s06。
            query = input("\033[36ms06 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        # 空输入或退出指令均结束程序。
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 追加用户消息后，让 agent_loop 运行直到本轮稳定。
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 打印最后一条 assistant 的文本块（忽略 tool_use 等结构化块）。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
