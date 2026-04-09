#!/usr/bin/env python3
# Harness: autonomy -- models that find work without being told.
"""
s11_autonomous_agents.py - Autonomous Agents

Idle cycle with task board polling, auto-claiming unclaimed tasks, and
identity re-injection after context compression. Builds on s10's protocols.

    Teammate lifecycle:
    +-------+
    | spawn |
    +---+---+
        |
        v
    +-------+  tool_use    +-------+
    | WORK  | <----------- |  LLM  |
    +---+---+              +-------+
        |
        | stop_reason != tool_use
        v
    +--------+
    | IDLE   | poll every 5s for up to 60s
    +---+----+
        |
        +---> check inbox -> message? -> resume WORK
        |
        +---> scan .tasks/ -> unclaimed? -> claim -> resume WORK
        |
        +---> timeout (60s) -> shutdown

    Identity re-injection after compression:
    messages = [identity_block, ...remaining...]
    "You are 'coder', role: backend, team: my-team"

Key insight: "The agent finds work itself."
"""

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# 加载 .env 配置；override=True 表示以 .env 中的值覆盖系统同名变量
load_dotenv(override=True)
# 当使用自定义 Anthropic 网关地址时，移除可能冲突的认证环境变量
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 当前工作目录作为整个代理系统的工作空间根目录
WORKDIR = Path.cwd()
# 初始化 Anthropic 客户端；支持通过环境变量重写 base_url
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 模型 ID 从环境变量读取，缺失会直接抛 KeyError（启动期失败更显式）
MODEL = os.environ["MODEL_ID"]
# 团队状态目录：保存配置与收件箱
TEAM_DIR = WORKDIR / ".team"
# 每个成员对应一个 jsonl 文件作为 inbox
INBOX_DIR = TEAM_DIR / "inbox"
# 任务看板目录：每个任务一份 task_*.json
TASKS_DIR = WORKDIR / ".tasks"

# 空闲轮询间隔（秒）
POLL_INTERVAL = 5
# 最长空闲等待时间（秒），超时则自动关机
IDLE_TIMEOUT = 60

# Lead 的系统提示词：强调“队员会自主找活干”
SYSTEM = f"You are a team lead at {WORKDIR}. Teammates are autonomous -- they find work themselves."

VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}

# -- Request trackers --
# 跟踪 shutdown 请求的状态：request_id -> {target, status}
shutdown_requests = {}
# 跟踪计划审批请求：request_id -> {from, plan, status}
plan_requests = {}
# 保护上述 tracker 的并发读写
_tracker_lock = threading.Lock()
# 保护任务 claim 的并发读写，避免多个线程同时认领同一任务
_claim_lock = threading.Lock()


# -- MessageBus: JSONL inbox per teammate --
class MessageBus:
    def __init__(self, inbox_dir: Path):
        # 收件箱根目录（每个成员一个 jsonl）
        self.dir = inbox_dir
        # 启动时确保目录存在
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(
        self,
        sender: str,
        to: str,
        content: str,
        msg_type: str = "message",
        extra: dict = None,
    ) -> str:
        # 先校验消息类型，避免写入无效协议消息
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        # 构造统一消息格式
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        # 允许附加协议字段（如 request_id、approve）
        if extra:
            msg.update(extra)
        # 目标成员的 inbox 文件路径
        inbox_path = self.dir / f"{to}.jsonl"
        # 采用追加写入，形成一行一条 JSON 消息
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        # 收件箱按成员名映射
        inbox_path = self.dir / f"{name}.jsonl"
        # 无文件即视为无消息
        if not inbox_path.exists():
            return []
        messages = []
        # 逐行解析 JSON（天然支持流式追加）
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                messages.append(json.loads(line))
        # 读后即清空，实现“消费并排空”语义
        inbox_path.write_text("")
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        # 统计广播触达成员数（不包含发送者自己）
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


BUS = MessageBus(INBOX_DIR)


# -- Task board scanning --
def scan_unclaimed_tasks() -> list:
    # 确保任务目录存在
    TASKS_DIR.mkdir(exist_ok=True)
    unclaimed = []
    # 扫描 task_*.json，按文件名排序保证行为稳定
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        # 仅返回“待处理 + 无 owner + 未被阻塞”的任务
        if (
            task.get("status") == "pending"
            and not task.get("owner")
            and not task.get("blockedBy")
        ):
            unclaimed.append(task)
    return unclaimed


