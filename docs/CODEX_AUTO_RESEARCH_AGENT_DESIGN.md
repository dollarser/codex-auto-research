# 基于 Codex Goal 的自动实验优化 Agent

## 1. 当前方案

当前唯一默认运行链路是：

```text
用户对话目标
    ↓
GoalHarness → Codex App Server
    ↓
Codex Goal（主研究 Agent / thread 上的研究目标）
    ↓ MCP tools
Experiment MCP Server（实验生命周期）
    ↓
Detached Experiment Worker（后台执行）
    ↓
metrics.json + 终态事件 + 日志
    ↓
Codex Goal 分析结果并决定下一轮
```

本方案不依赖 orze 运行时。orze、orze_pro 和其他架构只保存在[历史方案文档](HISTORY_DESIGN_ALTERNATIVES.md)中，用于记录设计演进和取舍。

## 2. Codex App Server、Goal 与实验执行层

### 2.1 App Server 的定位

当前 Harness 使用的是 Codex CLI 提供的本地 App Server 协议入口：

```bash
codex app-server --stdio
```

它不是另一个模型，也不是一个实验 Runner，而是 Codex 引擎的本地 RPC 控制面。`GoalHarness` 通过 stdin/stdout 发送和接收 JSONL 消息，负责创建、恢复和推进 Codex 会话；Codex 自己负责推理、修改代码、调用 MCP 以及执行被允许的工具。

```text
GoalHarness（Python 生命周期控制器）
        │ JSONL RPC / stdin、stdout
        ▼
Codex App Server（本地 Codex 会话服务）
        │
        ├── Codex 推理与代码修改
        ├── sandbox / approval / workspace
        └── MCP client
                │
                ▼
        Experiment MCP Server
                │
                ▼
        Detached Experiment Worker
```

当前实现使用 `codex app-server --stdio`，不是 Python Codex SDK，也不是把每轮研究拆成互相独立的 `codex exec` 进程。旧的 SDK/`codex exec` 适配代码已移除，避免形成绕过 Goal 的第二条研究入口。

### 2.2 App Server 的核心概念

| 概念 | 当前方案中的含义 |
|---|---|
| App Server | 维护 Codex 会话并提供 JSONL RPC；它本身不决定实验策略 |
| `thread` | 持久化的研究会话，保存上下文、Goal 和历史 turn；断线后按 `thread_id` 恢复 |
| Goal | 绑定在 thread 上的持续研究目标；描述目标不等于自动产生新的 turn |
| `turn` | 一次可运行推理和工具调用的周期，从 `turn/start` 到 `turn/completed`；启动实验后可以主动结束 |
| item / event | App Server 流式返回的消息、工具调用和 turn 生命周期事件 |
| MCP Server | 为 Codex 提供实验工具的边界；只暴露确定性实验操作，不负责算法判断 |
| GoalHarness | App Server 外部的生命周期控制器，负责断线重连、等待终态和恢复 turn |
| Worker | 与 Codex 解耦的后台实验进程，负责实际训练、评估、日志和终态事件 |

### 2.3 当前使用的 App Server RPC 顺序

首次启动时，Harness 按如下顺序建立研究会话：

```text
启动 codex app-server --stdio
    ↓
initialize
    ↓
thread/start
    ↓
thread/goal/set
    ↓
turn/start
    ↓
接收 item / MCP / turn 事件
    ↓
turn/completed
```

实验完成后的恢复不是新建会话，而是复用原来的 `thread_id`：

```text
本地终态事件
    ↓
thread/resume(thread_id)
    ↓
thread/goal/get + thread/read(includeTurns=true)
    ↓
对账 Goal/thread/遗留 turn
    ↓
thread/goal/set(status=active)
    ↓
turn/start（携带 run_id 和结果已就绪的提示）
    ↓
Codex 调用 get_experiment_result(run_id)
```

