from __future__ import annotations

import json
import sys
import asyncio
import os
from pathlib import Path

from .base import NEW_CODE_ROOT, NativeBenchmarkAdapter, NativeEvalResult, NativeTask


class LocaAdapter(NativeBenchmarkAdapter):
    name = "loca"
    aliases = ("loca_bench", "loca-bench")
    _LOCAL_STATE_PRIMARY_TASKS = frozenset({
        "WoocommerceStockAlertS2LEnv",
        "WoocommerceNewWelcomeS2LEnv",
    })
    _PAPER_TASKS = frozenset({
        "WoocommerceNewWelcomeS2LEnv", "WoocommerceStockAlertS2LEnv",
        "FilterLowSellingProductsS2LEnv", "ApplyPhDEmailS2LEnv",
        "SetConfCrDdlS2LEnv", "CourseAssistantS2LEnv",
        "CanvasArrangeExamS2LEnv", "CanvasListTestS2LEnv",
    })
    _TEST_CONTEXT_LEVELS = ("8k", "16k", "32k", "64k", "128k", "256k")

    def default_source(self) -> Path:
        raw = os.environ.get("SKILL_MAS_LOCA_ROOT", "").strip()
        root = Path(raw).expanduser().resolve() if raw else NEW_CODE_ROOT / "benchmarks" / "LOCA-bench"
        return root / "task-configs"

    def load_tasks(self, source: Path | None = None, *, split: str = "test", max_tasks: int = 0) -> list[NativeTask]:
        root = Path(source or self.default_source())
        tasks: list[NativeTask] = []
        split_name = str(split or "test").lower()
        if split_name.startswith("train"):
            paths = [root / "evolve_96k_set_config.json"]
        else:
            paths = [root / f"final_{level}_set_config.json" for level in self._TEST_CONTEXT_LEVELS]
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows = payload if isinstance(payload, list) else payload.get("configurations", payload.get("tasks", payload.get("data", []))) if isinstance(payload, dict) else []
            for idx, row in enumerate(rows if isinstance(rows, list) else []):
                row = dict(row) if isinstance(row, dict) else {"task": str(row)}
                row.setdefault("_context_level", path.stem.removeprefix("final_").removesuffix("_set_config"))
                row.setdefault("_split", split)
                row.setdefault("_seed", (row.get("env_params") or {}).get("seed", 0))
                task_name = str(row.get("name") or "")
                if task_name not in self._PAPER_TASKS:
                    continue
                if split_name.startswith("train") and int(row.get("_seed") or 0) not in {101, 102}:
                    continue
                tid = str(row.get("task_id") or row.get("id") or f"{path.stem}:{idx}")
                prompt = str(row.get("task") or row.get("prompt") or row.get("instruction") or row.get("name") or row)
                tasks.append(NativeTask(tid, prompt, row, split))
        return tasks[: int(max_tasks)] if max_tasks else tasks

    def build_prompt(self, task: NativeTask, context: dict) -> str:
        instruction = str(context.get("task_instruction") or task.prompt)
        catalog = context.get("loca_catalog") or []
        catalog_text = "\n".join(
            f"- {item.get('name')}: {str(item.get('description') or '')[:160]}"
            for item in catalog[:200] if isinstance(item, dict)
        )
        suffix = "\n\nAvailable native MCP tools:\n" + catalog_text if catalog_text else ""
        workspace = str(context.get("workspace") or "")
        return (
            f"## LOCA-Bench task\n{instruction}\n"
            f"Task workspace: `{workspace}`. Use absolute paths under this workspace for Excel/filesystem MCP calls.\n"
            f"{suffix}\nUse `loca_mcp` with `action=call`, `tool_name=<catalog name>`, and an object `arguments`; "
            "the bridge also accepts the tool name directly as action. Finish all required mutations and verify "
            "final state before answering."
        )

    def tool_names(self, task: NativeTask) -> tuple[str, ...]:
        return ("read_file", "write_file", "bash", "loca_mcp")

    @staticmethod
    def _legacy_module():
        """Load the project's already-tested LOCA adapter without vendoring it."""
        from .base import NEW_CODE_ROOT
        root = str(NEW_CODE_ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)
        import benchmarks.adapter_locabench_local_state_primary_v7 as legacy
        return legacy

    def setup_runtime(self, task: NativeTask, context: dict) -> None:
        legacy = self._legacy_module()
        raw = dict(task.raw)
        raw.setdefault("_task_name", str(raw.get("name") or ""))
        raw.setdefault("_context_level", str(raw.get("_context_level") or "8k"))
        raw.setdefault("_split", task.split)
        raw.setdefault("_seed", (raw.get("env_params") or {}).get("seed", 0))
        task_dir = str(context["workspace"])
        env = legacy._create_loca_env(raw, task_dir)
        context["loca_env"] = env
        instruction = env._get_instructions() if hasattr(env, "_get_instructions") else task.prompt
        context["loca_item"] = raw
        context["task_instruction"] = instruction
        context["loca_mcp"] = None
        context["loca_catalog"] = []
        task_name = str(raw.get("_task_name") or "")
        if task_name not in self._LOCAL_STATE_PRIMARY_TASKS:
            mcp_tool, catalog = legacy._setup_mcp_tool(raw, task_dir)
            context["loca_mcp"] = mcp_tool
            context["loca_catalog"] = catalog
        context["loca_legacy"] = legacy

    async def execute_tool(self, name: str, args: dict, context: dict) -> str | None:
        if name != "loca_mcp":
            return None
        tool = context.get("loca_mcp")
        catalog = context.get("loca_catalog") or []
        action = str(args.get("action") or "list_tools")
        # Models often use the native tool name as the action after reading the
        # catalog. Treat that shorthand as a normal MCP call so the bridge is
        # tolerant of both the explicit {action: call, tool_name: ...} contract
        # and the generated {action: tool_name, arguments: ...} form.
        if args.get("tool_name") and action not in {"list_tools", "call"}:
            action = "call"
        if action == "list_tools":
            return json.dumps({"tools": catalog}, ensure_ascii=False)
        if action != "call":
            return f"Error: unknown LOCA MCP action {action!r}"
        if tool is None:
            return "Error: this LOCA task uses local_state_primary; use the local workspace/database tools instead of MCP."
        tool_name = str(args.get("tool_name") or "")
        params = args.get("arguments", {})
        if isinstance(params, str):
            try:
                params = json.loads(params or "{}")
            except json.JSONDecodeError as exc:
                return f"Error: invalid JSON arguments: {exc}"
        if not isinstance(params, dict):
            return "Error: arguments must be an object"
        params = dict(params)
        for key in ("filepath", "file_path"):
            value = params.get(key)
            if isinstance(value, str) and value and not value.startswith("/"):
                # LOCA's spreadsheet MCP requires absolute paths outside SSE
                # mode. Resolve common task-relative forms for model callers.
                candidate = Path(str(context.get("workspace") or "")) / value
                if not candidate.exists():
                    candidate = Path(str(context.get("workspace") or "")) / "agent_workspace" / value
                params[key] = str(candidate)
        result = await asyncio.to_thread(tool.execute_tool, tool_name, params, "skill_mas_call")
        if isinstance(result, tuple) and len(result) >= 3:
            valid, has_error, observation = result[:3]
            if not valid:
                return f"Error: MCP tool {tool_name!r} not found"
            return str(observation)
        return str(result)

    def teardown_runtime(self, task: NativeTask, context: dict) -> None:
        del task
        tool = context.get("loca_mcp")
        if tool is not None:
            try:
                tool.close()
            finally:
                context["loca_mcp"] = None
        env = context.get("loca_env")
        for method in ("close", "cleanup", "teardown"):
            fn = getattr(env, method, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
                break

    def evaluate(self, task: NativeTask, output: str, context: dict) -> NativeEvalResult:
        del task, output
        env = context.get("loca_env")
        if env is None:
            return NativeEvalResult(False, 0.0, "LOCA environment was not initialized")
        try:
            with self._legacy_module()._eval_lock:
                _obs, reward, _terminated, _truncated, info = env.step("claim_done")
            score = float(reward)
            details = info.get("evaluation", "") if isinstance(info, dict) else ""
            return NativeEvalResult(
                score >= 1.0,
                score,
                f"reward={score:.1f} ({'PASS' if score >= 1.0 else 'FAIL'})",
                {"evaluation": details},
            )
        except Exception as exc:
            return NativeEvalResult(False, 0.0, f"LOCA evaluation error: {exc}")