def claim_task(task_id: int, owner: str) -> str:
    # 认领过程需要加锁，避免并发竞争
    with _claim_lock:
        path = TASKS_DIR / f"task_{task_id}.json"
        if not path.exists():
            return f"Error: Task {task_id} not found"
        # 读取任务并更新归属与状态
        task = json.loads(path.read_text())
        task["owner"] = owner
        task["status"] = "in_progress"
        # 以可读格式写回，便于人工检查
        path.write_text(json.dumps(task, indent=2))
    return f"Claimed task #{task_id} for {owner}"


# -- Identity re-injection after compression --
def make_identity_block(name: str, role: str, team_name: str) -> dict:
    # 上下文压缩后，补回最关键身份信息，降低“失忆”风险
    return {
        "role": "user",
        "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
    }


# -- Autonomous TeammateManager --
class TeammateManager:
    def __init__(self, team_dir: Path):
        # 团队配置目录
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        # 团队配置文件：成员、状态、团队名
        self.config_path = self.dir / "config.json"
        # 启动时加载已有配置（若不存在则给默认值）
        self.config = self._load_config()
        # 运行中的线程句柄：name -> Thread
        self.threads = {}

    def _load_config(self) -> dict:
        # 有配置就读配置
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        # 首次运行返回默认团队结构
        return {"team_name": "default", "members": []}

    def _save_config(self):
        # 所有配置变更都统一落盘
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find_member(self, name: str) -> dict:
        # 线性查找成员（成员数量通常不大）
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _set_status(self, name: str, status: str):
        # 更新成员状态并持久化
        member = self._find_member(name)
        if member:
            member["status"] = status
            self._save_config()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        # 如果成员已存在，先检查是否允许重启
        member = self._find_member(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            # 可重启状态下，切回 working 并允许更新 role
            member["status"] = "working"
            member["role"] = role
        else:
            # 新成员首次加入团队
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        # 启动后台线程执行成员自治循环
        thread = threading.Thread(
            target=self._loop,
            args=(name, role, prompt),
            daemon=True,
        )
        # 记录线程句柄，便于后续管理
        self.threads[name] = thread
        thread.start()
        return f"Spawned '{name}' (role: {role})"

    def _loop(self, name: str, role: str, prompt: str):
        # 读取团队名用于身份描述
        team_name = self.config["team_name"]
        # 每个成员独立系统提示词，明确其身份与自治职责
        sys_prompt = (
            f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
            f"Use idle tool when you have no more work. You will auto-claim new tasks."
        )
        # 会话初始消息：由 spawn 传入任务说明
        messages = [{"role": "user", "content": prompt}]
        # 可用工具列表（成员版）
        tools = self._teammate_tools()

        while True:
            # -- WORK PHASE: standard agent loop --
            for _ in range(50):
                # 每轮先处理 inbox，让外部信号（如 shutdown）优先生效
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        # 收到关机请求立即退出线程
                        self._set_status(name, "shutdown")
                        return
                    # 其他消息序列化后注入对话上下文
                    messages.append({"role": "user", "content": json.dumps(msg)})
                try:
                    # 调用模型，允许其进行工具调用
                    response = client.messages.create(
                        model=MODEL,
                        system=sys_prompt,
                        messages=messages,
                        tools=tools,
                        max_tokens=8000,
                    )
                except Exception:
                    # 模型调用异常时将成员标记为空闲后退出
                    self._set_status(name, "idle")
                    return
                # 记录 assistant 回复（含文本块与 tool_use 块）
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    # 不是工具调用则结束本轮工作阶段
                    break
                results = []
                idle_requested = False
                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "idle":
                            # idle 是成员主动进入空闲轮询的信号
                            idle_requested = True
                            output = "Entering idle phase. Will poll for new tasks."
                        else:
                            # 其他工具交给统一执行器
                            output = self._exec(name, block.name, block.input)
                        # 控制台打印工具执行摘要，便于观测
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        # 构造 tool_result 回传给模型
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(output),
                            }
                        )
                # 将工具结果作为 user 消息追加，驱动下一轮推理
                messages.append({"role": "user", "content": results})
                if idle_requested:
                    # 成员已主动宣告空闲，切换到 IDLE PHASE
                    break

            # -- IDLE PHASE: poll for inbox messages and unclaimed tasks --
            # 进入空闲状态，等待外部消息或自主发现任务
            self._set_status(name, "idle")
            # resume 标记是否找到新工作
            resume = False
            # 计算最大轮询次数；避免除 0
            polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)
            for _ in range(polls):
                # 固定间隔轮询
                time.sleep(POLL_INTERVAL)
                # 先看 inbox，若有消息优先恢复工作
                inbox = BUS.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            # 空闲期也必须即时响应关机
                            self._set_status(name, "shutdown")
                            return
                        # 非关机消息继续注入上下文
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break
                # inbox 为空时，尝试自主从任务看板领取任务
                unclaimed = scan_unclaimed_tasks()
                if unclaimed:
                    # 当前策略：认领排序后的第一个可领任务
                    task = unclaimed[0]
                    claim_task(task["id"], name)
                    # 生成自动认领提示，作为新任务输入给模型
                    task_prompt = (
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"
                        f"{task.get('description', '')}</auto-claimed>"
                    )
                    # 当上下文很短时，补回身份信息，避免压缩后角色漂移
                    if len(messages) <= 3:
                        messages.insert(0, make_identity_block(name, role, team_name))
                        messages.insert(
                            1,
                            {
                                "role": "assistant",
                                "content": f"I am {name}. Continuing.",
                            },
                        )
                    # 将新任务作为用户输入追加
                    messages.append({"role": "user", "content": task_prompt})
                    # 添加一条 assistant 自述，帮助模型稳定延续任务语境
                    messages.append(
                        {
                            "role": "assistant",
                            "content": f"Claimed task #{task['id']}. Working on it.",
                        }
                    )
                    resume = True
                    break

            if not resume:
                # 超时未收到消息也未认领到任务，执行自动关机
                self._set_status(name, "shutdown")
                return
            # 找到新工作后切回 working，进入下一轮 WORK PHASE
            self._set_status(name, "working")

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        # these base tools are unchanged from s02
        # 以下为成员侧工具分发：按工具名转发到具体实现
        if tool_name == "bash":
            return _run_bash(args["command"])
        if tool_name == "read_file":
            return _run_read(args["path"])
        if tool_name == "write_file":
            return _run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            return _run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            return BUS.send(
                sender, args["to"], args["content"], args.get("msg_type", "message")
            )
        if tool_name == "read_inbox":
            return json.dumps(BUS.read_inbox(sender), indent=2)
        if tool_name == "shutdown_response":
            # 成员回复 shutdown 请求：更新 tracker 并通知 lead
            req_id = args["request_id"]
            with _tracker_lock:
                if req_id in shutdown_requests:
                    shutdown_requests[req_id]["status"] = (
                        "approved" if args["approve"] else "rejected"
                    )
            BUS.send(
                sender,
                "lead",
                args.get("reason", ""),
                "shutdown_response",
                {"request_id": req_id, "approve": args["approve"]},
            )
            return f"Shutdown {'approved' if args['approve'] else 'rejected'}"
        if tool_name == "plan_approval":
            # 成员提交计划审批：创建 request_id 并登记待处理状态
            plan_text = args.get("plan", "")
            req_id = str(uuid.uuid4())[:8]
            with _tracker_lock:
                plan_requests[req_id] = {
                    "from": sender,
                    "plan": plan_text,
                    "status": "pending",
                }
            BUS.send(
                sender,
                "lead",
                plan_text,
                "plan_approval_response",
                {"request_id": req_id, "plan": plan_text},
            )
            return f"Plan submitted (request_id={req_id}). Waiting for approval."
        if tool_name == "claim_task":
            # 允许成员主动按 ID 认领任务
            return claim_task(args["task_id"], sender)
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list:
        # these base tools are unchanged from s02
        # 返回给模型的工具 schema（成员可见）
        return [
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
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            {
                "name": "write_file",
                "description": "Write content to file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
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
                "name": "send_message",
                "description": "Send message to a teammate.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "content": {"type": "string"},
                        "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)},
                    },
                    "required": ["to", "content"],
                },
            },
            {
                "name": "read_inbox",
                "description": "Read and drain your inbox.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "shutdown_response",
                "description": "Respond to a shutdown request.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string"},
                        "approve": {"type": "boolean"},
                        "reason": {"type": "string"},
                    },
                    "required": ["request_id", "approve"],
                },
            },
            {
                "name": "plan_approval",
                "description": "Submit a plan for lead approval.",
                "input_schema": {
                    "type": "object",
                    "properties": {"plan": {"type": "string"}},
                    "required": ["plan"],
                },
            },
            {
                "name": "idle",
                "description": "Signal that you have no more work. Enters idle polling phase.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "claim_task",
                "description": "Claim a task from the task board by ID.",
                "input_schema": {
                    "type": "object",
                    "properties": {"task_id": {"type": "integer"}},
                    "required": ["task_id"],
                },
            },
        ]

    def list_all(self) -> str:
        # 无成员时给出简洁提示
        if not self.config["members"]:
            return "No teammates."
        # 逐行汇总成员身份与状态
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        # 提取成员名列表，供 broadcast 使用
        return [m["name"] for m in self.config["members"]]


