from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from .base import NEW_CODE_ROOT, NativeBenchmarkAdapter, NativeEvalResult, NativeTask


def _locobench_root() -> Path:
    import os
    raw = os.environ.get("SKILL_MAS_LOCOBENCH_ROOT", "").strip()
    return Path(raw).expanduser().resolve() if raw else NEW_CODE_ROOT / "benchmarks" / "LoCoBench"


class LocoBenchAdapter(NativeBenchmarkAdapter):
    def __init__(self, category: str = "feature_implementation"):
        if category not in {"feature_implementation", "cross_file_refactoring"}:
            raise ValueError(f"unsupported LoCoBench category: {category}")
        self.category = category
        self.name = "locobench_fi" if category == "feature_implementation" else "locobench_cr"
        self.aliases = ("locobench",) if category == "feature_implementation" else ()

    def default_source(self) -> Path:
        return _locobench_root() / "data" / "output" / "scenarios"

    def load_tasks(self, source: Path | None = None, *, split: str = "test", max_tasks: int = 0) -> list[NativeTask]:
        root = Path(source or self.default_source())
        tasks = []
        for path in sorted(root.glob("*.json")):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            tid = str(row.get("id") or path.stem)
            if not tid.startswith("python_") or f"_{self.category}_" not in tid:
                continue
            prompt = str(row.get("task") or row.get("prompt") or row.get("description") or "")
            tasks.append(NativeTask(tid, prompt, row, split))
        # Paper split: first 20 Python cases evolve the skill; the remaining
        # 80 are held out and must never enter the evolution chain.
        tasks = tasks[:20] if str(split).lower().startswith("train") else tasks[20:]
        return tasks[: int(max_tasks)] if max_tasks else tasks

    def prepare_workspace(self, task: NativeTask, workspace: Path) -> dict:
        project_id = str(task.raw.get("_project_id") or task.task_id)
        if not task.raw.get("_project_id"):
            # Scenario IDs encode the project before the category suffix.
            for marker in ("_architectural_understanding_", "_cross_file_refactoring_", "_feature_implementation_", "_bug_investigation_", "_multi_session_development_", "_code_comprehension_", "_integration_testing_", "_security_analysis_"):
                if marker in project_id:
                    project_id = project_id.split(marker, 1)[0]
                    break
        context_dir = workspace / "context"
        solution_dir = workspace / "solution"
        context_dir.mkdir(parents=True, exist_ok=True)
        solution_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for raw_path in task.raw.get("context_files", []) or []:
            rel = str(raw_path).replace("//", "/")
            src = self.default_source().parent.parent / "generated" / project_id / rel
            if not src.is_file():
                continue
            dst = context_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        return {"workspace": str(workspace), "project_id": project_id, "context_files_count": copied}

    def build_prompt(self, task: NativeTask, context: dict) -> str:
        return (
            f"## LoCoBench task\n{task.raw.get('task_prompt') or task.prompt}\n\n"
            f"Work inside `{context['workspace']}`. Inspect and modify the repository as needed, then run relevant tests. "
            "Your final response should summarize the implemented change; the evaluator reads the workspace state."
        )

    def evaluate(self, task: NativeTask, output: str, context: dict) -> NativeEvalResult:
        # Use the official metric implementation in a clean subprocess. This avoids
        # importing the Meta-Team `core` package into Skill-MAS's process.
        script = (
            "import json, pathlib; "
            "from benchmarks.adapter_locobench import _collect_solution_files_from_dir, _evaluate_with_metrics; "
            "p=pathlib.Path(" + repr(str(context["workspace"])) + "); "
            "s=_collect_solution_files_from_dir(p/'solution' if (p/'solution').exists() else p); "
            "print(json.dumps(_evaluate_with_metrics(json.loads(" + repr(json.dumps(task.raw)) + "), s)))"
        )
        try:
            env = {**__import__("os").environ, "PYTHONPATH": str(NEW_CODE_ROOT)}
            proc = subprocess.run([sys.executable, "-c", script], cwd=str(NEW_CODE_ROOT), env=env, text=True, capture_output=True, timeout=180)
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            lcbs = float(payload.get("lcbs") or 0.0)
            score = min(lcbs / 5.0, 1.0)
            return NativeEvalResult(lcbs >= 3.0, score, "LoCoBench LCBS evaluator", payload)
        except Exception as exc:
            return NativeEvalResult(False, 0.0, f"LoCoBench evaluator unavailable: {exc}")
