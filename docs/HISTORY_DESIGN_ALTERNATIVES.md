# 自动实验优化 Agent 历史方案

本文档保存已经讨论过、但不属于当前默认运行链路的设计。当前方案请阅读 [CODEX_AUTO_RESEARCH_AGENT_DESIGN.md](CODEX_AUTO_RESEARCH_AGENT_DESIGN.md)。

## 1. Python Director + Codex SDK

早期方案将 Python Director 作为主 Agent：Director 维护搜索状态、选择候选、分配 explore/exploit/replicate，并调用 Codex SDK thread 完成研究推理和代码修改。

```text
Python Director
    ↓
Codex SDK thread
    ↓
Local Runner / Watcher
    ↓
结果事件与 ledger
```

这个方案适合无 UI、需要完全由 Python 控制线程生命周期的场景。缺点是需要自行实现 Goal 管理、上下文恢复、结果分析 turn 和调度逻辑，不能直接复用 Codex Goal 的持续研究能力。

## 2. Agents SDK + Codex MCP Server

第二种方案由 Agents SDK 中的 Research Director 作为主 Agent，Codex 通过 MCP 作为 coding specialist。适合需要多个 specialist、trace、handoff 和 guardrail 的场景。此时 `/goal` 只是 Codex thread 的持久目标，不是外层 Agents SDK 的调度器。

```text
Agents SDK Research Director
    ↓ MCP
Codex MCP Server / Codex Thread
    ↓ shell / workspace
Local Runner
```

## 3. 外部 Watcher + Goal 暂停/恢复

曾考虑由 Goal 启动后台实验，暂停 Goal，由 Watcher 监听完成事件，再恢复 Goal 并注入实验结果。该方案依赖 Goal pause/resume 接口，会引入会话绑定、幂等和恢复复杂度。

早期曾考虑由 Experiment MCP 提供 `run_experiment_and_wait`，在工具内部阻塞等待本地终态事件。该接口会让 Codex turn 长时间保持打开，已从当前实现删除。当前只使用 `start_experiment` 返回 `run_id`，再由 Harness 监听终态事件并恢复 Goal。

## 4. 方案演进

| 阶段 | 主调度者 | 实验执行方式 | 当前状态 |
|---|---|---|---|
| A | Python Director | Codex SDK + Runner | 仅保留设计记录，代码已删除 |
| B | Agents SDK Director | Codex MCP Server + Runner | 仅保留设计记录，未作为当前实现 |
| C | Codex Goal | Experiment MCP + detached worker | 当前默认方案 |
