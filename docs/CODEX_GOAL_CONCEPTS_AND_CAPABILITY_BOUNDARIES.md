# Codex Goal、Turn、连接与宿主功能边界

> 结论日期：2026-08-11。本文区分可持久状态、当前执行事实和宿主私有能力，避免
> 再用某个 JSON 文件或另一条连接的读数推断 Desktop 当前正在做什么。

## 1. 核心对象

| 对象 | 含义 | 是否直接代表模型在运行 |
|---|---|---|
| Thread / task | 持久对话、Turn 历史与项目 cwd | 否 |
| Goal | objective、预算和生命周期语义 | 否 |
| Turn | 一次具体 generation 和工具执行 | 是，处于 in-progress 时 |
| App Server connection | 某个客户端到 App Server 的实时 JSONL 连接 | 否 |
| host | Desktop、CLI、App Server client 等执行宿主 | 只有它创建 Turn 时才是 writer |

一个 Thread 可以先后被多条连接读取或恢复。连接不是 Thread；连接关闭也不删除
Thread。多个宿主能看到相同持久历史，但它们的事件订阅、注入工具和 scheduler
上下文并不因此共享。

## 2. Turn 状态与 Goal 状态

Turn 的关键状态是 `inProgress` 和终态 `completed` / `failed` /
`interrupted`。`turn/completed` 只说明这一次 generation 结束，不说明整个 Goal
完成。

Goal 的持久状态用于表达目标生命周期。实际观察和接口中出现过 `active`、
`paused`、`blocked`、`complete`/`completed`、`limited` 等语义。具体枚举可能随
Codex 版本变化，调用方应按当前协议校验，不能把 UI 文案当稳定 RPC 契约。

关键结论：

- Goal `active` 不等于存在 in-progress Turn。
- Goal `paused` 或 `blocked` 不等于某个普通 Turn 无法被宿主显式创建。
- `update_goal(blocked)` 与 Desktop 显示的暂停效果可以相似，但 `blocked` 是模型
  可写的目标终态/停点语义，不是一个通用的原生 pause/resume 调度 API。
- `/goal resume` 作为普通消息时只是文本；除非宿主命令解析器明确接管，否则不会
  神奇地修改 Goal 状态。

## 3. persisted、effective 与实时连接

`persisted state` 是能被后续连接读取的持久记录。`effective state` 是某个具体宿主
此刻据其 scheduler、writer、连接订阅和本地 UI 得出的运行效果。

因此可能出现：独立 App Server 把持久 Goal 写成 `active`，随后也能读回 active，
但 Desktop UI 仍显示 blocked，且没有 continuation Turn。这不是证明 Desktop 已
被唤醒，而是两个宿主没有共享同一个 scheduler/effective execution context。

判断“研究已重新运行”必须观察新的执行事实，例如：

1. `turn/start` 成功返回准确的 Turn ID；
2. 同一 App Server 连接收到该 Turn 的 lifecycle 事件；
3. 最终收到该 ID 的 `turn/completed`。

仅看到 `thread_goal_updated(active)` 是 Goal 持久状态证据，不足以证明新 Turn 已
创建。Goal-origin Turn 是执行证据，但历史日志里出现它也不能证明当前仍在运行。

## 4. App Server API 的精确边界

| API | 做什么 | 不做什么 |
|---|---|---|
| `thread/start` | 创建 Thread | 不启动 Turn |
| `thread/resume` | 在当前 App Server host 加载已有 Thread | 不自动生成 |
| `thread/read` | 读取持久 Thread/Turn | 不取得 writer |
| `thread/goal/set` | 写 Goal objective/status | `active` 不保证 scheduler 创建 Turn |
| `turn/start` | 显式创建 generation | 不代表整个目标自动循环 |
| `turn/steer` | 给精确的 in-progress Turn 增加输入 | 不唤醒 idle Thread |
| `thread/inject_items` | 写入历史 item | 不启动模型 |
| `turn/interrupt` | 中断精确 Turn | 不等于完成或暂停整个 Goal |

所以 App Server 本身不会“自动 scheduler”。外部 Supervisor 必须消费事件、等待
外部任务、判断控制 handoff，并在确定时机再次调用 `turn/start`。

## 5. Desktop 宿主私有工具

历史对话中的“由 ChatGPT 从另一项任务发送 `/goal pause`”来自 Desktop 宿主向
Agent 注入的 `codex_app__send_message_to_thread` 一类工具。该调用的效果是向目标
Thread 创建普通消息/Turn，不证明 `/goal pause` 是 App Server RPC，也不证明 Goal
状态发生了原生 pause。

这些工具属于宿主能力，不会写入 Thread 历史后被另一个 App Server 连接继承。
因此以下中转都不能构成确定性桥：

- 用 App Server 创建临时 Thread，再要求其调用 Desktop host tool；
- 从普通 MCP server 猜测或调用未公开的 Desktop 内部接口；
- 发送 `/goal resume` 文本并假定 Desktop 命令解析器一定执行；
- 只读 session JSONL 推断当前 scheduler 状态。

若不能修改 Desktop，可靠方案是让 App Server Supervisor 独占一个专用研究
Thread，并直接使用公开 `turn/start`，而不是尝试激活 Desktop Goal。

## 6. Listener 与 Supervisor 的结论

Listener 方案：等待实验终态，然后 `thread/goal/set(active)`。它能可靠改变持久
Goal，却不能可靠要求 Desktop 创建 continuation Turn。因此冻结在 `listener`
分支，适合作为实验与历史实现参考，不再作为 main 的自动调度保证。

Supervisor 方案：

- 自己持有项目级 scheduler lock；
- 自己持有专用 Thread 的唯一 writer；
- 用 `turn/start` 创建每次研究 Turn；
- 用结构化 handoff 决定等待、继续、停点或完成；
- 实验期间只等待 durable terminal event；
- 不把 Goal active/paused/blocked 当作跨宿主唤醒信号。

这不是把研究判断硬编码进 Python。Codex 仍选择 idea、修改代码、解释结果并判断
完成；Supervisor 只负责生命周期和可恢复调度。

## 7. 功能边界速查

| 需求 | 当前可靠实现 |
|---|---|
| 给同一运行 Turn 补充信息 | `turn/steer` |
| 给 idle Thread 开始新工作 | `turn/start` |
| 等长实验且不消耗模型 | Worker terminal event + Supervisor local wait |
| 跨进程恢复研究 | durable state + `thread/resume` + reconciliation |
| 修改 Goal objective/status | `thread/goal/set` |
| 确认 Goal 已在执行 | 检查精确 Turn lifecycle，而非 Goal status |
| 从 App Server 调 Desktop 私有 send-message | 当前无公开确定性接口 |
| 多实验并行汇合 | v0.4 暂不支持；默认一个活动实验 |

官方参考：

- [App Server lifecycle](https://learn.chatgpt.com/docs/app-server#lifecycle-overview)
- [App Server Turns](https://learn.chatgpt.com/docs/app-server#turns)
- [Codex remote control](https://learn.chatgpt.com/docs/developer-commands#codex-remote-control)