如果 App Server 连接断开，Harness 会有限次指数退避重连；连接恢复后优先 `thread/resume` 原 thread，并重新同步当前 Goal。若断线前 `start_experiment` 已经落盘，Harness 从 `research/runs` 恢复原 `run_id`，而不是再次提交实验。每次 `turn/start` 都记录 App Server 返回的 `turn_id`，只接收带有同一 `turnId` 的 MCP item 和 `turn/completed`；恢复 thread 时可能出现的历史 item 不会被误计入当前 turn。JSON-RPC 响应读取期间提前到达的生命周期通知会进入缓存，待 `turn/start` 响应解析后继续消费，避免丢失 `turn/completed` 导致假性超时。为避免历史 turn 或工具调用无终态时永久阻塞，App Server 响应和 turn 分别有可配置 watchdog；超时会尝试 `turn/interrupt`，禁止自动重试不确定是否已提交的 `turn/start`，并持久化 `APP_SERVER_STALLED` 状态及 stderr 诊断信息。

实验提交或 `goal_decision.json` 持久化后，Harness 会把它视为当前 turn 的确定性边界。实验提交后先执行 `thread/goal/set(status=paused)`，再调用 `turn/interrupt`；完成决策落盘时使用 `status=complete`。关闭客户端时会 best-effort 暂停尚未终止的 Goal，并收尾 active turn；如果第一次暂停未确认，会在中断后、关闭传输前再尝试一次。长实验等待期间 Goal 始终保持 paused；恢复带有 `pending_run_id` 的 thread 时会重新确认 paused，只有本地终态事件到达、Harness 即将启动结果分析 turn 时才重新设为 active。目标内容更新不携带 status，避免 contract 同步意外唤醒 paused Goal。最近一次 App Server 已确认的状态持久化在 `goal_harness.json.goal_status`。

已有合法完成决策的恢复启动在创建 App Server/thread 之前直接结束；`--fresh-thread` 仅供人工替换上下文，自动恢复必须使用持久化的原 thread id。这里的 `turn/interrupt` 只终止当前执行轮次，`thread/goal/set` 才负责 Goal 生命周期，两者不能互相替代。

`research/goal_harness.json` 只保存最近一次 API 已确认快照。恢复已有 thread 后，App Server 的 `thread/goal/get` 与 `thread/read(includeTurns=true)` 是 Codex 状态事实源；Harness 会持久化 `goal_status`、`thread_status`、`thread_active_flags` 和检查时间。若发现一个 `inProgress` 历史 turn，则暂停 active Goal、interrupt 该 turn，并再次读取 thread 验证已经静止；多个 in-progress turn、`systemError`、无法静止的 active thread 或缺失 Goal 都进入启动失败诊断，不会盲目创建新 turn。`thread/status/changed` 与 `thread/goal/updated` 通知也会更新运行期快照。

本地 `pending_run_id` 仍是实验恢复事实：它存在时，Harness 在读取 thread 前重新提交 `status=paused`，随后只等待本地实验终态。Goal 若处于 `blocked`、`usageLimited` 或 `budgetLimited`，Harness 记录 `GOAL_SUSPENDED` 并拒绝自动激活。若 App Server 为 `complete` 但缺少合法 `goal_decision.json`，Harness 只允许一个 `GOAL_DECISION_REPAIR` turn 补齐或修复结构化证据，该 turn 明确禁止启动实验；这样状态对账不会破坏“大模型格式错误交给 Codex 修复”的既有恢复路径。状态查询只发生在启动和断线恢复边界，不是周期轮询。

每个 Harness 外层 cycle 还会写入 `research/active_harness_cycle.json`。MCP server 将 cycle id 写入 `run.json`，同一个 cycle 已经提交过实验后，即使 Goal 自动产生续 turn，也会拒绝第二次 `start_experiment`；Harness 关闭该提交窗口后，下一 cycle 才能启动新实验。这是程序级的串行边界，不能只依赖 Goal 提示词。

### 2.4 Harness 配置与覆盖优先级

项目级 Harness 配置保存在 `research/harness.toml`，分为 `[codex]`、`[harness]` 和 `[experiment]` 三组。模型、reasoning effort、sandbox、审批策略、重连参数、事件等待参数、shell 模式、argv 白名单、实验默认超时和 Worker heartbeat 都从这里读取。

配置优先级固定为：

```text
AUTO_RESEARCH_* 环境变量
        ↓
research/harness.toml
        ↓
代码内置默认值
```

