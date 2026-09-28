"""Native benchmark rollout for GAIA/LoCoBench/LOCA/BeyondSWE.

This module keeps native benchmark state in disposable per-task workspaces and
uses Skill-MAS's existing three-stage build unchanged. It deliberately does
not import Meta-Team's Runner or any shared evolution registry.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from functools import partial
from dataclasses import asdict
from pathlib import Path
from typing import Any

from Skill_MAS.bench_adapters import get_adapter
from Skill_MAS.bench_adapters.base import make_workspace
from Skill_MAS.bench_adapters.native_runtime import NativeToolRuntime
from Skill_MAS.skill_mas.build import make_planner_call_fn, make_text_call_fn, run_mas_pipeline_with_retries
from Skill_MAS.skill_mas.openai_async_client import AsyncOpenAIClient, normalize_usage_tokens
from Skill_MAS.utils.llm_cost import empty_usage_totals, add_usage_totals, pricing_reference


class InfrastructureFailure(RuntimeError):
    """Fatal benchmark/runtime failure; abort the current native run."""


class APICircuitOpen(InfrastructureFailure):
    """Abort after a sustained upstream API outage."""


_NATIVE_TASK_TIMEOUT_S = {
    "gaia": 900.0,
    "locobench": 1800.0,
    "locabench": 7200.0,
    "beyondswe": 2400.0,
}
_API_RECOVERY_WINDOW_S = 800.0
# Kept as an alias for older callers and existing artifact terminology.
_TIMEOUT_WAIT_WINDOW_S = _API_RECOVERY_WINDOW_S

_API_ERROR_STREAK_LIMIT = 3
_API_ERROR_MARKERS = (
    "api error", "apiconnectionerror", "service unavailable", "rate limit",
    "ratelimit", "http 429", "http 500", "http 502", "http 503",
    "status code: 429", "status code: 500", "status code: 502", "status code: 503",
)


def _is_api_failure(trace: dict[str, Any]) -> bool:
    """Classify upstream/provider failures without treating task outcomes as API outages."""
    if trace.get("failure_stage") == "task_timeout":
        return False
    text = " ".join(str(trace.get(k) or "") for k in ("failure_reason", "failure_stage")).lower()
    return any(marker in text for marker in _API_ERROR_MARKERS)


def _task_timeout(bench_backend: str) -> float:
    key = str(bench_backend or "").strip().lower()
    for prefix, seconds in _NATIVE_TASK_TIMEOUT_S.items():
        if key.startswith(prefix):
            return seconds
    return 1800.0


def _task_wall_timeout(bench_backend: str) -> float:
    """Wall-clock deadline: effective task budget plus the API recovery window."""
    return _task_timeout(bench_backend) + _API_RECOVERY_WINDOW_S


def _task_budget_for_task(task: Any, bench_backend: str) -> float:
    """Resolve the effective budget for one task."""
    return _task_timeout(bench_backend)


def _elapsed_accounting(started_at: float, effective_budget: float) -> dict[str, Any]:
    """Separate effective task time from the bounded API recovery allowance."""
    elapsed_raw = max(0.0, time.monotonic() - started_at)
    excluded_raw = min(max(0.0, elapsed_raw - float(effective_budget)), _API_RECOVERY_WINDOW_S)
    effective_raw = max(0.0, elapsed_raw - excluded_raw)
    excluded = round(excluded_raw, 3)
    fields: dict[str, Any] = {
        "elapsed_seconds": round(elapsed_raw, 3),
        "effective_elapsed_seconds": round(effective_raw, 3),
        "effective_budget_seconds": float(effective_budget),
        "api_recovery_window_seconds": _API_RECOVERY_WINDOW_S,
        "api_recovery_excluded_seconds": excluded,
    }
    if excluded > 0:
        fields["elapsed_excluded_reason"] = "api_recovery_window"
    return fields


def _merge_usage(dst: dict[str, Any], src: dict[str, Any]) -> None:
    prompt, output, total = normalize_usage_tokens(src)
    add_usage_totals(dst, {**src, "prompt_tokens": prompt, "output_tokens": output, "total_tokens": total})


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Persist a checkpoint without leaving a half-written JSON document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _runtime_progress_trace(
    *,
    bench_backend: str,
    task: Any,
    started_at: float,
    effective_budget: float,
    holder: dict[str, Any],
    failure_stage: str,
    completion_status: str,
    failure_reason: str | None,
) -> dict[str, Any]:
    """Snapshot in-memory runtime state for partial and timeout checkpoints."""
    runtime = holder.get("runtime")
    client = holder.get("client")
    native_trace = list(getattr(runtime, "trace", []) or [])
    native_usage = [dict(x) for x in (getattr(runtime, "llm_usage", []) or [])]
    build_usage = [dict(x) for x in (getattr(client, "calls", []) or [])]
    usage = empty_usage_totals()
    for call_usage in [*build_usage, *native_usage]:
        _merge_usage(usage, call_usage)
    effective_fields = _elapsed_accounting(started_at, effective_budget)
    return {
        "schema": "skill_mas_native_trace_v1",
        "bench_backend": bench_backend,
        "task_id": task.task_id,
        "split": task.split,
        "prompt": holder.get("prompt", ""),
        "task_description": holder.get("prompt", ""),
        "success": False,
        "failure_stage": failure_stage,
        "completion_status": completion_status,
        "failure_reason": failure_reason,
        "evaluation": {"success": False, "score": 0.0, "summary": failure_stage, "details": {}},
        "final_output": "",
        "usage_totals": usage,
        "llm_usage": {"build_and_text": build_usage, "native_execution": native_usage},
        "native_tool_trace": native_trace,
        "partial_trace": True,
        "progress": {
            "phase": holder.get("phase", "unknown"),
            "active_operation": getattr(runtime, "active_operation", None) or getattr(client, "active_operation", None),
            "build_llm_calls": len(build_usage),
            "native_llm_calls": len(native_usage),
            "tool_events": len(native_trace),
        },
        **effective_fields,
    }


class _RecordedClient(AsyncOpenAIClient):
    """Keep build/repair usage, which the planner callback otherwise discards."""

    def __init__(self, checkpoint: Any | None = None, **kwargs):
        super().__init__(**kwargs)
        self.calls: list[dict[str, Any]] = []
        self.checkpoint = checkpoint
        self.active_operation: str | None = None

    async def generate(self, **kwargs):
        self.active_operation = "llm_generate"
        if self.checkpoint is not None:
            self.checkpoint()
        try:
            text, usage = await super().generate(**kwargs)
        except asyncio.CancelledError:
            self.active_operation = "llm_generate:cancelled"
            if self.checkpoint is not None:
                self.checkpoint()
            raise
        self.calls.append(dict(usage))
        self.active_operation = None
        if self.checkpoint is not None:
            self.checkpoint()
        return text, usage


def run_native_evaluation_round(
    *,
    bench_backend: str,
    bench_id: str,
    run_id: str,
    round_idx: int,
    task_ids: list[str],
    agent_llm: str,
    runs_dir: Path,
    skills_evolution_dir: Path,
    jsonl_path: Path | None = None,
    max_rounds: int = 12,
    trajectory_tag: str = "",
    split: str = "test",
    max_concurrency: int = 1,
    skill_path: Path | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    adapter = get_adapter(bench_backend)
    source = Path(jsonl_path).resolve() if jsonl_path is not None and str(jsonl_path) else None
    tasks = adapter.load_tasks(source, split=split)
    wanted = {str(t) for t in task_ids}
    selected = [task for task in tasks if str(task.task_id) in wanted]
    missing = sorted(wanted - {str(task.task_id) for task in selected})
    if missing:
        raise RuntimeError(f"{bench_backend} source missing task ids: {missing[:10]}")

    suffix = f"_{trajectory_tag}" if trajectory_tag else ""
    root = runs_dir / bench_id / run_id / f"round_{round_idx:02d}" / "bench_rollouts" / f"{bench_backend}_native_r{round_idx:02d}{suffix}"
    root.mkdir(parents=True, exist_ok=True)
    trace_dir = root / "process_traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    skill_path = (
        Path(skill_path).resolve()
        if skill_path is not None
        else skills_evolution_dir / bench_id / run_id / f"round_{round_idx:02d}" / "SKILL.md"
    )
    if not skill_path.is_file():
        raise FileNotFoundError(f"Skill workspace missing SKILL.md: {skill_path}")

    # Keep the complete task list separate from the pending list.  A resume run
    # executes only pending tasks, but its bundle must still contain the scores
    # from every durable checkpoint in this trajectory.
    all_selected = list(selected)

    # Resume safely: every durable evaluation trace is a checkpoint.  A timeout
    # is a completed attempt by policy and must not be rerun automatically.
    pending = []
    for task in all_selected:
        checkpoint = trace_dir / f"{task.task_id}.json"
        try:
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            if saved.get("failure_stage") == "task_timeout" or (
                saved.get("evaluation") is not None
                and saved.get("final_output") is not None
                and saved.get("success") is True
            ):
                continue
        except Exception:
            pass
        pending.append(task)
    selected = pending

    async def one(task, holder: dict[str, Any] | None = None):
        holder = holder if holder is not None else {}
        started_at = time.monotonic()
        effective_budget = _task_budget_for_task(task, bench_backend)
        holder.update({"started_at": started_at, "effective_budget": effective_budget, "phase": "setup"})
        workspace, context = make_workspace(bench_backend, task)
        context.update(adapter.prepare_workspace(task, workspace))
        client = _RecordedClient(model=agent_llm)
        holder["client"] = client
        planner = make_planner_call_fn(client, generation_kwargs={"response_format": {"type": "json_object"}, "temperature": 0.2})
        text_call = make_text_call_fn(client)
        runtime = None
        partial_path = trace_dir / f"{task.task_id}.partial.json"

        def checkpoint() -> None:
            snapshot = _runtime_progress_trace(
                bench_backend=bench_backend,
                task=task,
                started_at=started_at,
                effective_budget=effective_budget,
                holder=holder,
                failure_stage="in_progress",
                completion_status="in_progress",
                failure_reason=None,
            )
            _write_json_atomic(partial_path, snapshot)

        client.checkpoint = checkpoint

        try:
            try:
                runtime = NativeToolRuntime(adapter, task, context, agent_llm, checkpoint=checkpoint)
                holder["runtime"] = runtime
            except Exception as exc:
                raise InfrastructureFailure(
                    f"infrastructure failure during {bench_backend} setup for {task.task_id}: {exc}"
                ) from exc
            # Some stateful adapters create the authoritative task instruction
            # while attaching MCP/Docker services, so build the prompt after
            # runtime setup has completed.
            prompt = adapter.build_prompt(task, context)
            holder.update({"prompt": prompt, "phase": "mas_pipeline"})
            checkpoint()
            result = await run_mas_pipeline_with_retries(
                task_text=prompt,
                class_name="Native" + re.sub(r"\W", "_", f"{bench_backend}_{task.task_id}") + "Workflow",
                init_skill_path=skill_path,
                planner_call_fn=planner,
                text_call_fn=text_call,
                tool_call_fn=partial(runtime.run, max_rounds=max_rounds),
                dataset_name=bench_backend,
                max_generation_attempts=6,
                max_execution_attempts=4,
            )
            evaluation = adapter.evaluate(task, result.final_output, context)
            usage = empty_usage_totals()
            for call_usage in [*client.calls, *runtime.llm_usage]:
                _merge_usage(usage, call_usage)
            trace = {
                "schema": "skill_mas_native_trace_v1",
                "bench_backend": bench_backend,
                "task_id": task.task_id,
                "split": task.split,
                "prompt": prompt,
                "task_description": prompt,
                "build_stages": [asdict(s) for s in result.artifacts.stage_traces] if result.artifacts else [],
                "state": result.state,
                "mas_code": result.mas_code,
                "success": result.success,
                "failure_stage": result.failure_stage,
                "failure_reason": result.failure_reason,
                "generation_attempts_used": result.generation_attempts_used,
                "execution_attempts_used": result.execution_attempts_used,
                "retry_events": result.retry_events,
                "final_output": result.final_output,
                "evaluation": {"success": evaluation.success, "score": evaluation.score, "summary": evaluation.summary, "details": evaluation.details},
                "native_tool_trace": runtime.trace,
                "usage_totals": usage,
                "llm_usage": {"build_and_text": client.calls, "native_execution": runtime.llm_usage},
                "usage_scope": "All returned API usage, including build, repair, and execution; missing provider usage cannot be recovered.",
                **_elapsed_accounting(started_at, effective_budget),
            }
            (trace_dir / f"{task.task_id}.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
            partial_path.unlink(missing_ok=True)
            return task.task_id, float(evaluation.score), usage, trace
        finally:
            try:
                if runtime is not None:
                    await runtime.close()
            finally:
                try:
                    await client.aclose()
                finally:
                    adapter.cleanup_workspace(context)

    def write_timeout_trace(
        *,
        task: Any,
        started_at: float,
        effective_budget: float,
        holder: dict[str, Any],
        source_bench: str,
    ) -> tuple[Path, dict[str, Any]]:
        trace = _runtime_progress_trace(
            bench_backend=bench_backend,
            task=task,
            started_at=started_at,
            effective_budget=effective_budget,
            holder=holder,
            failure_stage="task_timeout",
            completion_status="completed_timeout",
            failure_reason=(
                f"task exceeded effective budget {effective_budget:.0f}s plus "
                f"API recovery window {_API_RECOVERY_WINDOW_S:.0f}s"
            ),
        )
        trace["wait_window_seconds"] = _API_RECOVERY_WINDOW_S
        trace["timeout_source_benchmark"] = str(source_bench)
        final_path = trace_dir / f"{task.task_id}.json"
        _write_json_atomic(final_path, trace)
        (trace_dir / f"{task.task_id}.partial.json").unlink(missing_ok=True)
        return final_path, trace

    async def gather_all():
        sem = asyncio.Semaphore(max(1, int(max_concurrency)))

        async def guarded(task):
            started_at = time.monotonic()
            effective_budget = _task_budget_for_task(task, bench_backend)
            holder: dict[str, Any] = {}
            try:
                async with sem:
                    return await asyncio.wait_for(one(task, holder), timeout=_task_wall_timeout(bench_backend))
            except asyncio.TimeoutError:
                _path, trace = write_timeout_trace(
                    task=task,
                    started_at=started_at,
                    effective_budget=effective_budget,
                    holder=holder,
                    source_bench=bench_backend,
                )
                return task.task_id, 0.0, empty_usage_totals(), trace
            except Exception as exc:
                elapsed_fields = _elapsed_accounting(started_at, effective_budget)
                reason = f"{type(exc).__name__}: {exc}"
                lowered = reason.lower()
                if any(marker in lowered for marker in _API_ERROR_MARKERS):
                    failure_stage = "api_failure"
                elif "infrastructure" in lowered or "docker" in lowered or "mcp" in lowered:
                    failure_stage = "infrastructure_failure"
                else:
                    failure_stage = "task_error"
                trace = {
                    "schema": "skill_mas_native_trace_v1",
                    "bench_backend": bench_backend,
                    "task_id": task.task_id,
                    "split": task.split,
                    "success": False,
                    "failure_stage": failure_stage,
                    "completion_status": "failed",
                    "failure_reason": reason,
                    "evaluation": {"success": False, "score": 0.0, "summary": failure_stage, "details": {}},
                    "final_output": "",
                    "usage_totals": empty_usage_totals(),
                    "native_tool_trace": [],
                    **elapsed_fields,
                }
                (trace_dir / f"{task.task_id}.json").write_text(
                    json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                return task.task_id, 0.0, empty_usage_totals(), trace

        return await asyncio.gather(*(guarded(task) for task in selected))

    rows = asyncio.run(gather_all())
    aggregate = empty_usage_totals()
    # ``rows`` contains only newly executed tasks.  Reconstruct the bundle from
    # disk for all tasks so resumed checkpoints cannot disappear from scoring.
    fresh_rows = {str(tid): (float(score), usage, trace) for tid, score, usage, trace in rows}
    scores: dict[str, float] = {}
    per_task: list[dict[str, Any]] = []
    missing_checkpoints: list[str] = []
    for task in all_selected:
        tid = str(task.task_id)
        trace_path = trace_dir / f"{tid}.json"
        trace: dict[str, Any] | None = None
        if trace_path.is_file():
            try:
                loaded = json.loads(trace_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    trace = loaded
            except Exception:
                trace = None
        if trace is None and tid in fresh_rows:
            trace = fresh_rows[tid][2]
        evaluation = trace.get("evaluation") if isinstance(trace, dict) else None
        if not isinstance(evaluation, dict) or "score" not in evaluation:
            missing_checkpoints.append(tid)
            continue
        score = float(evaluation.get("score", 0.0) or 0.0)
        usage = trace.get("usage_totals") if isinstance(trace, dict) else None
        usage = usage if isinstance(usage, dict) else empty_usage_totals()
        scores[tid] = score
        _merge_usage(aggregate, usage)
        per_task.append({"task_id": tid, "score": score, **usage})
    if missing_checkpoints:
        raise RuntimeError(
            "Native rollout cannot build a complete score bundle; missing or invalid "
            f"checkpoints for task_ids={missing_checkpoints[:10]}"
        )
    bundle = {
        "bench_backend": bench_backend,
        "round_idx": round_idx,
        "bench_id": bench_id,
        "run_id": run_id,
        "task_ids": [str(t.task_id) for t in all_selected],
        "process_trace_dir": str(trace_dir.resolve()),
        "merged_skill_dir": str(skill_path.parent.resolve()),
        "per_task_scores": scores,
    }
    bundle_path = root / f"{bench_backend}_native_bundle_r{round_idx:02d}.json"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    section = {"phase": "native_skill_mas_rollout", "description": f"Skill-MAS native {bench_backend} rollout", "model": agent_llm, "aggregate_usage": aggregate, "per_task": per_task, "pricing_reference": pricing_reference()}
    return bundle_path, root, section


def _collect_state_usage(state: dict[str, Any]) -> dict[str, Any]:
    total = empty_usage_totals()
    seen: set[int] = set()
    for key, value in (state or {}).items():
        if not str(key).startswith("usage_") or not isinstance(value, dict):
            continue
        marker = id(value)
        if marker in seen:
            continue
        seen.add(marker)
        add_usage_totals(total, value)
    return total