TEAM = TeammateManager(TEAM_DIR)


# -- Base tool implementations (these base tools are unchanged from s02) --
def _safe_path(p: str) -> Path:
    # 将相对路径解析到工作区绝对路径
    path = (WORKDIR / p).resolve()
    # 防止路径逃逸（例如 ../../）访问工作区外部
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def _run_bash(command: str) -> str:
    # 最基础的危险命令拦截（示例级安全控制）
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # 在工作区执行 shell 命令，并限制超时
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        # 合并标准输出与错误输出，避免丢信息
        out = (r.stdout + r.stderr).strip()
        # 对输出做长度截断，避免上下文爆炸
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def _run_read(path: str, limit: int = None) -> str:
    try:
        # 按行读取，便于做行数截断
        lines = _safe_path(path).read_text().splitlines()
        # 如果设置了 limit，仅返回前 N 行并提示剩余行数
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        # 最终再做字符级截断
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def _run_write(path: str, content: str) -> str:
    try:
        # 路径安全校验
        fp = _safe_path(path)
        # 自动创建父目录，减少调用方心智负担
        fp.parent.mkdir(parents=True, exist_ok=True)
        # 直接覆盖写入
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def _run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = _safe_path(path)
        # 读取原文件内容
        c = fp.read_text()
        # 仅支持“精确匹配一次替换”，找不到就返回错误
        if old_text not in c:
            return f"Error: Text not found in {path}"
        # 只替换首个匹配，避免误替换过多区域
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# -- Lead-specific protocol handlers --
def handle_shutdown_request(teammate: str) -> str:
    # 生成短 request_id 并登记为 pending
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    # 给目标成员发协议消息
    BUS.send(
        "lead",
        teammate,
        "Please shut down gracefully.",
        "shutdown_request",
        {"request_id": req_id},
    )
    return f"Shutdown request {req_id} sent to '{teammate}'"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    # 先查询是否存在对应计划请求
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    # 更新审批状态
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    # 将审批结果发送回请求成员
    BUS.send(
        "lead",
        req["from"],
        feedback,
        "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    return f"Plan {req['status']} for '{req['from']}'"


def _check_shutdown_status(request_id: str) -> str:
    # 返回指定 request 的当前状态快照
    with _tracker_lock:
        return json.dumps(shutdown_requests.get(request_id, {"error": "not found"}))


# -- Lead tool dispatch (14 tools) --
TOOL_HANDLERS = {
    # 统一的 lead 侧工具分发表：工具名 -> 执行函数
    "bash": lambda **kw: _run_bash(kw["command"]),
    "read_file": lambda **kw: _run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: _run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "spawn_teammate": lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates": lambda **kw: TEAM.list_all(),
    "send_message": lambda **kw: BUS.send(
        "lead", kw["to"], kw["content"], kw.get("msg_type", "message")
    ),
    "read_inbox": lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast": lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    "shutdown_request": lambda **kw: handle_shutdown_request(kw["teammate"]),
    "shutdown_response": lambda **kw: _check_shutdown_status(kw.get("request_id", "")),
    "plan_approval": lambda **kw: handle_plan_review(
        kw["request_id"], kw["approve"], kw.get("feedback", "")
    ),
    "idle": lambda **kw: "Lead does not idle.",
    "claim_task": lambda **kw: claim_task(kw["task_id"], "lead"),
}

# these base tools are unchanged from s02
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
    {
        "name": "spawn_teammate",
        "description": "Spawn an autonomous teammate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "role": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["name", "role", "prompt"],
        },
    },
    {
        "name": "list_teammates",
        "description": "List all teammates.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_message",
        "description": "Send a message to a teammate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "content": {"type": "string"},
                "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)},
            },
            "required": ["to", "content"],
        },
    },
    {
        "name": "read_inbox",
        "description": "Read and drain the lead's inbox.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "broadcast",
        "description": "Send a message to all teammates.",
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    },
    {
        "name": "shutdown_request",
        "description": "Request a teammate to shut down.",
        "input_schema": {
            "type": "object",
            "properties": {"teammate": {"type": "string"}},
            "required": ["teammate"],
        },
    },
    {
        "name": "shutdown_response",
        "description": "Check shutdown request status.",
        "input_schema": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "required": ["request_id"],
        },
    },
    {
        "name": "plan_approval",
        "description": "Approve or reject a teammate's plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "approve": {"type": "boolean"},
                "feedback": {"type": "string"},
            },
            "required": ["request_id", "approve"],
        },
    },
    {
        "name": "idle",
        "description": "Enter idle state (for lead -- rarely used).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "claim_task",
        "description": "Claim a task from the board by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
]


