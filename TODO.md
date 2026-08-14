# TODO

- [x] 使用 `CODEX_THREAD_ID` 作为 Supervisor 的唯一状态目录键，取消 Goal 内对任意
  `--state-root` 的依赖。

  目标结构：

  ```text
  research/supervisors/<thread-id>/
  ├── metadata.json
  ├── supervisor_session.json
  ├── supervisor/
  ├── cycles/<cycle-id>.json
  └── runs/
  ```

  设计约束与验收条件：

  - 强制“一 Thread 一个 Supervisor 根目录”；不得把同一 Thread 绑定到多个目录。
  - 首次启动先创建 App Server Thread，取得 ID 后立即原子写入对应目录和 session
    binding；中途失败不得留下无法追踪的 active Goal。
  - 同一 Thread 的后续研究使用新的 Goal cycle，复用根目录与历史 runs，不为每轮 Goal
    创建新的 Supervisor 根目录。
  - Goal Turn 从 `CODEX_THREAD_ID` 直接定位状态，因此常规暂停命令收敛为
    `auto-research goal set-status paused`；显式 `--thread-id` 只用于 task 外运维。
  - 人类可读名称写入 `metadata.json`；可生成 `index.json` 方便浏览，但索引不得参与
    控制流或恢复判断。
  - `submit`、`status`、Goal 状态控制、终态注入和重启恢复统一使用相同的 Thread-root
    解析函数，不允许各自保留 state-root fallback。
  - 新建、adopt、restart、同 Thread 多 Goal cycle、重复启动、损坏绑定和并发启动均需
    回归测试；必须证明不会创建第二个 Supervisor 或错误唤醒其他 Thread。
  - 当前实现只支持该结构，不保留旧目录迁移或 fallback。