因此生产/CI 可以用环境变量临时覆盖，普通研究任务则把配置和实验代码一起保存。每次 Harness 启动时会把实际模型和 reasoning effort 写入 `research/goal_harness.json`；启动实验后还会把它们写入该 run 的 `run.json`，避免后续无法判断实验使用的 Agent 配置。示例配置见仓库根目录的 `harness.toml.example`。

## 3. 组件职责

| 组件 | 职责 |
|---|---|
| Codex Goal | 质疑和优化用户目标、定义研究问题和指标、生成和筛选 idea、修改代码、分析结果、决定下一轮和最终是否达成 |
| Experiment MCP Server | 启动、查询和取消实验；不参与目标判断和算法决策 |
| Detached Worker | 在独立进程中执行实验，处理超时，保存日志和终态事件 |
| `research/ledger` | 保存 idea、实现记录、实验结果和判断理由 |
| `research/state` | 保存 baseline、当前最佳结果、预算和停止状态 |

Codex Goal 是主 Agent。Python 代码不是第二个研究 Agent，只承担确定性的实验生命周期、硬性约束校验和状态恢复。目标是否合理、指标是否重要、实验是否足以支持结论，必须由 Codex Goal 判断。

## 4. 当前端到端研究流程

一轮完整研究流程如下：

1. 用户描述一个可能不完整或不合理的优化目标。
2. Codex Goal 读取 baseline、评估协议、代码约束、历史 ideas、失败记录和必要的研究证据。
3. Codex Goal 质疑目标：确认研究问题、主指标、次指标、数据切分、资源预算和硬性约束是否真的服务于目标。
4. Codex Goal 将审查后的方案写入 `research/goal_contract.json`，并记录被拒绝或降级的原始要求及理由。
5. Codex Goal 基于 goal contract 生成多个候选 idea。每个 idea 必须包含假设、预期变化、修改文件、验证方法和失败判据。
6. Codex Goal 按预期收益、证据强度、信息增益、执行成本、风险和是否重复选择一个 idea。
7. Codex Goal 在独立 worktree 中实现最小代码变更，并执行 smoke test。
8. Codex Goal 通过 Experiment MCP 启动实验。
9. Detached Worker 执行训练、评估或基准命令，并保存结果。
10. Codex Goal 读取实验终态和指标，与 baseline、当前最佳及历史结果比较。
11. Codex Goal 根据反馈修订 goal contract：可以删除不重要指标、替换不合理指标或调整研究问题，但必须记录修订理由和证据。
12. Codex Goal 做出 `promote`、`discard`、`replicate`、`repair` 或 `pause` 决策，更新 ledger 和 state。
13. Codex Goal 如果判断目标已达成或继续实验没有价值，写入 `research/goal_decision.json`；否则启动下一轮。

### 4.1 一轮研究如何串联

```text
用户目标
  ↓
GoalHarness 创建/恢复 App Server thread
  ↓
Codex Goal 主动审查用户目标与研究证据
  ↓
写入 research/goal_contract.json
  ↓
Codex Goal 基于 goal contract 读取 ledger、state 和历史结果
  ↓
Codex 生成候选 ideas，选择一个并修改 worktree
  ↓
Codex → Experiment MCP.start_experiment()
  ↓ 返回 run_id
当前 Codex turn 结束；Goal 不在运行，Worker 继续运行
  ↓
Harness 等待本地终态事件（不是 LLM 轮询）
  ↓
Worker 写入 metrics、日志、heartbeat 和终态事件
  ↓
Harness 恢复同一个 thread，并只启动一个新的 turn
  ↓
Codex → get_experiment_result(run_id)
  ↓
Codex 比较 baseline / best / 历史实验，更新 ledger
  ↓
Codex 修订 goal contract 或写入 goal_decision.json
  ↓
promote / discard / replicate / repair / pause
  ↓
未满足停止条件时，由下一次恢复 turn 启动下一轮
```

这里有三个容易混淆的边界：

1. `Goal` 是研究目标和上下文，不是一个会持续占用模型调用的后台线程。
2. `turn/completed` 只表示本次 Codex 推理周期结束，不表示整个 Goal 已完成。
3. 实验进行时 Codex 处于空闲状态；只有 Worker 的终态事件到达后，Harness 才恢复 thread 并发起下一次 turn。

