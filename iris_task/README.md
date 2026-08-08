# Iris 自动实验任务

固定协议：sklearn 内置 Iris 数据集、`random_state=42`、分层 75/25 train/test split、accuracy 主指标。

运行：

```bash
python train.py
```

自动优化由项目根目录的 `auto-research` agent 启动，`goal.json` 和 baseline protocol 属于受保护内容。
