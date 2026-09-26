"""Detect where we're running and give every step the same ROOT-anchored layout."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # vishell/ (the package's parent, i.e. repo checkout)


def detect_env() -> str:
    if Path("/content").exists():
        return "colab"
    if Path("/kaggle/working").exists():
        return "kaggle"
    return "local"


def _default_root() -> Path:
    env = detect_env()
    if env == "colab":
        drive = Path("/content/drive/MyDrive")
        if drive.exists():
            return drive / "vishell"
        return Path("/content/vishell")
    if env == "kaggle":
        return Path("/kaggle/working/vishell")
    return PROJECT_ROOT


class Paths:
    """All artifacts live under ROOT/<kind>/<run_name>. run_name separates smoke from full runs."""

    def __init__(self, root: str | Path | None = None, run_name: str = "default"):
        self.root = Path(root) if root else _default_root()
        self.run_name = run_name

    def _dir(self, kind: str) -> Path:
        d = self.root / kind / self.run_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def data(self) -> Path:
        return self._dir("data")

    @property
    def models(self) -> Path:
        return self._dir("models")

    @property
    def checkpoints(self) -> Path:
        return self._dir("checkpoints")

    @property
    def results(self) -> Path:
        return self._dir("results")

    @property
    def logs(self) -> Path:
        return self._dir("logs")

    def done_marker(self, step: str) -> Path:
        return self._dir("state") / f"{step}.done"

    def is_done(self, step: str) -> bool:
        return self.done_marker(step).exists()

    def mark_done(self, step: str) -> None:
        self.done_marker(step).write_text("ok", encoding="utf-8")
