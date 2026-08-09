# Digits Goal + Listener 完整验证任务

本任务用于同时验证算法优化目标和 Auto Research v0.3 的异步 Goal 闭环。

## 目标

- 数据：`sklearn.datasets.load_digits()`。
- 固定协议：`train_test_split(test_size=0.25, random_state=42, stratify=y)`。
- 基线 accuracy：`0.9777777777777777`，即 450 个测试样本中错误 10 个。
- 硬门槛：accuracy `>= 0.988`，即最多错误 5 个。
- 最多 3 次正式实验；不得使用测试集选择模型或超参数。

完整目标和协议见 `goal.json` 与 `research/goal_contract.json`。

## Idea 选择

只在 `X_train/y_train` 上做五折分层交叉验证。首选候选为：

```python
SVC(C=10.0, kernel="rbf", gamma=0.0007)
```

训练集 CV accuracy 为 `0.992573 ± 0.005251`，高于本轮比较的 raw RBF `gamma=0.001`、scaled RBF 和 distance-weighted KNN 候选。

## 结果

两次 detached 正式 run 都得到：

- accuracy：`0.9911111111111112`，450 个测试样本中错误 4 个。
- 相对基线少 6 个错误，错误数下降 60%。
- 第二次训练与预测计时：`0.023750167340040207s`。

第二次 run 同时完整验证：

```text
PAUSE_HANDOFF
  -> turn finalization 覆盖状态被检测到
  -> Goal 再次确认 paused
  -> WAITING
  -> RUN_COMPLETED
  -> Goal status = active
  -> ACTIVATED
  -> 原生 Goal continuation
```

Listener 没有调用 `thread/resume` 或 `turn/start`，也没有在实验期间调用模型查询状态。完整结构化结论见 `research/final_result.json`。

## 复现

从仓库根目录运行：

```bash
AUTO_RESEARCH_RUN_DIR=/tmp/digits-goal-demo-result \
  uv run python digits_goal_demo_task/train.py
uv run python -m unittest discover -s tests -v
```

正式实验 run 证据保存在 `research/runs/`。run 命令中的 `sleep 20` 仅用于给异步暂停/激活验证留出观察窗口，不计入 `training_time_s`，也不参与模型指标。
