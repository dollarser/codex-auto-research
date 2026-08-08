# 历史问题与修复记录

本文档是当前实现的唯一历史修复索引。设计取舍和已放弃方案见 [HISTORY_DESIGN_ALTERNATIVES.md](HISTORY_DESIGN_ALTERNATIVES.md)。

## 当前状态

- Harness 已停止；没有残留的 `goal-harness`、Codex App Server 或实验 MCP 进程。
- 最近一次 breast-cancer 任务留下 12 个终态实验，最佳主指标为 `0.9850104821802935`。
- 原 `goal_harness.json` 曾显示 5 个实验，原因是状态只在启动时做单调增加式统计；修复后以 `research/runs/*/run.json` 和终态事件为事实源重建计数。
- 最近一次 App Server 卡住的直接证据是 `APP_SERVER_STALLED`，并伴随 `dropping turn-scoped item for unknown turn id`。这属于 Goal/App Server 的 turn 边界和 Harness 所有权失配，不是 Worker 仍在运行。

## 会话压缩前已完成的历史修改

以下内容来自本项目此前连续会话中的实现和验证，因会话压缩没有全部显示在当前上下文中，现统一补录到这里。

### 架构与方案收敛

- 放弃以 `orze`/`orze_pro` 作为运行时基础，只保留其研究循环、实验隔离和恢复思路作为参考。
- 方案收敛为 `Codex Goal + Codex App Server + Experiment MCP + detached Worker`：Codex Goal 负责研究判断，Harness 负责生命周期，MCP 负责确定性实验操作，Worker 负责长实验。
- 评估过 CLI、SDK 和 MCP；当前 Harness 使用 `codex app-server --stdio`，避免每轮创建独立 CLI 会话，同时保留 thread/turn/Goal 的可恢复关系。
- 将不同历史方案拆到 `docs/HISTORY_DESIGN_ALTERNATIVES.md`，当前架构集中在 `docs/CODEX_AUTO_RESEARCH_AGENT_DESIGN.md`，并统一放入 `docs/` 目录。

### 目标、Goal 和硬性验收

- 增加实验前目标优化阶段：Codex 必须先审查用户目标、指标、数据切分、泄漏风险、资源限制和研究价值，再生成 `research/goal_contract.json`，不能直接把用户原话当作算法目标。
- 明确“用户原始数值要求”和“经确认的硬性数值门槛”不再重复维护；目标优化阶段直接确认并写入当前 contract 的 `hard_requirements`。
- Harness 只核验可机械判断的数值门槛，例如 `ap >= x`、`thr == 0.1` 条件下 `recall >= y`；主研究目标是否达成、指标是否重要、提升是否有意义由 Codex Goal 判断。
- 默认不要求 Codex 每轮实验后修改 Goal/contract；只有反馈明确证明目标、指标或约束不合理、不可测量或明显错位时才允许修订，并记录旧值、新值和证据。
- 增加 `goal_decision.json` 显式完成协议，避免 Harness 根据单个指标擅自宣布研究完成。

### 后台实验与事件驱动

- `start_experiment` 改为立即返回 `run_id`，不在 MCP 请求中等待数小时；Worker 在后台 detached 运行，终态通过 `completed/failed/timeout/cancelled/lost` 文件事件落盘。
- Harness 等待本地文件事件，不向 Codex 发起无意义轮询，也不让 Codex 查询 `RUNNING` 状态；只有终态事件到达后才恢复 Goal 并启动下一 turn。
- 增加 `heartbeat.json`、实验超时、宽限期和 `LOST` 收尾，解决 Worker 崩溃、终端断开、MCP 断开或事件长期不到达时永久等待的问题。
- 增加 `run.json`、stdout/stderr、worker 日志、metrics 和事件的持久化，使实验与 Harness、终端和 Codex 进程解耦。

### Runner、MCP 和并发安全

- 增加 `active_experiment.json` 与项目级提交锁，默认限制每个项目只运行一个活动实验。
- 增加 `idempotency_key`，处理 MCP 重试和 App Server 连接中断时的重复提交。
- 增加终态锁和唯一 terminal event 规则，修复完成、取消、超时同时发生时的竞态。
- 取消流程先落盘请求，再协调 Worker/child PID，避免实验尚未完成启动时取消请求失效。
- 增加 run id 校验、路径限制、worktree 必须位于项目目录内、metrics 必须是非空 JSON 对象等输入保护。
- 增加 MCP 的 `start_experiment`、`get_experiment_result`、`cancel_experiment` 三个工具，并实现 `register-mcp` 脚本；注册时只更新目标 MCP 配置并保留其他 Codex 配置。

