# v0.1/v0.2 历史问题与修复记录

本文档记录完整 GoalHarness 在 v0.1/v0.2 中遇到的问题。现行默认设计见
[App Server Supervisor](AUTO_RESEARCH_SUPERVISOR_SCHEDULER_DESIGN.md)。旧 Listener 已从
代码、CLI、配置和测试中删除，只在本文保留必要的历史原因。

## 2026-08-14：最小控制状态、paused/blocked 规则与运行安全

- `state.json` 从运行阶段集合收敛为 `OPEN / NEEDS_USER / COMPLETED`；Goal、Turn、Worker
  和交付进度分别读取 App Server、run events 与 registry 事实。
- 终态先尽力注入轻量通知；paused Goal 随后无条件激活，blocked Goal 永不自动恢复。
  多个同时终态只激活一次，通知失败不阻断恢复。
- Supervisor 启动增加 launch lock、PID start-time 身份和 `OPERATIONAL` 门槛；顶层异常
  持久化并尽力创建 repair Turn。提交已落盘后启动失败返回同一个 run id 与
  `REPAIR_PENDING`。
- Runner 对 `run.json` 统一使用 per-run read-modify-write，避免 worker/child PID 覆盖；
  cancel 校验进程身份并确认退出后再提交终态。registry 损坏改为失败关闭。
- session runtime 只验证现有 binding；新 Goal cycle 只能显式 restart。Thread 创建使用稳定
  creation key，run 固化 `goal_cycle_id`，Goal 状态桥改成按 request id 的队列。

## 2026-08-14：设计原则审查后的永久停滞修复

- `NEEDS_USER` 现在只停止没有新终态依据的 Goal 自动推进，不再阻止 durable run 启动、
  监控、终态注入和清理；终态仍按实时 paused Goal 规则尝试一次唤醒。
- Worker watchdog 同时使用 PID start ticks、deadline 和 heartbeat。alive Worker 超过
  deadline 后先核验并终止 child/Worker，成功才提交 `TIMEOUT`。
- Goal Turn 增加默认 1800 秒的可配置无进展上限；快照变化会刷新期限。普通 Turn 卡死后
  只创建一次 repair Turn，repair 再卡死进入 `NEEDS_USER`。进程级 repair 等待也持续
  reconciliation runs；repair 失败时已有实验继续监控到终态，终态创建的 repair Turn
  重新接入 watchdog。
- 删除 `wait_requested` 对 Goal 的推测控制、旧 `wake.json` 展示字段、未调用 foreign wait、
  shell allowlist、七天 timeout 上限和未使用的 `GoalSpec`。同步只读
  `auto-research wait` 保留，不构成第二套 Goal control plane。

## 2026-08-14：终态唤醒竞态、死亡 Supervisor 与系统级串行限制

- 现象：实验完成后 native activation 已经创建 continuation，但紧随其后的 Goal 读取
  仍返回旧 `paused`；Supervisor 因而以 `GOAL_PAUSED` 退出。新 Turn 随后提交的 run
  只留下 `SUBMITTED`，因为没有 Supervisor 启动 Worker。
- 根因：单一 `active_experiment.json` 同时承担并发锁、等待交接和终态所有权；终态
  路径先删 marker，再激活 Goal，并把一次即时 Goal 读取误当作 continuation 证据。
  `process.json` 还可能在进程退出后残留。
- 修复：活动运行改为 `active_experiments.json` 的按 `run_id` 注册表；Supervisor 不实施
  实验数量限制。终态先注入并记录交付进度，再激活；只有交付和 wake/repair 被接受后
  才删除对应 entry。激活后的成功证据是新 `turn/started`，旧 `paused` 读取不再导致
  退出。`submit` 会验证 PID 和 READY，Supervisor 不存在时自动启动；子进程退出时清理
  自己的 `process.json`。
- MCP 边界：保留无需人工授权的查询和取消；二者都不能删除 registry entry 或修改
  Goal。暂停的唯一入口仍是 `auto-research goal set-status paused`，并作用于该 Task
  当前所有 active runs。
- 验证：覆盖多个 active run、查询/取消不消费终态、旧 paused 读取下的真实
  continuation，以及多 run pause handoff。

