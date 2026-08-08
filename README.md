# auto-research-agent

一个不依赖 orze 的 Python 自动算法实验优化 Agent 原型。它使用 Codex 作为研究/工程 Agent，使用本地 detached worker 执行长时间实验，并通过终态事件返回结果。

设计文档：

- [当前方案](docs/CODEX_AUTO_RESEARCH_AGENT_DESIGN.md)
- [历史方案](docs/HISTORY_DESIGN_ALTERNATIVES.md)
- [历史问题与修复记录](docs/HISTORY_BUG_FIXES.md)

## 创建环境

```bash
uv venv
uv sync
```

实验 MCP server 额外安装：

```bash
uv sync --extra mcp
```

事件监听器（推荐后台 Goal Harness 使用）：

```bash
uv sync --extra watcher
```

## 初始化

```bash
uv run auto-research init .
```

编辑 `goal.json`，至少设置：目标、主指标、方向、搜索空间、sealed 路径、实验预算和停止条件。`goal.json` 是 `max_experiments` 的唯一配置来源；不要在 `research/goal_contract.json` 重复配置它。

Harness 配置文件为项目内唯一的 `research/harness.toml`，可以从仓库中的 [harness.toml.example](harness.toml.example) 复制。配置文件按 `[codex]`、`[harness]`、`[experiment]` 分组；环境变量只用于 CI 或临时覆盖，配置文件优先级高于代码默认值。`max_cycles` 只在这里配置，命令行 `--max-cycles` 仅用于单次运行覆盖。

实验输出位于 `research/runs/`，研究记录位于 `research/ledger/`，状态位于 `research/state/current.json`。需要恢复一个已经提交的实验时，可以运行：

```bash
uv run auto-research wait run-... --runs-dir research/runs
```

## Codex Goal + Experiment MCP

当前推荐把 Codex Goal 作为主研究 Agent，把本项目的 MCP server 作为实验执行层。Goal 负责总结目标、提出和筛选 idea、修改可编辑代码、解释结果；MCP server 只负责启动、持久化、查询、等待和取消实验，不参与算法决策。

当前 Goal Harness 通过 `codex app-server --stdio` 连接 Codex，而不是为每轮实验创建独立的 `codex exec` 会话。App Server 维护可恢复的 `thread`；Goal 是 thread 上的持续研究目标；每个 `turn` 是一次独立的推理/工具调用周期。启动长实验的 turn 结束后，Codex 不运行，detached worker 在后台执行；Harness 只监听本地终态事件，事件到达后恢复原 thread 并发起一次新的 turn。Harness 会缓存先于 JSON-RPC 响应到达的生命周期通知，避免 `turn/completed` 被响应读取逻辑丢弃；恢复旧 thread 时也会重新同步并激活当前目标契约。完整的组件边界、RPC 顺序和时序见[当前方案设计](docs/CODEX_AUTO_RESEARCH_AGENT_DESIGN.md)。

首次实验前，Goal Harness 会要求 Codex 主动审查用户目标，并将带有 `schema_version` 和 `revision` 的研究契约写入 `research/goal_contract.json`。实验启动时会把当前 contract 的硬指标快照固化到该 run；实验过程中默认沿用 contract，只有实验反馈明确证明目标、指标或约束明显不合理时，Codex 才应修订它。Harness 只执行实验预算、失败上限、结果格式和硬性资源边界。目标是否达成必须由 Codex 写入 `research/goal_decision.json` 显式判断。

认证由本机 Codex CLI/App Server 自己管理，agent 不读取或保存 API key。请先完成 Codex CLI 登录，并确认 `codex login status` 可用；不要把密钥写入 `goal.json`、ledger 或实验日志。

### Codex 模型与思考程度

Goal Harness 可以通过环境变量显式固定 App Server 使用的模型和 reasoning effort：

```bash
AUTO_RESEARCH_CODEX_MODEL='gpt-5.6' \
AUTO_RESEARCH_CODEX_REASONING_EFFORT=high \
uv run --extra watcher auto-research goal-harness \
  --project /path/to/experiment-repo \
  --objective '优化固定验证集 accuracy，达到目标或预算耗尽后停止'
```

Harness 自身默认使用 `gpt-5.6-luna` 和 `medium`，不依赖用户级 Codex 配置。`AUTO_RESEARCH_CODEX_MODEL` 和 `AUTO_RESEARCH_CODEX_REASONING_EFFORT` 可以覆盖这两个默认值。当前支持的 effort 值为 `minimal`、`low`、`medium`、`high`、`xhigh`，但最终仍取决于所选模型。Harness 会把实际配置写入 `research/goal_harness.json`，并把每次实验的配置快照写入对应的 `run.json`，用于研究结果复现。

配置文件示例：

```toml
[codex]
model = "gpt-5.6-luna"
reasoning_effort = "medium"
sandbox = "danger-full-access"
approval_policy = "never"

[harness]
max_cycles = 1000
reconnect_attempts = 3
reconnect_backoff_s = 2.0
event_poll_s = 0.25
event_grace_s = 30.0

[experiment]
use_shell = true
allowed_executables = ["python", "python3"]
default_timeout_s = 3600
worker_heartbeat_s = 5.0
```