### App Server 稳定性与恢复

- 增加 App Server response timeout、turn timeout、有限次重连和指数退避。
- 连接异常时先扫描磁盘上的新 run，确认 MCP 提交已持久化后不重复启动；对不确定是否已提交的 `turn/start` 不做盲目重试。
- 增加非交互式 server request 的自动拒绝/响应，避免 approval 或其他请求把 JSONL 流卡住。
- 状态持久化 `thread_id`、`turn_id`、`pending_run_id`、App Server stderr、return code、上下文和当前 phase，支持进程重启后恢复。
- 增加 `--fresh-thread`，在不删除实验、ledger 和 Harness 状态的前提下，绕开过大的历史 thread 上下文。
- 修正 Harness 不应因一个 turn 无实验就静默结束的问题：如果没有显式 `goal_decision.json` 且仍有预算，会在边界内重新推进 Goal。

### 配置、安全与可复现性

- 将模型、reasoning effort、sandbox、approval、超时、重连、Worker heartbeat 和 shell 策略集中到 `research/harness.toml`，并支持 `AUTO_RESEARCH_*` 环境变量覆盖。
- 默认模型和 reasoning effort 已显式写入配置；每次 Harness 状态和每个 run 都记录实际模型/effort，便于复现实验。
- `max_experiments` 收敛为 `goal.json` 的唯一配置来源，`max_cycles` 收敛为 `research/harness.toml` 的唯一配置来源，命令行只提供单次覆盖。
- 按当前隔离环境要求支持 `AUTO_RESEARCH_CODEX_SANDBOX=danger-full-access` 和 `shell=True`；同时保留 `shell=False + argv allowlist` 安全模式，实际执行参数写入 `run.json`。

### 真实任务验证记录

- 已用鸢尾花数据集验证基本实验链路，确认 MCP 返回 run id、Worker 后台运行、终态读取和下一轮恢复。
- 已用手写数字数据集验证较长的自动优化链路，覆盖失败实验、结果读取和 Goal 继续研究。
- 已用 breast-cancer 任务进行多轮真实优化，期间发现并修复历史 turn 统计、App Server 静默卡住、状态滞后和重复提交边界问题；当前磁盘上有 12 个终态 run，最佳主指标为 `0.9850104821802935`。

## 已修复问题

### 1. 历史 turn 的 MCP item 被误认为当前实验

恢复旧 thread 时，事件流可能包含历史 turn 的 `item/completed`；只扫描 `run_id` 会把历史实验计入当前 turn。现在按 App Server 的 `turn_id` 过滤 MCP item 和 `turn/completed`，只接受当前 turn 的事件。

验证：`test_event_turn_id_filters_replayed_history`、`test_app_server_client_ignores_previous_turn_mcp_items`。

### 2. JSON-RPC 响应读取阶段丢失 turn 事件

`turn/completed` 可能先于 `turn/start` 的 JSON-RPC response 到达，原同步读取器会丢弃通知。现在使用通知缓存队列，待 `turn/start` 取得真实 turn id 后继续处理。

验证：`test_app_server_buffers_turn_events_received_before_start_response`。

### 3. App Server 静默无响应导致无限等待或危险重试

stdout 保持打开但长期没有数据时，Harness 原来可能无限阻塞；不确定 `turn/start` 是否已提交时重试还可能产生重复 turn/实验。现在有 response watchdog 和 turn watchdog，超时会尝试 `turn/interrupt`，保存 `APP_SERVER_STALLED`、turn id、stderr 和上下文，不自动重试不确定的 `turn/start`。

### 4. Goal 自动续 turn 绕过 Harness 的实验边界

Goal/App Server 可以在一个长生命周期内继续产生多个实际 turn。仅靠提示词要求“每 turn 只调用一次”不是可靠约束；实验完成后 active marker 被释放，续 turn 仍可能再次提交。

现在 Harness 每个外层 cycle 写入 `research/active_harness_cycle.json`。MCP server 读取标记，并把 `harness_cycle_id` 固化到 `run.json`；同一 cycle 已有任何提交记录后，第二次 `start_experiment` 直接返回可恢复错误。Harness 完成该 turn 后删除标记，下一外层 cycle 才打开新的提交窗口。标记包含 Harness PID，旧进程死亡后的残留标记会自动清理。