## 2026-08-13：blocked Goal 被 Supervisor 反复激活并空转

- 现象：Agent 经过严格审计真实调用 `update_goal(blocked)` 后，Supervisor 每隔数秒又
  将同一 Goal 设为 `active`。原生 scheduler 随即创建新 Turn；Agent 根据刚才的真实
  历史只回复“Goal 仍为 blocked”，形成无工具调用、无实验提交的无限 continuation。
- 根因：`_after_goal_turn()` 和 bootstrap 把所有非 `active` 状态统一送入激活/repair
  路径，混淆了“Agent 的终态决定”和“实验终态后的必要唤醒”。测试也把启动时自动
  激活 blocked Goal 当作正确行为，因而没有覆盖该循环。
- 当时修复：无活动实验时尊重 `blocked` 和已有控制器的 `paused`，停止创建 Turn；已有
  Worker 即使 Goal 停止仍监控到终态。后续最小状态设计已删除 `GOAL_BLOCKED`、
  `GOAL_PAUSED` 本地副本：现在实时 Goal 是唯一事实源，终态只激活 `paused`，`blocked`
  永不自动恢复。
- 防误判：Goal 提示词明确“缺少 WCS/标注平台访问本身不是 blocker”，只要本地固定
  快照、实验产物或未验证方法仍可推进就必须继续。
- 验证：新增 bootstrap blocked、Turn 后 blocked、Turn 后 paused、已有 paused 重启、
  实验终态 blocked 唤醒和 repair 拒绝测试，并运行完整 pytest。

## 2026-08-11：MCP Worker 所有权回归

- 现象：managed Goal Turn 调用公开 `start_experiment` 后，MCP/工具进程直接创建的
  Worker 在调用生命周期结束时被宿主回收；没有 `started.json` 或 heartbeat，实验未
  真正进入 GPU 执行。
- 对照：同一个 run 保持 `SUBMITTED`，再由长期运行的 Supervisor 调用 Runner launch
  后可以稳定完成。
- 修复：删除公开 MCP `start_experiment` 和 CLI `start`；替换为纯持久化的
  `submit_experiment` / `auto-research submit`。Codex 负责生成命令，Supervisor 是唯一
  Worker 启动者，Goal Turn、MCP server 和普通 shell 都不得直接启动长实验。
- 测试：提交结果必须是 `SUBMITTED`、`launch_worker=false`、
  `worker_owner=supervisor`；非 Supervisor task 提交必须 fail closed。

## 2026-08-11：订阅变化导致 Goal continuation 模型不可用

- 现象：实验终态、结果注入和 Goal `active` 均成功，但新 continuation 使用全局
  `gpt-5.6-sol` 后返回账户不支持错误，Goal 进入 `blocked`，后续实验未提交。
- 修复：新增 `[codex].model`，默认 `gpt-5.6-terra`；Supervisor 和 session
  bootstrap 均先调用 `model/list`，再把已验证模型显式传入 `thread/start` 和
  `thread/resume`。
- 状态修复：实验终态除清除 marker 外，同时将 `active_run_id` 和
  `waiting_run_id` 置空，避免 `NEEDS_USER` 状态继续展示已完成 run。

## 固定历史状态

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
- 将不同历史方案拆到 `docs/HISTORY_DESIGN_ALTERNATIVES.md`；当前架构只在
  `docs/AUTO_RESEARCH_SUPERVISOR_SCHEDULER_DESIGN.md` 定义。

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

`active_experiment.json` 被截断时原来可能让 MCP 服务失效。当时的修复把 marker 作为恢复提示，损坏时删除，并扫描 `SUBMITTED/RUNNING` run 恢复活动实验；该单 marker 方案已被 2026-08-14 的多-run registry 取代。

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

### 19. 中断 turn 后 Goal 仍为 active

问题：`turn/interrupt` 只结束当前 turn，不会改变 thread 上的 Goal 状态。旧实现虽然在实验落盘后中断 turn，但 Goal 仍保持 `active`；关闭 App Server 或在桌面端重新打开任务时，仍可能出现非预期续跑。恢复含 `pending_run_id` 的 Harness 时，如果过早同步目标，也可能在几个小时的实验等待期间把 Goal 唤醒。

