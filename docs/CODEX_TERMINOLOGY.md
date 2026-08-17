# Codex 术语与 Auto Research 状态边界

本文是本仓库使用 Codex 名词时的统一解释。其他文档首次出现下列术语时，可以直接
引用本文，不再各自创造定义。

定义来源分为两类：

- **官方协议术语**：以 [Codex App Server 文档](https://developers.openai.com/codex/app-server)
  为准；
- **当前实现或本项目术语**：用于描述 Goal extension、Desktop host 和 Auto Research，
  不冒充稳定的公开 API 承诺。

## 1. Codex 核心对象

| 术语 | 本文含义 | 容易混淆之处 |
|---|---|---|
| Codex agent | 接收上下文、调用模型和工具并产出结果的执行主体 | 不是 Supervisor，也不是训练 Worker |
| host（宿主） | 承载 Codex 客户端或运行时的程序，例如 Desktop、CLI 或 App Server 集成 | 同一 Thread 可被不同 host 访问，但它们不一定共享进程内调度状态 |
| Thread | 一段持久会话，包含历史 Turn；Desktop UI 中也可能显示为 task、chat 或 conversation | Thread 本身不是一次模型执行，也不是 Goal |
| Thread ID | Thread 的稳定标识；Auto Research 用它选择唯一 canonical state root | 标题、最近活跃时间和当前 UI 焦点都不能替代 Thread ID |
| Turn | 一个用户请求或 Goal continuation 所触发的一次 agent 执行；包含模型生成和工具调用 | 一个 Goal 可以经历多个 Turn；Turn 结束不等于 Goal 完成 |
| Item | Turn 内的一项输入或输出，例如用户消息、agent 消息、命令、文件修改或工具调用 | Item 状态不是 Turn、Goal 或实验状态 |
| commentary | agent 在 Turn 进行中发出的阶段性消息 | 看到 commentary 只能证明已有输出，不能证明实验已启动 |
| final answer | 一个 Turn 的最终 agent 消息 | 它结束当前 Turn；只有 Goal 状态也为 `complete` 时才表示整个 Goal 完成 |

官方 App Server 对象关系是：

```text
Thread
└── Turn
    └── Item
```

## 2. Turn 和 Item 状态

### Turn 状态

| 状态 | 含义 |
|---|---|
| `inProgress` | 已收到 `turn/started`，该 Turn 正在执行且尚未进入终态 |
| `completed` | Turn 正常结束 |
| `interrupted` | Turn 被精确中断，例如调用 `turn/interrupt` |
| `failed` | Turn 因错误结束；错误信息随 `turn/completed` 返回 |

`inProgress` 只回答“这个 Turn 是否还在运行”。它不保证 Goal 是 `active`，不保证已经
提交实验，也不保证训练 Worker 为 `RUNNING`。计划步骤和某些 Item 也可能使用
`inProgress`，但它们属于各自对象，不能相互替代。

### Thread 的 active/idle 展示

某些宿主会把 Thread 汇总显示为 `active` 或 `idle`：

- `active`：宿主观察到该 Thread 当前有执行中的 Turn；
- `idle`：当前没有执行中的 Turn。

这是宿主层的实时摘要，不是 Goal status，也不是 Auto Research 的持久控制状态。

## 3. Goal 与 Goal runtime

| 术语 | 本文含义 |
|---|---|
| Goal | 绑定到 Thread 的持久长期目标，核心字段包括 objective、status 和用量 accounting |
| objective | Goal 要持续完成的目标文本；本项目首次创建 Goal 时以 `GOAL.md` 全文为事实源 |
| Goal status | Goal 自身的控制状态；与 Turn、Supervisor 和 Experiment Run 状态相互独立 |
| Goal runtime | App Server 进程内负责 active Goal idle 检查和 continuation 创建的运行时 |
| Goal scheduler | 本文对 Goal runtime 调度职责的简称，不是另一个独立进程 |
| Goal continuation | Goal runtime 为尚未完成的 active Goal 自动创建的新 Turn |
| native continuation | 由 Goal runtime 创建的 continuation，用来区别 Supervisor 故障时显式创建的 repair Turn |
| accounting | Goal 的 token、时间或预算使用记录；暂停 Goal 不等于删除这些记录 |

当前实现中使用的 Goal 状态：

| 状态 | 含义 |
|---|---|
| `active` | Goal runtime 可以在 Thread idle 时继续创建 continuation |
| `paused` | 保留 Goal，但停止后续 continuation；正常实验等待使用此状态 |
| `blocked` | 等待真正的外部干预、修复或 terminal error；普通实验结束不得自动恢复它 |
| `usageLimited` | 因账户或服务用量边界停止 |
| `budgetLimited` | 因 Goal 预算边界停止 |
| `complete` | 长期目标完成，不再创建 continuation |

关键区别：Goal `active` 不表示此刻一定有 Turn；Turn `completed` 也不表示 Goal
`complete`。只有 active Goal 再次变为 idle 时，Goal runtime 才可能创建下一次
continuation。

## 4. App Server、daemon 与 connection

| 术语 | 本文含义 |
|---|---|
| Codex App Server | 提供 Thread、Turn、Item、认证、审批和事件流的 JSON-RPC 服务 |
| daemon | 本机后台常驻的 App Server 进程；持有进程内 Goal runtime 和 active-turn 状态 |
| managed daemon | Auto Research 明确选择并复用的唯一 daemon，避免多个 runtime 同时调度同一 Goal |
| connection | 一个客户端到 daemon 的 WebSocket/stdio 连接；只是订阅者和请求通道 |
| Desktop host | Desktop 自己的宿主与运行时；它可以显示同一持久 Thread，但不自动等于 managed daemon |
| persisted state | 已写入共享存储、其他进程以后可以读到的状态 |
| effective state | 某一具体运行时此刻实际加载并据此调度的进程内状态 |

`persisted` 和 `effective` 是本项目的诊断术语，不是两个 Goal schema 字段。多个 connection
连接同一个 daemon 时共享同一 Goal runtime；多个独立 daemon 即使读取同一持久 Thread，
也可能拥有不同的 effective 调度认知。

## 5. 常用 App Server 事件与操作

| 名称 | 作用 |
|---|---|
| `thread/start` | 创建新 Thread |
| `thread/resume` | 加载或恢复已有 Thread |
| `thread/read` | 读取持久历史，不单独启动 Turn |
| `turn/start` | 显式创建普通 Turn |
| `turn/started` | Turn 已创建的执行证据；对应状态通常为 `inProgress` |
| `turn/completed` | Turn 进入 `completed`、`interrupted` 或 `failed` 终态 |
| `turn/steer` | 向当前执行中的 Turn 追加输入，不创建新 Turn |
| `turn/interrupt` | 中断精确的执行中 Turn |
| `thread/inject_items` | 向历史注入后续模型可见的 Item，不单独启动 Turn |
| `thread/goal/set(...)` | 当前 Goal extension 用来改变 Goal 状态的接口 |

## 6. Auto Research 对象不是 Codex 原生对象

| 术语 | 本文含义 |
|---|---|
| Supervisor | 监控 Experiment Run、交付终态并按规则激活 paused Goal 的外部控制器 |
| Worker | Supervisor 启动的训练或评估子进程 |
| Experiment Run | 一次受管实验命令及其持久产物 |
| run registry | Supervisor 用 run ID 跟踪待启动、运行中和待交付实验的事实记录 |
| repair Turn | native continuation 未建立时，Supervisor 创建的一次可审计故障上报 Turn |
| canonical state root | `research/supervisors/<thread-id>/`，该 Thread 的唯一 Auto Research 状态根目录 |
| cycle | 同一 Thread 上一次明确的 Goal 生命周期；新 Goal 会创建新 cycle |

Supervisor 状态只有 `OPEN / NEEDS_USER / COMPLETED`；Experiment Run 使用
`SUBMITTED / RUNNING / terminal event`。这些名称都不能用来推断 Turn 或 Goal 状态。

## 7. 读取状态时先说明对象

文档、日志和排障结论必须写成“对象 + 状态”，不要只写裸状态词。例如：

- 正确：`Turn=e3a... status=inProgress`；
- 正确：`Goal status=paused`；
- 正确：`Supervisor state=OPEN`；
- 正确：`Run=run-v30... status=RUNNING`；
- 不清楚：`当前是 active`、`已经 running`、`处于 blocked`。

判断整个研究流程时，应分别读取四层事实：

```text
Codex Turn → Codex Goal → Auto Research Supervisor → Experiment Run/Worker
```

任何一层的状态都不能替代另一层的实时证据。