这条保护的边界是“一个 Harness cycle 一个实验提交窗口”，不替代 Codex 对目标是否达成的判断。

验证：`test_experiment_service_rejects_second_submission_in_same_harness_cycle`。

### 5. 历史 run 统计污染当前 Harness 状态

`completed_runs` 原来只在新值更大时更新，不能纠正旧 turn、重复启动或历史任务造成的错误计数。现在每个 cycle 开始、turn 返回后和 App Server 超时时都从磁盘终态事件重建精确计数，并记录 `reconciled_at`。

### 6. MCP active marker 损坏或进程中途退出

`active_experiment.json` 被截断时原来可能让 MCP 服务失效。现在 marker 只作为恢复提示，损坏时删除；真实状态以 `run.json` 和终态事件为准，并扫描 `SUBMITTED/RUNNING` run 恢复活动实验。提交和终态写入使用文件锁与原子 JSON。

### 7. Worker 与终端/Harness 生命周期耦合

Worker 现在 detached、独立 session、保存 PID/heartbeat；终端、App Server 或 Harness 重启不会主动杀死长实验。超过实验 timeout 加 grace 后 Runner 生成 `LOST` 终态并清理残留进程。

### 8. 完成、取消和超时竞态

每个 run 使用 terminal lock，`finalize_run` 只允许一个终态事件获胜；取消先写 request，再在同一锁内协调 PID 和终态。

### 9. 大模型输出格式异常导致 Harness 直接退出

`goal_contract.json` 或 `goal_decision.json` 格式错误会被捕获并写入对应 error 文件，Harness 进入 repair 阶段，让 Codex 修复或删除文件；实验命令失败则作为 `FAILED/TIMEOUT/LOST` 结果交给 Codex 分析。

### 10. 配置重复和恢复线程上下文过大

`goal.json` 是 `max_experiments` 唯一配置源，`research/harness.toml` 是 `max_cycles` 唯一项目配置源，代码只提供默认值。模型、reasoning effort 和环境变量覆盖集中在 `config.py`。`--fresh-thread` 只换 Codex thread，不删除 durable runs、ledger 或 Harness state。

### 11. shell 执行和实验命令入口

安全模式使用 argv 白名单和 `shell=False`；隔离测试配置按当前要求可使用 `AUTO_RESEARCH_USE_SHELL=true` 与 `danger-full-access`。实际 `argv` 和 `shell` 会写入 `run.json` 便于审计。

### 12. Harness 启动阶段异常不落状态、锁不释放

问题：`initialize`、`thread/start`、`thread/resume` 或 `thread/goal/set` 在明确失败或静默超时时，原启动路径可能没有写入失败 phase；如果 App Server 在创建 thread 后的 `goal/set` 阶段卡住，thread id 也可能丢失，导致下一次只能重新猜测恢复对象。App Server 进程创建失败时还可能发生 Harness 锁未释放。

修复：启动 RPC 统一纳入启动事务；失败会写入 `APP_SERVER_STALLED` 或 `APP_SERVER_STARTUP_FAILED`，保存上下文、stderr、return code 和已创建的 thread id，并确保释放 Harness 锁。`thread/start` 返回后立即记录 thread id；所有启动阶段都使用已有 response watchdog。

### 13. 当前 turn id 只在超时时写入，导致卡死诊断指向历史 turn

问题：Harness 只有在 turn timeout 时才更新 `goal_harness.json.turn_id`。当前 turn 正在长时间无输出时，状态文件仍显示上一次历史 turn，容易误判和错误恢复。

修复：App Server 收到 `turn/start` response 后立即触发 Harness callback，写入当前 `turn_id` 和 `turn_started_at`；启动新 Harness 时清空旧的 live reason、stderr、return code 和 turn id，但保留 `last_result` 等研究历史。

验证：`test_app_server_client_ignores_previous_turn_mcp_items` 同时校验 turn-start callback；完整测试集 33 项通过。

### 14. Codex 已 `task_complete` 但 App Server 缺少 `turn/completed`

问题：实际 session 记录显示 Codex 已完成 `get_experiment_result`、写入 `goal_decision.json` 并产生 `task_complete`，但该 App Server 连接没有向 Harness 发出可识别的 `turn/completed`。Harness 因而长期停在 `GOAL_RUNNING`，active cycle 也无法关闭；这不是训练 Worker 卡死。