def agent_loop(messages: list):
    while True:
        # 每轮先读取 lead inbox，确保先处理团队消息
        inbox = BUS.read_inbox("lead")
        if inbox:
            # 将 inbox 作为结构化上下文注入给模型
            messages.append(
                {
                    "role": "user",
                    "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
                }
            )
            # 添加确认语句，帮助模型建立“已处理消息”状态
            messages.append(
                {
                    "role": "assistant",
                    "content": "Noted inbox messages.",
                }
            )
        # 调用 lead 模型，允许其选择工具推进任务
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        # 记录模型回复
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            # 非工具调用表示本轮完成，返回到外层交互循环
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # 根据工具名找到处理器
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    # 处理器存在则执行；否则返回未知工具错误
                    output = (
                        handler(**block.input)
                        if handler
                        else f"Unknown tool: {block.name}"
                    )
                except Exception as e:
                    # 保护主循环：工具异常转换为可见错误文本
                    output = f"Error: {e}"
                # 控制台打印执行摘要，便于调试
                print(f"> {block.name}: {str(output)[:200]}")
                # 组装 tool_result 反馈给模型
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    }
                )
            # 将所有工具结果追加回上下文，继续下一轮模型决策
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # 交互历史（role/content），跨多轮保留上下文
    history = []
    while True:
        try:
            # 读取用户输入（含彩色提示符）
            query = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+D / Ctrl+C 直接退出
            break
        # 空输入或显式退出命令都结束程序
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 调试命令：查看团队状态
        if query.strip() == "/team":
            print(TEAM.list_all())
            continue
        # 调试命令：读取并清空 lead inbox
        if query.strip() == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue
        # 调试命令：查看任务看板摘要
        if query.strip() == "/tasks":
            TASKS_DIR.mkdir(exist_ok=True)
            for f in sorted(TASKS_DIR.glob("task_*.json")):
                t = json.loads(f.read_text())
                # 根据任务状态映射可视化标记
                marker = {
                    "pending": "[ ]",
                    "in_progress": "[>]",
                    "completed": "[x]",
                }.get(t["status"], "[?]")
                # 若已分配 owner，则显示 @owner
                owner = f" @{t['owner']}" if t.get("owner") else ""
                print(f"  {marker} #{t['id']}: {t['subject']}{owner}")
            continue
        # 正常用户输入进入对话历史
        history.append({"role": "user", "content": query})
        # 运行一轮 lead 代理循环
        agent_loop(history)
        # 读取最后一条 assistant 内容并尝试输出文本块
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        # 轮次间空行分隔
        print()
