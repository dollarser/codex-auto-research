# v0.3 当前方案：Codex Goal + One-shot Wake Listener

## 1. 设计目标

Codex Goal 保留完整研究自主权；外部代码只解决长实验与暂停 Goal 之间的异步断点。

核心原则：

1. Codex 决定研究内容和 Goal 生命周期。
2. Worker 独立于 Codex turn 执行。
3. Goal 暂停期间不运行模型、不查询 `RUNNING`。
4. 终态事件只触发一次对原 Goal 的恢复。
5. 不引入 Harness cycle、阶段编排或外部停止判断。

## 2. 组件边界

| 组件 | 负责 | 不负责 |
|---|---|---|
| Codex Goal | 目标优化、资料研究、idea、代码、实验解释、暂停与完成 | 后台进程保活 |
| CLI / Experiment MCP | 确定性提交、结果读取、取消 | 研究策略、Goal 状态 |
| detached Worker | 执行命令、heartbeat、metrics、终态 | 唤醒 Codex |
| Goal Wake Listener | run/thread 绑定、等待终态、恢复一次 Goal | cycle、idea、指标判断 |
| Codex App Server | thread/Goal 持久状态和 turn 执行控制面 | 实验执行 |

## 3. 完整时序

```text
Codex Goal(active)
  │
  ├─ 研究、实现候选
  │
  ├─ auto-research start(...)
  │    ├─ Runner 持久化 run.json
  │    ├─ detached Worker 启动
  │    └─ one-shot Listener 启动并绑定 thread_id
  │
  ├─ 检查 heartbeat / worker.log
  └─ Codex 主动暂停 Goal

Worker
  └─ events/completed.json | failed.json | timeout.json | cancelled.json | lost.json

Listener
  ├─ thread/resume(thread_id)
  ├─ thread/goal/get(thread_id)
  ├─ thread/read(thread_id)
  ├─ thread/goal/set(status=active)     # 仅 paused 时
  ├─ turn/start(run_id + result path)
  └─ 监听 goal updated，直到 paused/complete/blocked/limited 后退出
```

Listener 只显式创建一个恢复 turn。Goal 后续若产生原生续 turn，Listener 只保持 App Server 执行宿主存活，不负责创建研究循环。

## 4. thread 绑定

绑定优先级：

1. `wake.json` 已持久化的 `thread_id`。
2. CLI/MCP 显式 `thread_id`。
3. 当前 Codex 工具环境的 `CODEX_THREAD_ID`。
4. `thread/list(cwd=<project>)` 中最近的 active/paused Goal。

自动发现只接受与 run 创建时间接近的候选。多个候选时间相同或候选过旧时，Listener 保持 `BINDING_RETRY`，等待显式绑定，不采用“最新历史会话”猜测。

`run_id -> thread_id` 写入每个 run 的 `wake.json`，之后恢复不再重新选择任务。

## 5. 为什么 Listener 需要保持 App Server 存活

`thread/goal/set(status=active)` 只改变持久 Goal 状态；`turn/start` 才开始生成。通过 stdio 启动的 App Server 同时是本次恢复执行的宿主，如果在 `turn/start` 后立即终止，可能留下一个 UI 中显示 active、实际没有执行进程的任务。

因此 Listener 在发起恢复 turn 后继续读取 App Server 事件，直到 Goal 再次进入：

- `paused`
- `complete`
- `blocked`
- `usageLimited`
- `budgetLimited`

这不是模型轮询，不产生额外 token。

## 6. 幂等与竞态

每个 run 使用 `.wake-listener.lock` 保证只有一个 Listener。`wake.json` 记录：

- `thread_id`
- `state`
- `terminal_status`
- `wake_turn_id`
- `attempts`
- `last_error`

恢复前读取 `thread/read`：

- thread 已 active 且没有本 Listener 的 `wake_turn_id`：认为用户或其他恢复器已接管，标记 `SKIPPED`。
- Goal complete/blocked/limited：不自动激活，标记 `SKIPPED`。
- Listener 在 `turn/start` 后断线：保留 `wake_turn_id`；重启时优先附着已有 active turn，不创建第二个 turn。

## 7. 失败模型

| 故障 | 行为 |
|---|---|
| 终端或 Codex turn 关闭 | Worker 和 Listener 均为 detached，不受影响 |
| 实验失败/超时/被 kill | 写终态或最终进入 LOST，然后照常唤醒 Goal |
| App Server 暂时不可用 | 本地指数退避重试，不调用模型 |
| Listener 崩溃/机器重启 | `recover-wakes` 从 run/wake 文件恢复 |
| thread 候选不明确 | 不猜测；持续 `BINDING_RETRY` 或显式 `--thread-id` |
| 重复终态文件事件 | 单实例锁和终态锁阻止重复 turn |
| Goal 已被人工恢复 | thread active 时 Listener 跳过重复唤醒 |

## 8. 被删除的 v0.2 职责

v0.3 不再包含：

- `GoalHarness`
- `max_cycles`
- `active_harness_cycle.json`
- 每 cycle 一个实验的控制器约束
- Harness 自动暂停 Goal
- Harness 对目标完成、连续失败、plateau 的判断
- `goal_contract.json` / `goal_decision.json` 格式修复 turn

实验预算和硬指标仍可写在 `goal.json`，由 Codex Goal 判断。Runner 仅在 run 中保留目标快照用于追溯，不把它升级为外部研究流程。

## 9. 代码映射

| 能力 | 文件 |
|---|---|
| CLI | `src/auto_research/cli.py` |
| 配置 | `src/auto_research/config.py` |
| detached Runner | `src/auto_research/runner.py` |
| Worker | `src/auto_research/runner_worker.py` |
| Wake Listener | `src/auto_research/wake_listener.py` |
| App Server 最小客户端 | `src/auto_research/app_server.py` |
| 可选 MCP | `src/auto_research/mcp_server.py` |

## 10. 验收标准

1. `start` 立即返回 run id，Worker 和 Listener 都脱离当前终端。
2. 当前 Codex thread id 被持久化，不会绑定历史 Goal。
3. 实验运行数小时期间没有 Codex turn 或 MCP 状态轮询。
4. 所有终态都会触发一次恢复，包括失败和 LOST。
5. 恢复后是同一个 thread，Listener 不创建新研究会话。
6. Codex 再次暂停或完成后，本次 Listener 自动退出。
7. Listener/App Server 中断后可通过 `recover-wakes` 恢复且不重复启动 turn。
