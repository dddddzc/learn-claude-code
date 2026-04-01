#!/usr/bin/env python3
# 驱动层：循环机制——模型与真实世界的第一层连接。
"""
s01_agent_loop.py - The Agent Loop

The entire secret of an AI coding agent in one pattern:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.
"""

import os
import subprocess

from anthropic import Anthropic
from dotenv import load_dotenv

# 在读取配置前先加载本地环境变量（例如 .env）。
load_dotenv(override=True)

# 使用自定义网关/base URL 时，清理可能冲突的认证令牌。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 从环境变量创建 API 客户端并选择模型。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# 系统提示词让代理始终以当前工作目录为上下文。
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

# 暴露给模型的工具契约。
# 模型只能请求一个名为 "bash" 的工具，且输入字段仅有 command（字符串）。
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


def run_bash(command: str) -> str:
    # 基础安全护栏：拦截明显危险的命令。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # 在当前工作区执行，捕获标准输出/错误，并设置超时。
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        # 合并输出并限制长度，避免消息历史无限膨胀。
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


# -- 核心模式：持续调用工具，直到模型主动停止 --
def agent_loop(messages: list):
    # 一次完整循环 = 一次模型回合 + 可选的工具执行。
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 将 assistant 的内容块（文本 + 可能的 tool_use）写入历史。
        messages.append({"role": "assistant", "content": response.content})
        # 如果模型没有请求工具，本轮任务结束。
        if response.stop_reason != "tool_use":
            return
        # 执行每个工具请求，并将输出封装成 tool_result 块。
        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"\033[33m$ {block.input['command']}\033[0m")
                output = run_bash(block.input["command"])
                print(output[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})
        # 将工具输出作为“用户回合”回灌给模型，驱动下一轮循环。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # 当前会话内，多轮用户输入共享同一份对话历史。
    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        # 空输入或退出命令会结束交互模式。
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 追加用户问题，并让代理循环执行直到不再需要工具。
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 打印本轮最后一次 assistant 回合中的文本块。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
