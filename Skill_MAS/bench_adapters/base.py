from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _configured_path(env_name: str, default: Path) -> Path:
    """Resolve an optional external benchmark path without embedding host paths."""
    raw = os.environ.get(env_name, "").strip()
    return Path(raw).expanduser().resolve() if raw else default.resolve()


# Native adapters may wrap external benchmark repositories. Configure these
# roots on the machine running the benchmark; no host-specific path is stored.
NEW_CODE_ROOT = _configured_path("SKILL_MAS_BENCH_ROOT", ROOT.parent)
DATA_ROOT = _configured_path("SKILL_MAS_DATA_ROOT", ROOT / "data")


@dataclass
class NativeTask:
    task_id: str
    prompt: str
    raw: dict[str, Any] = field(default_factory=dict)
    split: str = "test"


@dataclass
class NativeEvalResult:
    success: bool
    score: float
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class NativeBenchmarkAdapter:
    name = ""
    aliases: tuple[str, ...] = ()

    def default_source(self) -> Path:
        raise NotImplementedError

    def load_tasks(self, source: Path | None = None, *, split: str = "test", max_tasks: int = 0) -> list[NativeTask]:
        raise NotImplementedError

    def prepare_workspace(self, task: NativeTask, workspace: Path) -> dict[str, Any]:
        return {"workspace": str(workspace)}

    def setup_runtime(self, task: NativeTask, context: dict[str, Any]) -> None:
        """Attach benchmark services needed by the native tool runtime.

        Adapters that wrap an existing benchmark runner can override this hook
        to create a task-local MCP session or Docker container.  The default
        backend has no external service.
        """

    async def execute_tool(self, name: str, args: dict[str, Any], context: dict[str, Any]) -> str | None:
        """Execute an adapter-specific tool, or return ``None`` if unknown."""
        del name, args, context
        return None

    def teardown_runtime(self, task: NativeTask, context: dict[str, Any]) -> None:
        """Release benchmark services created by :meth:`setup_runtime`."""
        del task, context

    def build_prompt(self, task: NativeTask, context: dict[str, Any]) -> str:
        return task.prompt

    def tool_names(self, task: NativeTask) -> tuple[str, ...]:
        return ("read_file", "write_file", "bash")

    def evaluate(self, task: NativeTask, output: str, context: dict[str, Any]) -> NativeEvalResult:
        return NativeEvalResult(False, 0.0, "No evaluator registered for this backend.")

    def cleanup_workspace(self, context: dict[str, Any]) -> None:
        root = context.get("_temporary_root")
        if root:
            shutil.rmtree(str(root), ignore_errors=True)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
    return rows


def _norm(value: str) -> str:
    return "".join(ch.lower() for ch in str(value) if ch.isalnum())


_ADAPTERS: dict[str, NativeBenchmarkAdapter] = {}


def _register() -> None:
    if _ADAPTERS:
        return
    from .gaia import GaiaAdapter
    from .locobench import LocoBenchAdapter
    from .loca import LocaAdapter
    from .beyondswe import BeyondSWEAdapter

    adapters = (
        GaiaAdapter(),
        LocoBenchAdapter("feature_implementation"),
        LocoBenchAdapter("cross_file_refactoring"),
        LocaAdapter(),
        BeyondSWEAdapter("CrossRepo"),
        BeyondSWEAdapter("DepMigrate"),
    )
    for adapter in adapters:
        for key in (adapter.name, *adapter.aliases):
            _ADAPTERS[key.lower()] = adapter


def get_adapter(backend: str) -> NativeBenchmarkAdapter:
    _register()
    key = str(backend or "").strip().lower()
    if key not in _ADAPTERS:
        raise ValueError(f"Unknown native benchmark backend {backend!r}; available: {list_backends()}")
    return _ADAPTERS[key]


def list_backends() -> list[str]:
    _register()
    return sorted(_ADAPTERS)


def make_workspace(prefix: str, task: NativeTask) -> tuple[Path, dict[str, Any]]:
    root = Path(tempfile.mkdtemp(prefix=f"skill_mas_{prefix}_{_norm(task.task_id)}_"))
    context = {"workspace": str(root), "_temporary_root": str(root), "task_id": task.task_id}
    return root, context