常用环境变量包括 `AUTO_RESEARCH_CODEX_MODEL`、`AUTO_RESEARCH_CODEX_REASONING_EFFORT`、`AUTO_RESEARCH_CODEX_SANDBOX`、`AUTO_RESEARCH_CODEX_APPROVAL`、`AUTO_RESEARCH_MAX_CYCLES`、`AUTO_RESEARCH_USE_SHELL`、`AUTO_RESEARCH_ALLOWED_EXECUTABLES` 和 `AUTO_RESEARCH_DEFAULT_EXPERIMENT_TIMEOUT_S`。完整名称和默认值见 [harness.toml.example](harness.toml.example)。

自动算法优化时建议在同一个研究任务中固定模型和 effort，避免把 Agent 能力变化误当成算法改进。通常可使用较强模型配合 `high`；如果更重视成本和速度，可使用 `medium`。App Server 会在 `thread/start` 和每个 `turn/start` 中接收这些覆盖项。

### 历次设计参考文档

本项目的当前实现不依赖下列参考项目；它们只用于抽取研究循环、实验隔离、结果记录和长任务恢复等思路：

- [Karpathy autoresearch](https://github.com/karpathy/autoresearch)：自动修改、运行实验、根据结果继续搜索的最小研究循环。
- `/Users/dollars/work/orze`：本地 Agent 编排和实验管理思路参考；未作为当前运行时依赖。
- `/Users/dollars/work/orze_pro`：本地实验调度、状态和恢复思路参考；未作为当前运行时依赖。
- [当前方案设计](docs/CODEX_AUTO_RESEARCH_AGENT_DESIGN.md)：Codex Goal、App Server、Experiment MCP、detached worker 和终态事件的现行架构。
- [历史方案与取舍](docs/HISTORY_DESIGN_ALTERNATIVES.md)：CLI、SDK、直接轮询、Goal Harness 以及其他已放弃方案的记录。
- [Codex App Server 官方文档](https://learn.chatgpt.com/docs/app-server.md)：JSONL RPC、thread、turn、模型/effort 覆盖和事件流。
- [Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)：`model_reasoning_effort`、模型默认值及配置优先级。
- [Codex 长时运行工作流](https://learn.chatgpt.com/docs/long-running-work.md)：Goal、暂停/恢复和长任务的官方工作流背景。

先生成 Codex MCP 配置片段：

```bash
uv run auto-research print-mcp-config --project /path/to/experiment-repo
```

也可以直接注册到任务目录的 `.codex/config.toml`：

```bash
uv run auto-research register-mcp --project /path/to/experiment-repo
```

该命令只创建或更新 `[mcp_servers.experiment]`，会保留已有的其他 Codex 配置。注册完成后，在 Codex 中启动一个 Goal。Goal 的实验调用方式如下：

1. Goal 只调用 `start_experiment`，立即得到 `run_id`，随后结束当前 turn；实验继续由 detached worker 在后台运行。
2. `GoalHarness` 监听本地 `completed/failed/timeout/cancelled` 终态事件，不调用 Codex，也不调用 MCP 状态查询。
3. 事件到达后，Harness 只为同一个 Goal thread 发起一次恢复 turn；该 turn 调用 `get_experiment_result(run_id)`，分析结果并可启动下一轮。
4. 不允许使用定时器反复启动 turn，也不允许 Codex 反复查询 `RUNNING`。
5. 需要停止时调用 `cancel_experiment(run_id)`。

Harness 会按 App Server 的 `turn_id` 关联 MCP 事件。恢复历史 thread 时，即使事件流包含旧 turn 的 MCP item，也不会把旧 `run_id` 误认为当前 turn 新启动的实验。App Server 响应默认 60 秒、单个 turn 默认 900 秒超时；超时会尝试 `turn/interrupt`，不盲目重试 `turn/start`，并把状态保存为 `APP_SERVER_STALLED`，避免静默连接无限阻塞。可通过 `AUTO_RESEARCH_APP_SERVER_RESPONSE_TIMEOUT_S` 和 `AUTO_RESEARCH_APP_SERVER_TURN_TIMEOUT_S` 覆盖。

也可以单独启动 MCP server：

```bash
AUTO_RESEARCH_PROJECT_DIR=/path/to/experiment-repo \
  uv run auto-research mcp-server
```

长实验的关键点是 `run_id`、`run.json`、终态事件和日志都落盘于 `research/runs/<run_id>/`。终端、MCP 连接或 Codex 当前 turn 断开不会杀死 worker；恢复后按 `run_id` 查询即可。Goal 的暂停/恢复由 Codex 会话控制；本项目不模拟未公开稳定的 Goal pause/resume API。

实验命令成功退出后必须生成非空 JSON 对象 `research/runs/<run_id>/metrics.json`；否则 Runner 将实验标记为 `FAILED`，不会把缺少指标的结果交给 Goal 晋级。

如果项目有数值门槛，目标优化阶段会把它们确认并写入当前有效的 `research/goal_contract.json` 的 `hard_requirements`，例如 `ap >= 0.50`，或 `thr == 0.1` 时 `recall >= 0.80`。Harness 会逐个实验核验最新 contract 中的门槛并把失败项反馈给 Codex；它不会仅凭门槛通过就判定整个研究目标完成。

自动恢复 Harness：

```bash
uv run --extra watcher auto-research goal-harness \
  --project /path/to/experiment-repo \
  --objective '优化固定验证集 accuracy，达到目标或预算耗尽后停止' \
  --prompt-file research/goal_prompt.txt
```

Harness 状态写入 `research/goal_harness.json`，其中的 `pending_run_id` 保证进程重启后只等待并恢复原实验，不重复提交。

使用 macOS `launchctl` 后台运行时，建议通过仓库提供的一次性包装器提交。包装器会在 Harness 正常结束后自动卸载 launchd job，避免 `goal_decision.json` 已完成后被反复拉起：

```bash
launchctl submit -l com.auto-research.example -- \
  /Users/dollars/work/Auto\ Research/scripts/run_background_harness.sh \
  com.auto-research.example \
  uv run --extra watcher auto-research goal-harness \
    --project /path/to/experiment-repo \
    --objective '优化固定验证集 accuracy，达到目标或预算耗尽后停止' \
    --prompt-file research/goal_prompt.txt
```

`goal_decision.json` 使用结构化停止决策：`decision` 为 `achieved`、`plateau`、`budget_exhausted` 或 `blocked`，并必须提供 `evidence_run_ids` 和 `hard_requirements_passed`。自然语言 `reason` 仅作解释，不作为计数或硬门槛事实来源。

如果 Codex 生成的 `goal_contract.json` 缺字段、字段类型错误或 JSON 格式异常，Harness 不会直接终止。错误会写入 `research/goal_contract_error.json`，当前阶段变为 `GOAL_REPAIR`，并在同一个 Goal thread 中要求 Codex 修复契约；契约重新通过校验后才允许启动实验。实验命令本身的崩溃、超时或缺少 `metrics.json` 仍会按 `FAILED`、`TIMEOUT` 或 `LOST` 交给 Codex 判断，不会被误认为契约格式错误。

同样地，格式错误的 `goal_decision.json` 会写入 `research/goal_decision_error.json`，进入 `GOAL_DECISION_REPAIR`，由 Codex 修复或删除后再继续，不会因为大模型输出格式错误而直接结束 Harness。

当前 Harness 只支持串行实验。如果异常情况下一个 turn 仍然产生多个 `run_id`，Harness 会把这些 ID 写入状态并进入 `PAUSED`，等待人工核对，而不是因未捕获异常退出。`active_experiment.json` 损坏时会自动清理，并从 `runs/*/run.json` 恢复活动实验状态。

Harness 还为每个外层 cycle 写入 `active_harness_cycle.json`，并把 cycle id 固化到 run。即使 Codex Goal 自动续 turn，同一 Harness cycle 也不能再次提交实验；只有 Harness 进入下一 cycle 才会打开新的提交窗口。完整历史见 [HISTORY_BUG_FIXES.md](docs/HISTORY_BUG_FIXES.md)。

Worker 每 5 秒写入 `heartbeat.json`。如果超过实验超时和宽限期仍没有终态事件，Runner 会生成 `LOST` 终态并结束残留进程，Harness 随后恢复 Goal；不会无限卡在等待状态。

当前 Runner 还提供终态锁、启动/取消协调、`idempotency_key` 和 Harness 单实例锁，防止取消/完成竞态、MCP 重试重复启动以及两个恢复器同时操作同一 Goal。

安全模式下实验命令通过 `shlex.split` 转成 argv，并以 `shell=False` 执行。当前 Harness 按要求默认使用 `shell=True` 和 `danger-full-access`；如需切回安全模式：

```bash
AUTO_RESEARCH_USE_SHELL=false \
AUTO_RESEARCH_CODEX_SANDBOX=workspace-write \
  uv run auto-research goal-harness ...
```

使用 `torchrun`、`bash` 等入口时，安全模式还需要配置 `AUTO_RESEARCH_ALLOWED_EXECUTABLES`。当前默认配置会允许 shell 命令和整机访问，请仅在隔离实验环境使用。

Iris 示例还需要任务依赖：

```bash
uv sync --extra iris
uv run --extra watcher auto-research goal-harness \
  --project iris_task \
  --objective '优化 Iris 固定评估集 accuracy，达到目标或预算耗尽后停止' \
  --prompt '每轮提出多个候选 idea，只实现一个；实验完成后比较 baseline 和历史结果，再决定继续或停止。'
```

## 验证

```bash
uv run python -m unittest discover -s tests -v
```

测试覆盖 Runner、Experiment MCP、终态事件、幂等提交、终态竞态和 Goal Harness 的关键解析逻辑。
