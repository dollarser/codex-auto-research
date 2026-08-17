# Codex Goal、Goal Runtime、Turn 与宿主边界

> 结论日期：2026-08-11；本机验证版本：`codex-cli 0.144.5`，`goals stable true`。

本文使用的 Thread、Turn、Item、Goal、continuation、daemon、connection、
persisted/effective 及各层状态，统一定义见
[Codex 术语与 Auto Research 状态边界](CODEX_TERMINOLOGY.md)。尤其注意：
`Turn=inProgress`、`Goal=active`、`Supervisor=OPEN` 和 `Run=RUNNING` 属于四个不同对象。

## 1. 对象与职责

| 对象 | 职责 |
|---|---|
| Thread | 持久历史、Turn 和项目 cwd |
| Goal | objective、status、token/time accounting |
| Goal runtime | active Goal 空闲时自动创建 continuation |
| Turn | 一次真实模型 generation 和工具执行 |
| App Server daemon | 保存进程内 Thread/Goal runtime 和事件流 |
| daemon connection | 连接同一 daemon 的 WebSocket 客户端，不是独立 scheduler |
| Desktop host | 另一种宿主；不参与本方案 |

Goal continuation 不是 Goal status。它是 Goal runtime 根据 active Goal 创建的一种
Turn。

## 2. 当前 Goal 状态

当前 schema 枚举：

```text
active
paused
blocked
usageLimited
budgetLimited
complete
```

- `active`：Goal runtime 可以在 Thread idle 时继续。
- `paused`：保留目标和 accounting，但不再产生 continuation。
- `blocked`：等待外部干预或 Turn terminal error。
- `usageLimited` / `budgetLimited`：资源边界停止。
- `complete`：目标完成。

## 3. App Server 确实包含 Goal scheduler

通用 App Server 生命周期文档强调普通客户端通过 `turn/start` 驱动会话，但 Goal
extension 还提供了内部自动路径：

1. 外部 `thread/goal/set(active)`；
2. Goal runtime 记录 idle active Goal；
3. 调用 `continue_if_idle()`；
4. 通过 `try_start_turn_if_idle()` 创建 Goal continuation；
5. 发出标准 `turn/started` 和 `turn/completed`。

`thread/resume` 也会恢复 active Goal accounting，并在 Thread idle 时触发该路径。
因此，“普通 App Server API 需要 `turn/start`”与“active Goal 可以自动 continuation”
同时成立，分别属于普通 Turn 与 Goal extension。

## 4. persisted 与 effective

Goal status 持久化在共享 state DB；Goal runtime 的 idle check、active turn、semaphore
和 Thread map 是 App Server 进程内状态。

因此：

- 在进程 A 写入 active，不保证进程 B 的 Desktop UI/effective scheduler立即接管。
- 在同一个 managed daemon 内写 active，会调用该 daemon 的 Goal runtime。
- 多个独立 App Server 进程恢复同一 active Goal，反而可能产生重复 continuation。

这解释了历史 Listener 为何能写入 Desktop Thread 的 persisted active，却没有可靠
唤醒 Desktop：Listener 与 Desktop 是不同 App Server host。新方案不是跨 host
唤醒，而是让所有控制连接同一个 daemon。

## 5. API 边界

| API | 普通语义 | Goal runtime 相关效果 |
|---|---|---|
| `thread/start` | 创建 Thread | 不自动创建 Goal |
| `thread/resume` | 加载/订阅 Thread | 恢复 active Goal并触发 idle lifecycle |
| `thread/read` | 读取持久历史 | 不加载或调度 |
| `thread/goal/set(active)` | 更新持久 Goal | 同 daemon 内调用 `continue_if_idle()` |
| `thread/goal/set(paused)` | 更新持久 Goal | 清除 active accounting，停止后续 continuation |
| `turn/start` | 创建普通用户 Turn | 正常 continuation 禁止调用；仅 native 激活失败后的可审计 repair fallback 使用 |
| `thread/inject_items` | 追加后续模型可见历史 | 不单独启动 Turn |
| `turn/steer` | 给当前普通 Turn追加输入 | 不唤醒 idle Thread |

## 6. paused 与 blocked

等待正常长实验使用 `paused`，因为它是可预期、可恢复的控制状态。`blocked` 应保留
给真正需要外部判断、权限、修复或 terminal error 的情形。

模型侧 `update_goal(blocked)` 与 App Server 客户端
`thread/goal/set(paused)` 不是同一控制语义。实验工具通过 daemon WebSocket 设置
paused，不要求正在运行的模型原生拥有 Goal pause/resume 工具。Codex 所谓“自主暂停”是
自主决定后调用 Auto Research 的状态桥，最终由该桥执行 `thread/goal/set(paused)`。

同步等待实验结果与修改 Goal 是两个正交维度。`auto-research wait` 可以阻塞调用方直到
durable terminal event，但不写 Goal、不启动 Worker，也不承担终态唤醒 ownership；因此
它不是第二套 Goal 控制面。长实验仍优先使用 paused→active 交接，短时同步等待和人工诊断
可以使用阻塞观察入口。

## 7. Desktop 与 daemon 隔离

本方案不需要：

- Desktop Goal scheduler；
- `codex_app__send_message_to_thread`；
- `/goal pause` 或 `/goal resume` 文本；
- Desktop UI reflected/effective state；
- Supervisor 用普通 Turn 代替正常 Goal continuation。

唯一例外是实验终态已经注入、`paused → active` 已请求但 native continuation 未出现时，
Supervisor 可以创建一次带明确 run id 和错误原因的 repair Turn。它是故障上报，不是
正常调度路径，也不得用于恢复 `blocked` Goal。

专用研究 Thread 不应同时由 Desktop 的独立 App Server host恢复。需要观察时读取
持久记录或连接 managed daemon，而不是让另一个 host取得 writer。

## 8. 成功判断

激活链必须同时满足：

1. `thread/goal/set(active)` 返回 active；
2. 同一 daemon 发出该 Thread 的新 `turn/started`；
3. 该 Turn 最终发出 `turn/completed`。

第 1 项是状态证据，第 2、3 项才是执行证据。

参考：

- [App Server README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- [Goal runtime](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/src/runtime.rs)
- [App Server lifecycle](https://learn.chatgpt.com/docs/app-server#lifecycle-overview)
