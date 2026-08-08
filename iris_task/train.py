"""Fixed-protocol Iris experiment.

The dataset, split, seed, and evaluation protocol are fixed by this file.
Codex may modify the model/algorithm section, but must preserve the protocol.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from sklearn.datasets import load_iris
from sklearn.discriminant_analysis import QuadraticDiscriminantAnalysis
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


SEED = 42
TEST_SIZE = 0.25


def main() -> None:
    dataset = load_iris()
    X_train, X_test, y_train, y_test = train_test_split(
        dataset.data,
        dataset.target,
        test_size=TEST_SIZE,
        random_state=SEED,
        stratify=dataset.target,
    )

    # Algorithm under optimization. Keep the data and evaluation protocol above fixed.
    model = make_pipeline(
        StandardScaler(),
        QuadraticDiscriminantAnalysis(reg_param=0.01),
    )

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
    }
    run_dir = Path(os.environ.get("AUTO_RESEARCH_RUN_DIR", "."))
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics), flush=True)


if __name__ == "__main__":
    main()