修复：实验落盘后先把同一 thread 的 Goal 设置为 `paused`，再中断当前 turn；`goal_decision.json` 落盘后设置为 `complete`。关闭 App Server 时会 best-effort 暂停非终态 Goal，并在首次暂停失败时于中断后重试；恢复带有 `pending_run_id` 的 thread 时也会重新确认 paused。实验终态到达后，仅在下一次 `turn/start` 前重新设置为 `active`。目标 contract 内容同步不再携带 status，避免意外改变 paused 状态。Harness 状态新增 `goal_status` 和 `goal_status_changed_at` 作为 App Server 确认记录。

验证：`test_app_server_interrupts_turn_after_durable_run_is_discovered`、`test_app_server_marks_goal_complete_after_durable_decision`、`test_app_server_close_pauses_goal_without_active_turn`、`test_goal_objective_update_preserves_paused_status`、`test_goal_harness_reactivates_goal_only_before_next_turn`、`test_goal_harness_reconfirms_pause_before_waiting_for_resumed_run`。

### 20. 本地会话快照可能与 App Server 实际状态漂移

问题：Harness 过去只保存 `thread_id`、turn 事件和最后一次 `thread/goal/set` 的响应。用户从桌面端操作、旧 App Server 连接断开或暂停请求未送达后，`goal_harness.json` 可能落后于真实 thread/Goal；恢复时直接按本地状态推进，可能与遗留 in-progress turn 冲突，或错误激活 blocked/limited/complete Goal。

修复：恢复已有 thread 时一次性调用 `thread/goal/get` 和 `thread/read(includeTurns=true)` 对账。`pending_run_id` 存在时先重新提交 paused；发现单个遗留 turn 时暂停 active Goal、interrupt 并复读确认 idle。运行期消费 `thread/status/changed` 和 `thread/goal/updated`，把 API 已确认状态写回本地。多个 active turn、systemError、无法静止或缺失 Goal 被视为显式启动故障；blocked/limited Goal 进入 `GOAL_SUSPENDED`。Goal complete 但缺少合法本地决策时，只启动禁止实验的 decision-repair turn，避免格式错误导致永久停死。

验证：`test_app_server_reads_thread_and_goal_state_from_api`、`test_goal_harness_reconciles_and_interrupts_orphaned_turn`、`test_recovered_turn_remains_active_when_first_interrupt_fails`、`test_goal_harness_reconfirms_pause_before_waiting_for_resumed_run`、`test_complete_goal_without_valid_decision_gets_repair_turn`。

### 21. 任意 state root 造成同一 Thread 多套控制状态

问题：调用方可以通过 `--state-root` 自由选择 session、Supervisor、run registry 和
Worker 目录。同一 App Server Thread 因而可能被绑定到多个目录；Goal 内提示词若保留
旧目录，暂停、终态注入和重启还会各自读取不同事实源。

修复：状态目录唯一映射为 `research/supervisors/<thread-id>/`。Goal Turn 只使用
`CODEX_THREAD_ID`，task 外运维显式传 `--thread-id`；删除所有 `--state-root` 和历史
session mode。首次 `thread/start` 返回后立即原子写入 `supervisor_session.json`、
`metadata.json` 与 cycle；同一 Thread 的下一轮 Goal 只新增 cycle，复用 runs。进程生命周期
拆到 `supervisor_process.py`，Supervisor 状态机保持单一职责。

验证：覆盖新建、adopt、同 Thread 多 cycle、completed restart、重复 start、损坏绑定、
并发 bootstrap、外部 Thread 运维和 CLI 拒绝 `--state-root`；完整测试集 68 项通过。

## 验证标准

```bash
cd "/Users/dollars/work/Auto Research"
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
ps -axo pid,etime,command | rg 'goal-harness|app-server|auto_research.mcp' || true
```

当前关键不变量：一个 Harness cycle 最多提交一个实验；长实验不依赖 Codex/MCP 轮询；所有实验最终落盘 terminal event；状态统计可从 runs 目录重建。

## v0.3 真实闭环验证补充

### 独立 App Server 与桌面端争抢 thread writer

