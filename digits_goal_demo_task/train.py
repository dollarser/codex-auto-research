"""Fixed-protocol sklearn digits optimization experiment."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from sklearn.datasets import load_digits
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.svm import SVC


SEED = 42
TEST_SIZE = 0.25


def main() -> None:
    dataset = load_digits()
    X_train, X_test, y_train, y_test = train_test_split(
        dataset.data,
        dataset.target,
        test_size=TEST_SIZE,
        random_state=SEED,
        stratify=dataset.target,
    )

    # Editable algorithm section. The candidate was selected using only
    # training-fold cross-validation; the held-out test labels are not used
    # until the single final evaluation below.
    model = SVC(C=10.0, kernel="rbf", gamma=0.0007)

    start = time.perf_counter()
    model.fit(X_train, y_train)
    predictions = model.predict(X_test)
    elapsed = time.perf_counter() - start
    metrics = {
        "status": "COMPLETED",
        "accuracy": float(accuracy_score(y_test, predictions)),
        "training_time_s": elapsed,
        "num_samples": int(len(dataset.data)),
        "num_features": int(dataset.data.shape[1]),
        "test_size": TEST_SIZE,
        "seed": SEED,
        "model": model.__class__.__name__,
        "model_params": {"C": 10.0, "kernel": "rbf", "gamma": 0.0007},
    }
    run_dir = Path(os.environ.get("AUTO_RESEARCH_RUN_DIR", "."))
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metrics), flush=True)


if __name__ == "__main__":
    main()
