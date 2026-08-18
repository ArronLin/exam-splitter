from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    """Locate bundled resources in a PyInstaller build or files in development."""
    base = Path(getattr(sys, "_MEIPASS", ROOT))
    return base.joinpath(*parts)


def app_data_path(*parts: str) -> Path:
    """Write runtime files beside the desktop executable, never inside bundle resources."""
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT
    target = base.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def pipeline_command(pdf: Path, output_dir: Path, work_dir: Path, skip: int) -> tuple[str, list[str]]:
    common_args = ["--pdf", str(pdf), "--out", str(output_dir), "--workdir", str(work_dir), "--skip", str(skip)]
    if getattr(sys, "frozen", False):
        return sys.executable, ["--pipeline-worker", *common_args]
    return sys.executable, [str(resource_path("scripts", "run_pipeline.py")), *common_args]


def stage_from_log(line: str) -> tuple[str, int] | None:
    normalized = line.lower()
    mapping: Iterable[tuple[tuple[str, ...], tuple[str, int]]] = (
        (("stage 1", "layout"), ("版式识别", 20)),
        (("stage 2", "ocr"), ("文字识别", 45)),
        (("stage 3", "extract"), ("试卷抽取", 70)),
        (("stage 4", "split"), ("生成拆分结果", 90)),
        (("done", "completed", "完成"), ("处理完成", 100)),
    )
    for markers, state in mapping:
        if any(marker in normalized for marker in markers):
            return state
    return None


@dataclass(slots=True)
class LocalTask:
    source: Path
    skip: int = 2
    output_root: Path | None = None
    status: str = "等待处理"
    stage: str = "尚未开始"
    progress: int = 0
    output_dir: Path | None = None
    work_dir: Path | None = None
    logs: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def display_name(self) -> str:
        return self.source.name

    def prepare_directories(self, fallback_root: Path) -> None:
        root = self.output_root or fallback_root
        safe_name = self.source.stem.strip() or "未命名试卷"
        self.output_dir = root / safe_name
        self.work_dir = self.output_dir / "_work"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def append_log(self, text: str) -> None:
        cleaned = text.rstrip("\r\n")
        if cleaned:
            self.logs.append(cleaned)
            if len(self.logs) > 500:
                del self.logs[:-500]
