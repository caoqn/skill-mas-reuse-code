from __future__ import annotations

import json
import sys
import uuid
import asyncio
import os
from pathlib import Path

from .base import NEW_CODE_ROOT, NativeBenchmarkAdapter, NativeEvalResult, NativeTask, _load_jsonl


class BeyondSWEAdapter(NativeBenchmarkAdapter):
    def __init__(self, task_type: str = "DepMigrate"):
        if task_type not in {"CrossRepo", "DepMigrate"}:
            raise ValueError(f"unsupported BeyondSWE task type: {task_type}")
        self.task_type = task_type
        self.name = "beyondswe_crossrepo" if task_type == "CrossRepo" else "beyondswe_depmigrate"
        self.aliases = ("beyondswe_crossrepo", "crossrepo") if task_type == "CrossRepo" else ("beyondswe", "depmigrate")

    def default_source(self) -> Path:
        raw = os.environ.get("SKILL_MAS_BEYONDSWE_ROOT", "").strip()
        root = Path(raw).expanduser().resolve() if raw else NEW_CODE_ROOT / "benchmarks" / "BeyondSWE"
        return root / "beyondswe.jsonl"

    def load_tasks(self, source: Path | None = None, *, split: str = "test", max_tasks: int = 0) -> list[NativeTask]:
        path = Path(source or self.default_source())
        rows = _load_jsonl(path) if path.is_file() else []
        tasks = []
        for idx, row in enumerate(rows):
            if str(row.get("task")) != self.task_type:
                continue
            tid = str(row.get("instance_id") or row.get("id") or idx)
            prompt = str(row.get("problem_statement") or row.get("prompt") or row.get("instruction") or "")
            tasks.append(NativeTask(tid, prompt, row, split))
        tasks = tasks[:20] if str(split).lower().startswith("train") else tasks[20:]
        return tasks[: int(max_tasks)] if max_tasks else tasks

    def build_prompt(self, task: NativeTask, context: dict) -> str:
        return f"## BeyondSWE task\n{task.prompt}\n\nOperate in the provided repository workspace, edit files, run tests, and leave the final patch in the workspace."

    def tool_names(self, task: NativeTask) -> tuple[str, ...]:
        return ("read_file", "write_file", "docker_bash", "docker_str_replace_editor")

    @staticmethod
    def _legacy_module():
        from .base import NEW_CODE_ROOT
        root = str(NEW_CODE_ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)
        import benchmarks.adapter_beyondswe as legacy
        return legacy

    def setup_runtime(self, task: NativeTask, context: dict) -> None:
        legacy = self._legacy_module()
        item = dict(task.raw)
        instance_id = str(item.get("instance_id") or task.task_id)
        safe_name = instance_id.replace("/", "-").replace("__", "-").replace(".", "-").replace(":", "-")
        name = f"skill-mas-{uuid.uuid4().hex[:8]}-{safe_name}"[:63]
        container = legacy.create_beyondswe_container(name, item, str(context["workspace"]))
        workdir = str(item.get("workdir") or "/workspace")
        try:
            code, out = container.exec_run(["git", "config", "--global", "--add", "safe.directory", workdir], demux=True)
            if code != 0:
                err = out[1].decode(errors="replace") if out and out[1] else ""
                raise RuntimeError(f"git safe.directory setup failed: {err[-500:]}")
            legacy._run_pre_commands(container, item, workdir)
        except Exception:
            legacy.destroy_container(container)
            raise
        context.update({"beyondswe_container": container, "beyondswe_container_name": name, "beyondswe_workdir": workdir, "beyondswe_item": item, "beyondswe_legacy": legacy})

    async def execute_tool(self, name: str, args: dict, context: dict) -> str | None:
        container = context.get("beyondswe_container")
        if container is None:
            return "Error: no BeyondSWE Docker container is active"
        workdir = str(context.get("beyondswe_workdir") or "/workspace")
        legacy = context.get("beyondswe_legacy") or self._legacy_module()
        if name == "docker_bash":
            command = str(args.get("command") or "")
            if not command.strip():
                return "Error: empty command"
            from tools.docker_bash import _ENV_PREFIX, _is_command_blocked, _shell_quote
            if _is_command_blocked(command):
                return "Error: command blocked by security policy."
            timeout = min(max(int(args.get("timeout", 300) or 300), 1), 600)
            wrapped = f"{_ENV_PREFIX}bash -c {_shell_quote(command)}"
            def _run():
                return container.exec_run(["bash", "-c", wrapped], workdir=workdir, demux=True)
            try:
                code, output = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout)
            except asyncio.TimeoutError:
                return f"[timeout after {timeout}s]"
            stdout = output[0].decode(errors="replace") if output and output[0] else ""
            stderr = output[1].decode(errors="replace") if output and output[1] else ""
            result = stdout + (f"\n[stderr]\n{stderr}" if stderr else "")
            if code != 0:
                result += f"\n[exit code: {code}]"
            return result.strip() or "[no output]"
        if name == "docker_str_replace_editor":
            command = str(args.get("command") or "view")
            editor = __import__("tools.docker_str_replace_editor", fromlist=["_do_view", "_do_create", "_do_str_replace", "_do_insert"])
            path = str(args.get("path") or "")
            if not path:
                return "Error: path is required"
            if command == "view":
                return await asyncio.to_thread(editor._do_view, container, path, args.get("view_range"))
            if command == "create":
                return await asyncio.to_thread(editor._do_create, container, path, str(args.get("file_text") or ""))
            if command == "str_replace":
                return await asyncio.to_thread(editor._do_str_replace, container, path, str(args.get("old_str") or ""), str(args.get("new_str") or ""))
            if command == "insert":
                return await asyncio.to_thread(editor._do_insert, container, path, int(args.get("insert_line", 0) or 0), str(args.get("new_str") or ""))
            return f"Error: unknown editor command {command!r}"
        return None

    def teardown_runtime(self, task: NativeTask, context: dict) -> None:
        del task
        container = context.get("beyondswe_container")
        if container is not None:
            try:
                (context.get("beyondswe_legacy") or self._legacy_module()).destroy_container(container)
            finally:
                context["beyondswe_container"] = None

    def evaluate(self, task: NativeTask, output: str, context: dict) -> NativeEvalResult:
        del output
        container = context.get("beyondswe_container")
        if container is None:
            return NativeEvalResult(False, 0.0, "BeyondSWE Docker container is unavailable")
        legacy = context.get("beyondswe_legacy") or self._legacy_module()
        try:
            result = legacy.evaluate_patch_in_container(
                container, context.get("beyondswe_item") or task.raw,
                str(context.get("beyondswe_workdir") or "/workspace"), timeout=600,
            )
            f2p_total = int(result.get("f2p_total", 0) or 0)
            f2p_pass = int(result.get("f2p_pass", 0) or 0)
            p2p_total = int(result.get("p2p_total", 0) or 0)
            p2p_pass = int(result.get("p2p_pass", 0) or 0)
            f2p = f2p_pass / f2p_total if f2p_total else 0.0
            p2p = p2p_pass / p2p_total if p2p_total else 1.0
            score = f2p * 0.7 + p2p * 0.3
            return NativeEvalResult(bool(result.get("resolved")), 1.0 if result.get("resolved") else score, str(result.get("reason") or "")[:500], result)
        except Exception as exc:
            return NativeEvalResult(False, 0.0, f"BeyondSWE evaluator error: {exc}")
