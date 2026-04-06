# Learn Claude Code 课程总览

这份总览不是 README 的复述，而是面向“要读懂 agent harness 怎么落到代码里”的工程路线图。整套仓库以 `agents/s01_*.py` 到 `agents/s12_*.py` 为主线，同时配有 `docs/`、`skills/` 和 `web/` 作为辅助材料。

先说一个必须明确的前提：这些 `s01-s12` 文件在概念上是递进的，但在源码上并不全是“上一课完整代码 + 本课增量”的严格超集。很多文件是围绕某个机制做的教学切片。也就是说：

- 递进关系是真的。
- 但代码组织往往是“聚焦一个机制重新讲”，不是“把前 11 课所有类都永远带着跑”。

后面的 12 节讲义会同时保持这两条线：

1. 讲清楚概念上为什么要一层层引入这些机制。
2. 讲清楚源码里这一课到底新增了什么，以及它有没有把前一课的某些模块暂时收窄掉。

## 一、项目结构怎么读

最值得先看的目录有 5 个：

- `agents/`
  课程主源码。`s01` 到 `s12` 是教学切片，`s_full.py` 是把多种机制揉在一起的参考整合版。
- `docs/zh/`、`docs/en/`
  现有说明文档。适合快速对照每一课的教学目标，但写法偏短，很多细节需要回到源码确认。
- `skills/`
  `s05` 会用到的真实技能文件。这里不是概念示意，而是 `SkillLoader` 真会扫描的内容。
- `web/`
  交互式课程前端。`web/src/lib/constants.ts` 定义了课程顺序与分层，`web/src/data/execution-flows.ts` 给出了每课的流程图元数据，`web/scripts/extract-content.ts` 负责把源码和 docs 提取成前端可消费的数据。
- `README.md`、`README-zh.md`
  给出整套课程想表达的高层理念，尤其是“模型就是 agent，工程重点在 harness”。

## 二、四个阶段怎么理解

### 阶段 1：单体 Agent 内核

- `s01` 到 `s02`
- 核心问题：如何让模型不是“一次性回答器”，而是能持续行动的 agent。
- 关键主题：循环、工具协议、handler 分发。

### 阶段 2：计划与记忆管理

- `s03` 到 `s06`
- 核心问题：agent 会漂、会被上下文污染、会被知识和历史拖死。
- 关键主题：todo、子 agent、按需技能加载、上下文压缩。

### 阶段 3：持久化协调与并发

- `s07` 到 `s08`
- 核心问题：状态不能只活在对话里，慢操作也不能一直堵住主循环。
- 关键主题：任务图、磁盘持久化、后台线程、通知注入。

### 阶段 4：多 Agent 协作与执行隔离

- `s09` 到 `s12`
- 核心问题：一个 agent 不够时，如何让多个 agent 协作、协商、自主找活，并且互不污染工作目录。
- 关键主题：队友、邮箱协议、请求-响应握手、自治认领、worktree 隔离。

## 三、课程总览表

| 编号 | 课程名称 | 核心机制 | 一句话价值 | 关键源码入口 |
|---|---|---|---|---|
| s01 | Agent Loop | `agent_loop` + 单工具 `bash` | 建立最小可运行 agent 内核 | `agents/s01_agent_loop.py` 中的 `agent_loop()`、`run_bash()` |
| s02 | Tool Use | `TOOL_HANDLERS` + 多工具 schema | 加工具不用改主循环 | `agents/s02_tool_use.py` 中的 `safe_path()`、`TOOL_HANDLERS`、`TOOLS` |
| s03 | TodoWrite | `TodoManager` + nag reminder | 让 agent 显式维护当前计划 | `agents/s03_todo_write.py` 中的 `TodoManager.update()`、`TodoManager.render()`、`agent_loop()` |
| s04 | Subagent | 新 `messages[]` 的子代理 | 子任务隔离上下文污染 | `agents/s04_subagent.py` 中的 `run_subagent()`、`CHILD_TOOLS`、`PARENT_TOOLS` |
| s05 | Skills | 两层知识注入 | 知识按需加载，不把 system prompt 塞爆 | `agents/s05_skill_loading.py` 中的 `SkillLoader`、`load_skill` |
| s06 | Context Compact | 微压缩 + 自动压缩 + 手动压缩 | 会话可以长期运行，不被历史拖死 | `agents/s06_context_compact.py` 中的 `micro_compact()`、`auto_compact()` |
| s07 | Task System | 文件化任务图 + 依赖关系 | 状态脱离对话，持久化到磁盘 | `agents/s07_task_system.py` 中的 `TaskManager.create()`、`update()`、`list_all()` |
| s08 | Background Tasks | 后台线程 + 通知队列 | 慢操作不再阻塞主 agent | `agents/s08_background_tasks.py` 中的 `BackgroundManager.run()`、`_execute()`、`drain_notifications()`、`agent_loop()` |
| s09 | Agent Teams | 持久化队友 + JSONL 邮箱 | 从一次性 subagent 走向持续协作队友 | `agents/s09_agent_teams.py` 中的 `MessageBus`、`TeammateManager.spawn()`、`_teammate_loop()` |
| s10 | Team Protocols | `request_id` 关联的协议握手 | 协作从“发消息”升级为“可追踪协商” | `agents/s10_team_protocols.py` 中的 tracker、`handle_shutdown_request()`、`handle_plan_review()` |
| s11 | Autonomous Agents | 空闲轮询 + 自动认领任务 | 队友能自己找活，不必领导逐个下发 | `agents/s11_autonomous_agents.py` 中的 `scan_unclaimed_tasks()`、`claim_task()`、`make_identity_block()`、`TeammateManager._loop()` |
| s12 | Worktree Task Isolation | 任务板 + worktree 生命周期 + 事件流 | 并行改动真正做到目录隔离 | `agents/s12_worktree_task_isolation.py` 中的 `detect_repo_root()`、`EventBus`、`TaskManager.bind_worktree()`、`WorktreeManager.create()`、`run()`、`remove()`、`keep()` |