### 4.2 研究周期的完成条件

一轮实验反馈周期在以下条件满足后才算完成：

- Worker 已写入一个唯一终态：`COMPLETED`、`FAILED`、`TIMEOUT`、`CANCELLED` 或 `LOST`；
- Harness 已用同一 `run_id` 读取结果；
- Codex 已比较指标并更新 ledger/state；
- Codex 已读取并解释结果，明确选择下一步：继续实验，或写入 `goal_decision.json` 声明目标达成、价值不足或需要人工确认。

因此，单次 `turn/completed` 只结束一个 turn；“研究完成”必须由 Goal 根据研究证据显式决定。Harness 不根据单次指标、target 或 plateau 自行宣布目标达成。

## 5. 实验调用模式

### 5.1 默认模式：事件驱动后台调用

```text
Codex Goal 当前 turn
  └─ start_experiment(...) -> run_id
       └─ Goal paused + 当前 turn interrupted
            └─ Detached Worker 后台运行
                 └─ 本地 GoalHarness 监听终态事件
                      └─ Goal active + 只启动一次恢复 turn
                           └─ Codex 调用 get_experiment_result(run_id)
```

`GoalHarness` 保存 `thread_id`、`pending_run_id` 和最近确认的 `goal_status`。它只监听本地终态事件，不启动定时器，不调用 MCP 状态查询；实验等待期间 Goal 为 paused，事件到达后才激活同一个 Goal thread。恢复 turn 可以分析结果并启动下一轮。

## 6. Experiment MCP 工具

当前 MCP Server 只提供三个工具：

```text
start_experiment(idea_id, worktree, command, timeout_s) -> run_id
get_experiment_result(run_id) -> TerminalRunResult | RUNNING             # 事件后读取
cancel_experiment(run_id) -> CancelledResult
```

工具层只暴露确定性操作，不允许通过自然语言直接宣布算法成功。晋级判断由 Codex Goal 根据指标和约束完成。

## 7. 实验持久化与恢复

每个实验保存到：

```text
research/runs/<run_id>/
├── run.json
├── worker.log
├── stdout.log
├── stderr.log
├── metrics.json
├── heartbeat.json
└── events/
    ├── started.json
    ├── completed.json
    ├── failed.json
    ├── timeout.json
    ├── cancelled.json
    └── lost.json
```

生命周期：

```text
SUBMITTED → RUNNING → COMPLETED
                    ├→ FAILED
                    ├→ TIMEOUT
                    ├→ CANCELLED
                    └→ LOST
```

Worker 使用独立进程组运行，并每 5 秒写入 heartbeat。实验命令退出、超时或被取消后写入唯一终态事件；如果超过 `timeout_s + grace` 仍没有终态，Runner 会检查并终止残留进程，写入 `lost.json`，再恢复 Goal。这样 Worker 崩溃、机器重启或事件丢失不会让 Harness 无限等待。

实验命令以退出码 0 结束并不自动代表结果有效。Worker 要求实验生成非空 JSON 对象格式的 `metrics.json`；缺失、空对象或非法 JSON 会写入 `FAILED` 终态，避免无指标实验被错误晋级。

## 8. Goal 规范

用户提供的 Goal 只是待审查输入，不是不可修改的最终目标。首次实验前，Codex 必须生成并维护：

```text
research/goal_contract.json
```

该文件必须声明 `schema_version: 1` 和正整数 `revision`，并记录研究问题、主指标、次指标、硬性约束、软性偏好、被拒绝的要求和每次修订理由。实验反馈到达后默认沿用 contract；只有证据明确显示目标、指标或约束明显不合理时，Codex 才修订 contract。指标重要性和研究问题的最终判断属于 Codex，而不是 Harness。

实验启动时会把当前 contract 的 digest、revision 和 `hard_requirements` snapshot 写入该 run 的 `run.json`。硬性数值指标按实验启动时的 snapshot 核验，避免实验运行期间 contract 变化导致同一实验的验收标准漂移。当前有效的 `goal_contract.json` 的 `hard_requirements` 由 Harness 在每个新实验启动时固化。例如：

