from __future__ import annotations

import re
import shutil
from pathlib import Path

from .base import DATA_ROOT, NativeBenchmarkAdapter, NativeEvalResult, NativeTask, _load_json


class GaiaAdapter(NativeBenchmarkAdapter):
    name = "gaia"
    aliases = ("gaia_test", "gaia_train")

    def default_source(self) -> Path:
        return DATA_ROOT / "gaia"

    def load_tasks(self, source: Path | None = None, *, split: str = "test", max_tasks: int = 0) -> list[NativeTask]:
        root = Path(source or self.default_source())
        suffix = "train_20" if split.startswith("train") else "test_100"
        rows: list[dict] = []
        for level in (1, 2, 3):
            path = root / f"level_{level}_{suffix}.json"
            if path.is_file():
                payload = _load_json(path)
                for row in payload if isinstance(payload, list) else []:
                    row = dict(row)
                    row["_level"] = level
                    rows.append(row)
        tasks = []
        for row in rows:
            task_id = str(row.get("task_id") or row.get("id") or len(tasks))
            question = str(row.get("Question") or row.get("question") or "")
            tasks.append(NativeTask(task_id, question, row, split))
        return tasks[: int(max_tasks)] if max_tasks else tasks

    def prepare_workspace(self, task: NativeTask, workspace: Path) -> dict:
        file_name = str(task.raw.get("file_name") or "")
        if file_name:
            root = self.default_source() / "val_files"
            candidates = [root / file_name, root / task.task_id / file_name]
            candidates.extend(p / file_name for p in root.glob(task.task_id) if p.is_dir())
            src = next((p for p in candidates if p.exists()), None)
            if src is not None:
                dst = workspace / file_name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        return {"workspace": str(workspace), "attachment": file_name, "task_id": task.task_id}

    def build_prompt(self, task: NativeTask, context: dict) -> str:
        file_name = context.get("attachment")
        prompt = f"## GAIA question\n{task.prompt}\n\n"
        if file_name:
            prompt += f"An attachment is available at `{context['workspace']}/{file_name}`. Inspect it when needed.\n\n"
        return prompt + "Return exactly one concise line beginning with `FINAL ANSWER:`."

    def tool_names(self, task: NativeTask) -> tuple[str, ...]:
        return ("read_file", "write_file", "bash", "web_search", "web_fetch")

    def evaluate(self, task: NativeTask, output: str, context: dict) -> NativeEvalResult:
        expected = str(task.raw.get("Final answer") or task.raw.get("final_answer") or "")
        actual = re.split(r"(?i)final\s*answer\s*:", output or "", maxsplit=1)[-1].strip()
        norm = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())
        ok = bool(expected) and norm(actual) == norm(expected)
        return NativeEvalResult(ok, 1.0 if ok else 0.0, "GAIA exact-match evaluator", {"expected": expected, "actual": actual})