## 四、每一课相对前一课到底加了什么

### s01 -> s02

- 不再只有 `bash`。
- 真正新增的不是“多几个函数”，而是“工具声明”和“工具执行”被拆开了。
- `agent_loop()` 基本不变，变的是循环外侧的 dispatch 结构。

### s02 -> s03

- 模型第一次开始写“结构化的自我进度状态”。
- 重点不是规划算法，而是 harness 提供一个可写、可校验、可提醒的状态槽位。

### s03 -> s04

- 不是再加一个 todo 类型工具，而是把“上下文隔离”引入了系统。
- 子 agent 共享文件系统，但不共享会话历史。

### s04 -> s05

- 不是让 system prompt 更大，而是让知识变成可按需拉取的资源。
- 这里第一次出现“元信息常驻，正文延迟注入”的两层结构。

### s05 -> s06

- 不再只考虑“该给模型什么知识”，开始考虑“该让模型忘掉什么”。
- 核心变化是 memory lifecycle 进入主循环。

### s06 -> s07

- 任务状态正式从对话内存迁移到磁盘。
- 这一步之后，压缩上下文不再等于丢掉任务结构。

### s07 -> s08

- 慢命令从阻塞调用变成后台任务。
- 关键变化不是“多了线程”，而是“主循环不再负责等待，改为负责在边界处接收通知”。

### s08 -> s09

- 并行执行从“并行 shell 命令”升级为“并行模型代理”。
- 后台线程只会跑命令，队友线程会自己调用 LLM、工具和邮箱。

### s09 -> s10

- 协作从非结构化消息变成可追踪协议。
- 重点不是多两个工具名，而是引入 `request_id` 和全局 tracker。

### s10 -> s11

- 领导从“显式分配每个任务”退居成“维护制度和看板的人”。
- 最大变化不是多了 `claim_task`，而是队友线程从单阶段工作循环变成“工作相 + 空闲相”双相生命周期。

### s11 -> s12

- 协调平面和执行平面正式拆开。
- `task` 负责回答“做什么”，`worktree` 负责回答“在哪做”。

## 五、哪些辅助文件能帮助理解源码

### 1. `web/src/lib/constants.ts`

这个文件不是业务逻辑，但它非常有价值，因为它显式声明了：

- 课程顺序 `VERSION_ORDER`
- 每课的标题、副标题、核心增量 `coreAddition`
- 每课所属分层 `layer`

它相当于前端视角的课程目录。

### 2. `web/src/data/execution-flows.ts`

这个文件把每课的执行流程画成节点和边。它不是运行时代码，但很适合作为“脑内流程图”的辅助材料。尤其适合对照 `agent_loop()`、后台通知注入、任务图、worktree 生命周期。

### 3. `web/scripts/extract-content.ts`

这个脚本做的事很有代表性：

- 扫描 `agents/*.py`
- 提取 class、function、tool 名称
- 读取 `docs/{locale}/*.md`
- 生成 `web/src/data/generated/*.json`

这说明仓库本身就是按“源码 + 教学文档 + 可视化数据”三层组织的。

### 4. `skills/*/SKILL.md`

这是 `s05` 的真实输入，不是摆设。比如：

- `skills/pdf/SKILL.md`
- `skills/code-review/SKILL.md`

`SkillLoader` 会真正扫描这些文件，解析 frontmatter，然后在运行时按技能名返回正文。

### 5. `docs/zh/*.md`

这些文档适合拿来确认课程主题，但不够细。后面的 `summary/*.md` 会以源码为主，把调用链、数据结构、设计动机补全。

## 六、阅读顺序建议

如果你是第一次深入读这个仓库，建议按下面的节奏：

1. 先读 `s01`、`s02`
   先把“主循环 + 工具分发”打透。
2. 再读 `s03` 到 `s06`
   这四课讲的是 agent 为什么会漂、会污染、会忘记，以及 harness 怎么兜底。
3. 然后读 `s07`、`s08`
   这是从“会话内 agent”走向“可持续系统”的转折点。
4. 最后读 `s09` 到 `s12`
   真正进入多 agent 协作、协议化治理和执行隔离。

## 七、这套讲义会特别强调什么

后面的 12 个文件会统一强调 6 件事：

- 这节课到底想解决什么工程问题。
- 如果没有这个机制，agent 会卡在哪里。
- 相对上一课，新增能力到底是什么。
- 代码层面新加了哪些类、函数、字段和调用链。
- 运行时流程是怎样串起来的。
- 教学版实现已经说明了什么，又还缺哪些生产级能力。

## 八、一句话路线图

这 12 课不是在堆“高级概念”，而是在一步一步把一个最小的 `while tool_use` 循环，扩成一个能规划、会记忆、可持久化、可并发、可协作、可隔离执行的 agent harness。