问题：v0.3 初版 Listener 在终态后依次调用 `thread/resume`、`thread/goal/set(active)` 和 `turn/start`。真实 digits Goal 验证中，算法 run 已正常 `COMPLETED`，但桌面端仍拥有同一 thread 的 writer，Listener 连续收到 `thread ... already has an active writer`，停在 `WAKE_RETRY`。这证明“外部 Listener 自建恢复 turn”仍保留了不必要的执行所有权。

修复：Listener 现在只核对持久 Goal/thread 状态并调用 `thread/goal/set(status=active)`，写入 `ACTIVATED` 后退出。continuation turn 完全由原生 Goal scheduler 创建；同时删除 Listener 中的 `thread/resume`、`turn/start`、长 turn 等待，以及已经没有作用的 Codex 模型、reasoning effort、sandbox、approval 和 resumed-turn timeout 配置。

后续核对同一 session 又发现：在活跃提交 turn 内写入 paused 虽然 RPC 成功，但 turn finalization 仍可能发布最新 Goal usage/state并触发原生 continuation。Listener 因此在启动时记录 Goal usage、请求 paused，并等待提交 turn 的 usage 或状态发生持久变化后再次确认 paused；只有完成该 `PAUSE_HANDOFF` 才进入实验等待。终态后再写 active，形成完整的 paused/active 双边桥。

验证：`test_wake_listener_binds_explicit_thread_and_wakes_once` 断言只发生一次 Goal 激活且没有 resume/turn 调用；真实验证任务位于 `digits_goal_demo_task/`。
## v0.3：项目专用会话只创建一次

问题：完全禁止当前版本创建 thread 可以杜绝历史重复会话，却要求用户先手动建 task、查找 thread id 并完成绑定；如果简单恢复 `thread/start`，重试、并发启动或初始化中途失败又可能重新制造多个空 Goal task。

修复：新增独立 Session Bootstrap 和 `auto-research session`。只有显式 `--create-thread` 才允许缺失时创建；`research/codex_session.json` 与文件锁保证重复调用和并发调用复用同一 thread。取得 thread id 后先持久化，再命名和初始化 paused Goal。已有 task 可由 `--thread-id` 采用；状态损坏、项目 cwd 不符、当前 task 与专用 task 不一致时均失败关闭。Listener 仍不调用 `thread/start` 或 `turn/start`。

### 22. 终态注入、Worker ownership 与人工额度恢复不收敛

问题：把 Thread 注入当成必须成功的结果交付，会在完整终态已经持久化时不必要地停止
研究。Supervisor 在 registry 提交中断后也可能漏掉已经 durable 的 `SUBMITTED/RUNNING` run。
取消、超时和 LOST 路径若在无法确认进程退出时仍写终态，会释放仍存活 Worker/child 的
ownership。额度限制恢复还曾依赖旧 marker，导致结果已注入但 marker 清除后无法继续。

修复：Thread 中只尽力注入 `run_id/status/result_dir`，失败写诊断文件但继续 Goal 恢复；
完整结果由 Codex 主动读取 run 目录。启动时从 Thread root 扫描 unfinished run 并补回
registry。只有 PID 与 start ticks 证明 Worker 已死或 PID 已复用，且 child
清理成功时才提交 LOST；身份不可核验、元数据损坏或清理失败均 fail closed。结果成功注入
后 marker 可以清除；额度恢复由用户手动 `supervisor start/resume` 发起一次实时 Goal
激活，实时状态仍是 paused 时同样适用，失败即回到 `NEEDS_USER`，不自动循环。第二个
Supervisor 进程拿不到 scheduler lock 时只返回 `ALREADY_OWNED`，不得改写唯一 owner 的
共享状态或创建 repair Turn。

验证：覆盖轻量通知字段、通知失败后继续唤醒、批量部分失败、手动 limited/paused 单次恢复、orphan
SUBMITTED 接管、dead Worker LOST、不可核验 Worker、取消清理失败和重复进程旁路；当前
测试集在删除 11 条 legacy Listener 兼容测试后为 88 项。真实旁路 smoke test 同时确认
拒绝前后 `state.json` 哈希不变。