```json
{
  "hard_requirements": [
    {"metric": "ap", "operator": ">=", "value": 0.50},
    {
      "metric": "recall",
      "operator": ">=",
      "value": 0.80,
      "when": {"thr": {"operator": "==", "value": 0.1}}
    }
  ]
}
```

对应的实验结果可以写成：

```json
{
  "metrics": {"ap": 0.56, "recall": 0.83},
  "params": {"thr": 0.1}
}
```

条件不满足时该条约束不适用；条件满足但数值不达标时，Harness 会记录硬指标失败并告知 Codex。Harness 不会因为硬指标通过就宣布整个研究目标达成。

Goal contract 应描述算法优化问题，而不是泛化工程任务，至少包括：

- 优化对象和可修改的算法范围。
- 主指标、优化方向和必要的次指标。
- 固定数据切分、评估脚本、随机种子和资源预算。
- 可编辑路径与 sealed 路径。
- 最大实验数、plateau 窗口和连续失败阈值。
- 目标值、停止条件和人工确认条件。

其中 `max_experiments`、`max_consecutive_failures` 和资源边界只来自项目的 `goal.json`，由 Harness 作为不可被模型放宽的运行保护；`goal_contract.json` 不重复声明实验预算。首次目标优化后，`goal_contract.json` 成为 AP、Recall 等数值门槛的唯一来源。`hard_requirements` 只允许结构化数值门槛；数据切分不可变、不得修改评估器等自然语言协议约束必须写入 `protocol_requirements`。格式不符合当前 schema 时，Harness 会进入 `GOAL_REPAIR`，由 Codex 修复后再继续。`target_metric`、plateau、secondary metrics 和“是否达成”属于 Codex 的研究判断，也可以随 contract 修订。

详细规则放在仓库文件中，Goal 只引用它们。例如：

```text
/goal 优化固定验证集上的 F1。
主指标 F1 越高越好；保持数据、评估器和训练预算不变。
只允许修改 models/、train.py 和 configs/experiments/，不得修改 data/ 与 eval/。
每轮提出多个候选，只实现一个，并通过 Experiment MCP 执行。
实验完成后根据指标、约束和历史结果决定 promote、discard 或 replicate。
达到目标值、实验预算耗尽、plateau 或连续失败时停止。
```

当 Codex 判断研究已经完成时，必须写入：

```json
{
  "status": "complete",
  "decision": "plateau",
  "evidence_run_ids": ["run-example-123"],
  "hard_requirements_passed": false,
  "reason": "基于 baseline、复现实验和历史结果，继续搜索的预期信息增益不足。"
}
```

保存路径为 `research/goal_decision.json`。`decision` 必须是 `achieved`、`plateau`、`budget_exhausted` 或 `blocked`；`evidence_run_ids` 必须引用真实终态 run；`achieved` 必须同时声明 `hard_requirements_passed=true`。Harness 只接受这个显式判断作为“Goal 完成”的依据，不会根据某一个 accuracy 或 loss 自动宣布目标达成，但会校验结构化字段，避免把硬门槛未通过误写成执行失败。

## 9. 安全和确定性边界

- Codex 不能修改 sealed 数据、评估器、凭据和实验历史。
- 代码 diff、命令、环境、指标和资源预算必须写入 run receipt。
- 指标必须可解析、可比较；缺失或格式错误的结果不能晋级。
- 超时、OOM、崩溃和无提升都要进入 ledger，防止重复实验。
- `LOST`、Worker 崩溃和 heartbeat 超时都必须进入 ledger，并由 Goal 决定 repair、retry 或 pause。
- `run_id` 只允许安全的 `run-*` 格式；MCP 查询和取消不能通过参数访问 runs 目录之外的路径。
- 当前 Harness 按部署要求默认使用 `danger-full-access`；生产环境建议显式切回 `workspace-write`。
- 当前 Runner 按部署要求默认使用 `shell=True`；安全模式通过 `AUTO_RESEARCH_USE_SHELL=false` 启用 argv 白名单和 `shell=False`。
- 一个 GoalHarness 进程通过文件锁独占一个项目，避免两个 Harness 同时恢复同一 thread。
- `start_experiment` 支持 `idempotency_key`，MCP 重试不会重复创建相同实验。
- 幂等扫描和创建由项目级提交锁保护，避免并发 MCP 请求创建重复实验。
- 取消请求与 Worker 启动共用终态锁；Worker 记录 `child_pid` 前不会让取消流程越过启动阶段。
- run 的终态使用锁保护，只允许 `COMPLETED`、`FAILED`、`TIMEOUT`、`CANCELLED` 或 `LOST` 中的一个获胜。
- `idea_id` 会清洗为安全路径片段，不能通过 idea 名称穿越到 runs 目录之外。
- App Server 连接异常时，Harness 会有限次指数退避重连，并 resume 原 thread；如果连接中断前 MCP 已持久化 run，则从 `research/runs` 发现它，不重复提交。
- 主指标比较、资源限制和停止条件由确定性代码校验，不能只依赖模型描述。
- Harness 只校验主指标存在且为数值、实验结果格式、实验预算、连续执行失败上限、run 启动时固化的硬性数值门槛和 Worker 终态；硬门槛失败会反馈给 Codex，但不等同于 Codex 整体目标失败。`target_metric`、plateau、提升是否有意义以及目标是否达成由 Codex Goal 判断。
- Codex 不读取或保存 API key；认证由 Codex 自己的运行环境管理。

