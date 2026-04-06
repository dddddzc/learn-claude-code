#!/usr/bin/env python3
# Harness: on-demand knowledge -- domain expertise, loaded when the model asks.
"""
s05_skill_loading.py - Skills

Two-layer skill injection that avoids bloating the system prompt:

    Layer 1 (cheap): skill names in system prompt (~100 tokens/skill)
    Layer 2 (on demand): full skill body in tool_result

    skills/
      pdf/
        SKILL.md          <-- frontmatter (name, description) + body
      code-review/
        SKILL.md

    System prompt:
    +--------------------------------------+
    | You are a coding agent.              |
    | Skills available:                    |
    |   - pdf: Process PDF files...        |  <-- Layer 1: metadata only
    |   - code-review: Review code...      |
    +--------------------------------------+

    When model calls load_skill("pdf"):
    +--------------------------------------+
    | tool_result:                         |
    | <skill>                              |
    |   Full PDF processing instructions   |  <-- Layer 2: full body
    |   Step 1: ...                        |
    |   Step 2: ...                        |
    | </skill>                             |
    +--------------------------------------+

Key insight: "Don't put everything in the system prompt. Load on demand."
"""

import os
import re
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# 加载本地 .env，便于在不同环境中统一管理密钥与模型配置。
load_dotenv(override=True)

# 如果走自定义网关，移除可能冲突的鉴权变量。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 约定当前工作目录为代理可操作根目录。
WORKDIR = Path.cwd()
# 初始化 Anthropic 客户端，可通过环境变量切换 base_url。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 从环境变量读取模型名；缺失时会在启动阶段报错。
MODEL = os.environ["MODEL_ID"]
# 技能目录固定在仓库下的 skills/。
SKILLS_DIR = WORKDIR / "skills"


# -- SkillLoader: scan skills/<name>/SKILL.md with YAML frontmatter --
class SkillLoader:
    def __init__(self, skills_dir: Path):
        # 保存技能根目录。
        self.skills_dir = skills_dir
        # 内存索引：key=技能名，value=元数据/正文/路径。
        self.skills = {}
        # 初始化时一次性扫描并加载所有技能。
        self._load_all()

    def _load_all(self):
        # 没有技能目录时保持空技能集。
        if not self.skills_dir.exists():
            return
        # 递归发现所有 SKILL.md，支持多层目录组织。
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            text = f.read_text()
            # frontmatter 作为结构化元信息，body 作为详细指令内容。
            meta, body = self._parse_frontmatter(text)
            # 若未声明 name，回退到父目录名作为技能名。
            name = meta.get("name", f.parent.name)
            # 建立技能索引，便于后续按名称加载。
            self.skills[name] = {"meta": meta, "body": body, "path": str(f)}

    def _parse_frontmatter(self, text: str) -> tuple:
        """Parse YAML frontmatter between --- delimiters."""
        # 用正则提取 frontmatter 与正文；frontmatter 仅支持简单 key:value 行。
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        # 没有 frontmatter 时，整段文本按正文处理。
        if not match:
            return {}, text
        meta = {}
        # 逐行解析元数据，遇到第一个冒号分割键值。
        # match.group(1) 是 frontmatter 部分，strip 去除首尾空白后按行分割。
        # match.group(2) 是正文部分，strip 去除首尾空白后作为技能内容。
        for line in match.group(1).strip().splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                meta[key.strip()] = val.strip()
        # 返回结构化元数据 + 去首尾空白后的正文。
        return meta, match.group(2).strip()

    def get_descriptions(self) -> str:
        """Layer 1: short descriptions for the system prompt."""
        # 没有技能时返回显式提示，避免系统提示里出现空段。
        if not self.skills:
            return "(no skills available)"
        lines = []
        # 将技能压缩为短描述，用于低成本注入 system prompt。
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "No description")
            tags = skill["meta"].get("tags", "")
            line = f"  - {name}: {desc}"
            # tags 可选，用于提示模型按领域筛选技能。
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)

    def get_content(self, name: str) -> str:
        """Layer 2: full skill body returned in tool_result."""
        # 按名称查找技能，供 load_skill 工具按需加载。
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        # 用轻量 XML 包裹，帮助模型识别这是技能正文边界。
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"


# 启动时构建技能加载器，后续由 load_skill 工具按名称取用技能正文。
SKILL_LOADER = SkillLoader(SKILLS_DIR)

# 第一层注入：仅把技能名/描述放进 system，保持提示词轻量。
SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use load_skill to access specialized knowledge before tackling unfamiliar topics.

Skills available:
{SKILL_LOADER.get_descriptions()}"""


# -- Tool implementations --
def safe_path(p: str) -> Path:
    # 将外部输入路径绑定到工作区内，防止目录穿越。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    # 对高风险命令做最小黑名单拦截，降低误操作概率。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # 在工作区中执行命令，统一收集 stdout/stderr 并设置超时。
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        # 限制工具输出大小，避免把超长日志注入上下文。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        # 读取目标文件并按行返回，便于模型逐行理解内容。
        lines = safe_path(path).read_text().splitlines()
        # 可选截断行数，避免一次读入过大文件。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        # 统一返回字符串错误，避免工具调用中断对话流程。
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        # 自动创建父目录，支持一次写入深层路径。
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        # 统一字符串化异常，保持工具返回格式一致。
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        # 仅在旧文本存在时替换，避免误改无关内容。
        if old_text not in content:
            return f"Error: Text not found in {path}"
        # 只替换首个匹配项，控制改动范围。
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        # 统一字符串化异常，保持工具返回格式一致。
        return f"Error: {e}"


TOOL_HANDLERS = {
    # 统一工具分发表：工具名 -> 本地执行函数。
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # load_skill 不做文件写操作，只返回对应技能全文。
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),
}

TOOLS = [
    # 工具 schema 提供给模型，用于约束可调用能力与参数结构。
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
        "name": "load_skill",
        "description": "Load specialized knowledge by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name to load"}
            },
            "required": ["name"],
        },
    },
]


def agent_loop(messages: list):
    # 经典 agent 循环：模型思考 -> 请求工具 -> 回填结果 -> 继续推理。
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        # 保留 assistant 原始 block，确保上下文完整可追溯。
        messages.append({"role": "assistant", "content": response.content})
        # 如果本轮没有工具调用，说明模型已给出最终答复。
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # 根据 block.name 分派到对应本地处理器。
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    # 将 block.input 解包为关键字参数传给工具函数。
                    output = (
                        handler(**block.input)
                        if handler
                        else f"Unknown tool: {block.name}"
                    )
                except Exception as e:
                    # 工具异常转为文本，避免主循环崩溃。
                    output = f"Error: {e}"
                # 打印简短执行日志，便于观察 agent 行为。
                print(f"> {block.name}: {str(output)[:200]}")
                # 按 Anthropic 协议回传 tool_result，绑定原 tool_use_id。
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    }
                )
        # 以 user 角色注入工具结果，驱动下一轮推理。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # history 保存整个多轮会话，包括工具调用与结果。
    history = []
    while True:
        try:
            # 使用带颜色的命令行前缀，区分当前示例编号。
            query = input("\033[36ms05 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        # 支持空输入与退出指令快速结束程序。
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 先追加用户输入，再让 agent_loop 消化到稳定状态。
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 读取最后一条 assistant 内容，打印其中可见文本块。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