修复：当前 turn 收到带相同 `turn_id` 且 `phase=final_answer` 的 `item/completed` 时，将其作为缺失 `turn/completed` 的保守终态兜底；Harness 重启时若已有 `goal_decision.json(status=complete)`，直接恢复为 `COMPLETED`，不再删除决策后启动新实验。

验证：新增 `test_app_server_accepts_final_answer_when_turn_completed_is_missing`，完整测试集 34 项通过。

### 15. App Server turn 总超时过长，静默阶段无法快速收尾

问题：总 turn watchdog 为 900 秒，但 App Server 在 `turn/start` 已返回后可能长时间不产生任何生命周期或 item 事件；Harness 会在低 CPU、无 Worker 的情况下等待很久，难以及时区分模型推理延迟和协议卡死。

修复：增加独立的 `app_server_event_idle_timeout_s`，当前默认 180 秒。总 turn 仍允许最长 900 秒，但任意连续 180 秒没有事件就进入现有 `turn/interrupt` 与 `APP_SERVER_STALLED` 收尾路径。该阈值可通过 `AUTO_RESEARCH_APP_SERVER_EVENT_IDLE_TIMEOUT_S` 和 `research/harness.toml` 覆盖。

### 16. 后台实验运行时环境缺失，Codex 在失败阈值前没有诊断和 repair 机会

问题：由 `launchctl` 或其他最小环境启动 Harness 时，Worker 子进程继承的 PATH 可能找不到 `python`；改用系统 `python3` 又可能缺少项目依赖。此前 `get_experiment_result` 只返回通用的 `experiment exited with code N`，Codex 看不到 `stderr.log`；达到 `max_consecutive_failures` 后 Harness 又会立即停止，不再开启 repair turn。

修复：Runner 启动实验时把启动 Worker 的 Python 环境放在子进程 PATH 首位，并把有限长度的 stdout/stderr、command、argv 和 worktree 注入终态结果。连续失败首次达到阈值时，Harness 保留一次强制 repair turn，要求 Codex 先定位解释器、依赖、工作目录、命令、权限或代码根因并做 smoke test；repair 后仍失败才执行 `max_consecutive_failures` 停止保护。成功实验会清除 repair 状态。

验证：`test_runner_exposes_failure_diagnostics_and_venv_path`、`test_goal_harness_allows_one_repair_turn_at_failure_limit`。

### 17. 完成理由事实混淆与 launchd 正常退出重启循环

问题：`goal_decision.json` 只有自由文本 `reason`，Codex 可能把硬门槛连续未通过误写为执行失败次数；使用 `launchctl submit` 时，已正常结束的 Harness 还可能被重复拉起。

修复：完成决策现在必须包含结构化 `decision`、真实 `evidence_run_ids` 和 `hard_requirements_passed`，并拒绝 `decision=achieved` 但硬门槛为 false 的决策。Harness state 同时记录 `stop_reason`。新增 `scripts/run_background_harness.sh`，后台任务正常退出后自动卸载自身 launchd job，避免完成后的重启循环。

验证：`test_structured_goal_decision_is_required_and_validated`；完整测试集 40 项通过。

### 18. 后台重启制造多个未完成 Codex 会话

问题：旧后台命令反复携带 `--fresh-thread`，launchd 每次拉起都会执行 `thread/start`。同时，Harness 在 durable run 出现后提前返回，却没有中断仍活跃的 turn；关闭 App Server 也只是终止进程。这会留下多个服务端仍标记为 active 的 Goal/turn，桌面端重新打开后可能继续执行。

修复：合法完成决策现在会在创建 App Server 之前被消费；durable run 或完成决策成为明确 turn 边界，Harness 会 best-effort `turn/interrupt` 并记录 `turn_status`、`turn_finished_at`；关闭 App Server 前也会收尾 active turn。`--fresh-thread` 明确限制为人工替换上下文，自动恢复必须复用持久化 thread id。

验证：`test_app_server_interrupts_turn_after_durable_run_is_discovered`、`test_completed_decision_is_consumed_before_app_server_start`；完整测试集 42 项通过。

## 验证标准

```bash
cd "/Users/dollars/work/Auto Research"
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
ps -axo pid,etime,command | rg 'goal-harness|app-server|auto_research.mcp' || true
```

当前关键不变量：一个 Harness cycle 最多提交一个实验；长实验不依赖 Codex/MCP 轮询；所有实验最终落盘 terminal event；状态统计可从 runs 目录重建。