## 10. Token 策略

- 实验运行期间：0 次 LLM 调用。
- 短实验：一次 Goal turn 完成启动、等待和结果分析。
- 长实验：启动 turn 立即结束，恢复时再读取结果。
- 日志先由本地程序提取指标、异常和尾部摘要，再交给 Codex。
- 只有失败、恢复、plateau 或策略切换时才增加额外分析。

## 11. 当前实现映射

| 设计对象 | 实现 |
|---|---|
| 实验后台执行 | `src/auto_research/runner.py` + `runner_worker.py` |
| Experiment MCP | `src/auto_research/mcp_server.py` |
| Goal 事件驱动恢复 | `src/auto_research/goal_harness.py` |
| Codex App Server RPC | `AppServerClient`（`goal_harness.py`）调用 `codex app-server --stdio` |
| Goal/idea/state | Codex Goal + `research/goal_contract.json` + `models.py` + `ledger.py` |
| CLI 配置 | `auto-research print-mcp-config` |
| Codex 适配 | `AppServerClient`，通过 `codex app-server --stdio` 连接 |

安装并配置：

```bash
uv sync --extra mcp
uv run auto-research print-mcp-config --project /path/to/experiment-repo
```

将输出放入目标项目的 `.codex/config.toml` 后，在 Codex 中启动 Goal。需要自动恢复时使用：

```bash
uv run --extra watcher auto-research goal-harness \
  --project /path/to/experiment-repo \
  --objective '优化固定评估集上的主指标，达到目标或预算耗尽后停止' \
  --prompt-file research/goal_prompt.txt
```

Harness 的关键状态：

```text
GOAL_RUNNING
  ↓ start_experiment -> run_id
WAITING_FOR_EVENT
  ↓ completed/failed/timeout/cancelled
RESULT_READY
  ↓ 同一个 thread 发起一次 turn
GOAL_RUNNING / GOAL_REVISED / COMPLETED / PAUSED
```

## 12. 验收标准

- 任意实验都能通过 `run_id` 找到命令、worktree、日志、指标和终态。
- 终端或 MCP 连接断开不会导致后台实验中断。
- 实验期间没有 LLM 轮询。
- App Server 断线重连后能够恢复原 `thread_id`，不会重复提交 `run_id`。
- 实验终态到达前不会启动恢复 turn；终态到达后每个 `run_id` 最多恢复一次。
- `turn/completed` 不会被误判为整个 Goal 完成，停止必须由 Goal 根据停止条件决定。
- 失败、超时和取消都有明确终态。
- Goal 能基于 baseline 和历史结果选择下一轮，而不是重复失败配置。
- sealed 文件和评估协议不会被候选实现修改。
- 重启后可从 `research/runs`、ledger 和 state 恢复。
- Worker 崩溃或终态事件丢失后，watchdog 会生成 `LOST`，不会无限等待。
- 同一幂等键重复提交时返回同一个 `run_id`。
- 两个 Harness 不能同时控制同一个项目。
- App Server 进程重启后可以 resume 原 thread，且中断后的已提交实验不会重复启动。
