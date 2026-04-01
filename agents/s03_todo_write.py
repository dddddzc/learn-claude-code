#!/usr/bin/env python3
# Harness: planning -- keeping the model on course without scripting the route.
"""
s03_todo_write.py - TodoWrite

The model tracks its own progress via a TodoManager. A nag reminder
forces it to keep updating when it forgets.

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> | Tools   |
    |  prompt  |      |       |      | + todo  |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                                |
                    +-----------+-----------+
                    | TodoManager state     |
                    | [ ] task A            |
                    | [>] task B <- doing   |
                    | [√] task C            |
                    +-----------------------+
                                |
                    if rounds_since_todo >= 3:
                      inject <reminder>

Key insight: "The agent can track its own progress -- and I can see it."
"""

import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# 加载 .env 中的环境变量；override=True 表示允许 .env 覆盖已有同名环境变量。
load_dotenv(override=True)

# 当使用自定义 Anthropic 网关地址时，移除可能冲突的认证 token，
# 避免请求被错误地携带到不兼容的后端。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 以当前工作目录作为代理可操作的根目录。
WORKDIR = Path.cwd()
# 初始化 Anthropic 客户端；base_url 可为空（走官方默认地址），也可指向代理网关。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 从环境变量读取模型 ID，未设置会直接抛错，便于尽早暴露配置问题。
MODEL = os.environ["MODEL_ID"]

# 系统提示词：约束模型在该工作目录内执行，并鼓励优先使用工具而非纯文本回答。
SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use the todo tool to plan multi-step tasks. Mark in_progress before starting, completed when done.
Prefer tools over prose."""


# -- TodoManager: structured state the LLM writes to --
class TodoManager:
    def __init__(self):
        # items 结构示例：
        # [{"id": "1", "text": "实现功能", "status": "in_progress"}, ...]
        self.items = []

    def update(self, items: list) -> str:
        # 限制待办数量，防止上下文膨胀或模型输出过长。
        if len(items) > 20:
            raise ValueError("Max 20 todos allowed")
        # validated 用于存储经过验证的所有任务列表，确保状态机一致性。
        validated = []
        # 统计进行中任务数，保证同一时间只有一个 in_progress。
        in_progress_count = 0
        for i, item in enumerate(items):
            # 统一清洗字段：text 去首尾空格，status 小写化，id 转字符串。
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            # text 必填，否则该 todo 无法表达实际任务。
            if not text:
                raise ValueError(f"Item {item_id}: text required")
            # 仅允许三种状态，保持状态机简单明确。
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})
        # 多个 in_progress 会导致执行焦点不清，直接拒绝。
        if in_progress_count > 1:
            raise ValueError("Only one task can be in_progress at a time")
        # 整体验证通过后再一次性替换 self.items，避免部分更新。
        self.items = validated
        return self.render()

    def render(self) -> str:
        # 无任务时返回固定占位文本，便于模型判断是否已初始化 todo。
        if not self.items:
            return "No todos."
        lines = []
        for item in self.items:
            # 将状态映射为可视化标记，提升可读性。
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[√]"}[
                item["status"]
            ]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        # 统计完成数量，输出汇总进度。
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)


TODO = TodoManager()


# -- Tool implementations --
def safe_path(p: str) -> Path:
    # 将用户传入路径解析为绝对路径，并强制限制在 WORKDIR 内，
    # 防止通过 ../ 访问工作区外文件。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    # 最基础的危险命令拦截，防止误删系统文件或执行高风险操作。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # 在工作目录执行 shell 命令，收集 stdout/stderr，并设置超时保护。
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        # 对输出做截断，避免向模型回灌过长文本。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        # 读取文本文件并按行处理，limit 用于控制返回行数上限。
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            # 超限时追加提示，告知仍有剩余内容未展示。
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        # 自动创建父目录，支持直接写入尚不存在的嵌套路径。
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        # 仅替换第一个匹配项，降低误改范围。
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


TOOL_HANDLERS = {
    # 将工具名映射到本地处理函数，供 agent loop 动态分发调用。
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo": lambda **kw: TODO.update(kw["items"]),
}

TOOLS = [
    # 下面是提供给模型的工具元数据（名称、描述、JSON 输入约束）。
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
        "name": "todo",
        "description": "Update task list. Track progress on multi-step tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "text": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                        "required": ["id", "text", "status"],
                    },
                }
            },
            "required": ["items"],
        },
    },
]


# -- Agent loop with nag reminder injection --
def agent_loop(messages: list):
    # 记录距离上次调用 todo 工具经过了几轮；用于触发提醒。
    rounds_since_todo = 0
    while True:
        # Nag reminder is injected below, alongside tool results
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        # 将 assistant 返回原样加入历史，供下一轮上下文使用。
        messages.append({"role": "assistant", "content": response.content})
        # 当模型不再请求工具时，结束本轮代理循环并返回给上层交互。
        if response.stop_reason != "tool_use":
            return
        results = []
        # 标记本轮是否使用了 todo；若使用则重置提醒计数器。
        used_todo = False
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    # 根据工具名动态分派；未知工具返回明确错误。
                    output = (
                        handler(**block.input)
                        if handler
                        else f"Unknown tool: {block.name}"
                    )
                except Exception as e:
                    # 捕获工具异常并回传文本，避免循环中断。
                    output = f"Error: {e}"
                # 本地打印摘要，方便在终端观察执行轨迹。
                print(f"> {block.name}: {str(output)[:200]}")
                # 组装 tool_result 发回模型，使用 tool_use_id 对应请求块。
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    }
                )
                if block.name == "todo":
                    used_todo = True
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        # 若连续 3 轮未更新 todo，注入提醒文本，促使模型回到任务管理流程。
        if rounds_since_todo >= 3:
            results.insert(
                0, {"type": "text", "text": "<reminder>Update your todos.</reminder>"}
            )
        # 以 user 角色回传工具结果，驱动模型继续下一轮决策。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # history 保存完整对话上下文（用户输入、assistant 输出、tool_result 等）。
    history = []
    while True:
        try:
            query = input("\033[36ms03 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        # 空输入或退出指令均结束程序。
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 先记录用户输入，再进入 agent loop。
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 循环结束后，history[-1] 通常是 assistant 的最终回复内容。
        response_content = history[-1]["content"]
        # 兼容内容块列表结构，提取可打印文本。
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
