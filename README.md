# Auto Research Agent

当前 `main` 是 v0.3 简化方案：Codex Goal 是唯一研究 Agent；本项目只提供 detached 实验运行和一个事件驱动的 Goal 唤醒桥。

Codex 自己负责优化目标、调查资料、提出 idea、修改代码、判断实验是否稳定、暂停 Goal、分析结果以及决定继续或完成。本项目不再运行固定 cycle，也不替 Codex 决定研究步骤。

## 版本

| 版本 | Git 位置 | 方案 |
|---|---|---|
| v0.1.0 | tag `v0.1.0` | 原 `main` 的完整 Goal Harness |
| v0.2.0 | tag `v0.2.0`、branch `goal-autonomous-research` | Codex 自主研究 + 完整生命周期 Harness |
| v0.3.0 | `main` | Codex 自主暂停 + one-shot Goal Wake Listener |

历史版本可以直接检出：

```bash
git switch --detach v0.1.0
git switch --detach v0.2.0
```

## 当前架构

```text
Codex Goal
  ├─ 优化研究目标、选择 idea、修改代码
  ├─ auto-research start / Experiment MCP
  ├─ 确认 worker 稳定后台运行
  └─ 主动暂停 Goal
             │
             ▼
detached Worker ──> completed / failed / timeout / cancelled / lost
             │
             ▼
one-shot Goal Wake Listener
  ├─ 恢复原 thread
  ├─ paused -> active
  ├─ turn/start 注入 run_id 和终态路径
  └─ 保持 App Server 存活，直到 Goal 再次 paused/complete
```

Listener 不轮询 Codex，也不创建研究 cycle。安装 `watchfiles` 时监听操作系统文件事件；否则只轮询本地终态文件，不调用模型或 MCP。

## 安装

```bash
uv venv
uv sync --extra watcher
```

如果使用可选的 Experiment MCP：

```bash
uv sync --extra mcp --extra watcher
```

认证由本机 Codex CLI/App Server 管理：

```bash
codex login status
```

项目不会读取或保存 API key。

## 初始化

```bash
uv run auto-research init /path/to/experiment-project
```

这会创建：

- `goal.json`：供 Codex 审查和优化的初始目标。
- `research/config.toml`：实验和 listener 的唯一配置文件。

配置模板见 [config.toml.example](config.toml.example)。

## 推荐：Codex 直接启动实验

在 Codex Goal 中执行：

```bash
uv run auto-research start \
  --project /path/to/experiment-project \
  --idea-id idea-001 \
  --worktree /path/to/experiment-project \
  --timeout-s 14400 \
  --command 'python train.py --config candidate.yaml'
```

命令会立即返回类似结果：

```json
{
  "run_id": "run-idea-001-ab12cd34",
  "status": "RUNNING",
  "wake_listener": {
    "status": "ARMED",
    "thread_id": "019f..."
  }
}
```

`auto-research start` 会同时完成：

1. 落盘 `run.json`。
2. 启动独立 session 的 detached Worker。
3. 优先从 `CODEX_THREAD_ID` 精确绑定当前 Goal task。
4. 启动 detached one-shot listener。

Codex 随后检查 `heartbeat.json` 或日志，确认实验稳定运行，然后主动暂停 Goal。不要让 Codex 循环查询 `RUNNING`。

推荐提示词见 [Codex Goal 提示词](docs/CODEX_GOAL_PROMPT.md)。

## 查看状态

```bash
uv run auto-research status run-idea-001-ab12cd34 \
  --project /path/to/experiment-project
```

每个 run 的持久状态位于：

```text
research/runs/<run_id>/
├── run.json
├── heartbeat.json
├── worker.log
├── metrics.json
├── events/<terminal>.json
├── wake-launch.json
├── wake.json
└── wake-listener.log
```

`wake.json` 状态转换如下：

```text
BINDING -> WAITING -> TERMINAL -> WAKING -> GOAL_RUNNING -> WOKEN
                         └───────────────────────────────> SKIPPED
```

## 异常恢复

Listener 在 App Server 暂时不可用时会在本地指数退避重试，不消耗模型 token。进程意外退出或机器重启后执行：

```bash
uv run auto-research recover-wakes --project /path/to/experiment-project
```

也可以给单个 run 重新绑定：

```bash
uv run auto-research arm-wake run-... \
  --project /path/to/experiment-project \
  --thread-id <codex-thread-id>
```

如果 `CODEX_THREAD_ID` 不可用，Listener 会通过 App Server 的 `thread/list(cwd=...)` 查找最近的 active/paused Goal。候选不明确时会持续等待显式绑定，不会唤醒猜测出来的历史任务。

## 可选：Experiment MCP

CLI 已经足以完成闭环。需要结构化工具调用时再注册 MCP：

```bash
uv run auto-research register-mcp --project /path/to/experiment-project
```

MCP 只提供：

- `start_experiment`
- `get_experiment_result`
- `cancel_experiment`

`start_experiment` 与 CLI `start` 使用相同 Runner，并自动启动同一个 one-shot listener。MCP 不控制 Goal 状态，也不做算法判断。

## 安全和并发边界

- 默认一个项目只允许一个未终态实验。
- `idempotency_key` 防止工具重试重复提交。
- Worker 使用独立 session；终端、MCP 或 Codex turn 结束不会杀死训练。
- 终态写入带锁，成功、失败、超时、取消和 LOST 只会有一个最终结果。
- Listener 按 `run_id -> thread_id` 持久绑定并加单实例锁，重复文件事件不会启动多个 turn。
- Goal 已经 active、complete、blocked 或受限时，Listener 不会盲目创建重复 turn。
- 当前按要求默认 `shell=true`、`danger-full-access`；仅在隔离实验环境使用。

## 文档

- [当前方案设计](docs/CODEX_AUTO_RESEARCH_AGENT_DESIGN.md)
- [Codex Goal 提示词](docs/CODEX_GOAL_PROMPT.md)
- [版本与历史方案](docs/HISTORY_DESIGN_ALTERNATIVES.md)
- [v0.1/v0.2 历史问题修复](docs/HISTORY_BUG_FIXES.md)
- [Codex App Server 官方文档](https://developers.openai.com/codex/app-server)

## 验证

```bash
uv run python -m unittest discover -s tests -v
```
