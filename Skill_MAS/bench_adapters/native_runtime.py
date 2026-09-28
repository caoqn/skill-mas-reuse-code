from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import subprocess
import time
import urllib.parse
import urllib.request
import io
import tarfile
from pathlib import Path
from typing import Any, Callable

from .base import NativeBenchmarkAdapter, NativeTask
from Skill_MAS.skill_mas.openai_async_client import AsyncOpenAIClient, normalize_usage_tokens


class NativeToolRuntime:
    """Isolated tool-call loop used by native benchmark adapters.

    The runtime implements the common file/shell tools itself. Benchmark
    adapters can add an ``execute_tool`` method later for MCP or Docker tools;
    unsupported tools are recorded as structured failures instead of silently
    falling back to text-only execution.
    """

    def __init__(
        self,
        adapter: NativeBenchmarkAdapter,
        task: NativeTask,
        context: dict[str, Any],
        model: str,
        checkpoint: Callable[[], None] | None = None,
    ):
        self.adapter = adapter
        self.task = task
        self.context = context
        self.workspace = Path(str(context["workspace"])).resolve()
        self.trace: list[dict[str, Any]] = []
        self.llm_usage: list[dict[str, Any]] = []
        self.checkpoint = checkpoint
        self.active_operation: str | None = None
        try:
            adapter.setup_runtime(task, context)
            self.client = AsyncOpenAIClient(model=model)
        except BaseException:
            adapter.teardown_runtime(task, context)
            raise

    def _checkpoint(self) -> None:
        if self.checkpoint is None:
            return
        try:
            self.checkpoint()
        except Exception:
            # Diagnostics must never change benchmark behavior.
            pass

    @staticmethod
    def _schema(name: str) -> dict[str, Any]:
        schemas = {
            "read_file": {"type": "function", "function": {"name": "read_file", "description": "Read a UTF-8 text file relative to the task workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
            "write_file": {"type": "function", "function": {"name": "write_file", "description": "Write UTF-8 text to a file relative to the task workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
            "bash": {"type": "function", "function": {"name": "bash", "description": "Run a shell command in the task workspace.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}}},
            "web_search": {"type": "function", "function": {"name": "web_search", "description": "Search the web for factual evidence.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
            "web_fetch": {"type": "function", "function": {"name": "web_fetch", "description": "Fetch a web page URL.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
            "loca_mcp": {"type": "function", "function": {"name": "loca_mcp", "description": "Call a LOCA stateful MCP tool. Prefer action='call' with tool_name and an arguments object; action may also be the catalog tool name. Use absolute workspace paths for spreadsheet/filesystem calls.", "parameters": {"type": "object", "properties": {"action": {"type": "string"}, "tool_name": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["action"]}}},
            "docker_bash": {"type": "function", "function": {"name": "docker_bash", "description": "Run a command in the BeyondSWE task container.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
            "docker_str_replace_editor": {"type": "function", "function": {"name": "docker_str_replace_editor", "description": "View or edit a file in the BeyondSWE task container.", "parameters": {"type": "object", "properties": {"command": {"type": "string", "enum": ["view", "create", "str_replace", "insert"]}, "path": {"type": "string"}, "file_text": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}, "insert_line": {"type": "integer"}, "view_range": {"type": "array", "items": {"type": "integer"}}}, "required": ["command", "path"]}}},
        }
        return schemas[name]

    def _safe_path(self, raw: str) -> Path:
        p = (self.workspace / str(raw)).resolve()
        if p != self.workspace and self.workspace not in p.parents:
            raise ValueError("path escapes task workspace")
        return p

    async def execute_tool(self, name: str, args: dict[str, Any]) -> str:
        started = time.monotonic()
        entry = {
            "tool": name,
            "arguments": args,
            "status": "started",
            "started_at_monotonic": started,
        }
        self.trace.append(entry)
        self.active_operation = f"tool:{name}"
        self._checkpoint()
        result = ""
        try:
            if name == "read_file":
                p = self._safe_path(args.get("path", ""))
                result = p.read_text(encoding="utf-8", errors="replace")[:50000] if p.is_file() else f"[error: file not found: {p}]"
            elif name == "write_file":
                p = self._safe_path(args.get("path", "")); p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(str(args.get("content", "")), encoding="utf-8")
                result = f"[wrote {len(str(args.get('content', '')))} chars to {p}]"
            elif name == "bash":
                timeout = min(max(int(args.get("timeout", 30) or 30), 1), 120)
                proc = await asyncio.create_subprocess_shell(str(args.get("command", "")), cwd=str(self.workspace), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
                try:
                    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                    result = (out + b"\n" + err).decode(errors="replace")[-30000:]
                except asyncio.TimeoutError:
                    os.killpg(proc.pid, signal.SIGKILL); await proc.wait(); result = f"[timeout after {timeout}s]"
            elif name == "web_search":
                query = urllib.parse.quote_plus(str(args.get("query", "")))
                req = urllib.request.Request(f"https://duckduckgo.com/html/?q={query}", headers={"User-Agent": "Skill-MAS/1.0"})
                with urllib.request.urlopen(req, timeout=20) as response:
                    result = response.read().decode(errors="replace")[:30000]
            elif name == "web_fetch":
                req = urllib.request.Request(str(args.get("url", "")), headers={"User-Agent": "Skill-MAS/1.0"})
                with urllib.request.urlopen(req, timeout=20) as response:
                    result = response.read().decode(errors="replace")[:30000]
            elif name in {"loca_mcp", "docker_bash", "docker_str_replace_editor"}:
                adapter_result = await self.adapter.execute_tool(name, args, self.context)
                result = adapter_result if adapter_result is not None else f"[native tool unavailable: {name}]"
            else:
                result = f"[native tool unavailable: {name}]"
        except asyncio.CancelledError:
            entry.update({"status": "cancelled", "result": "[tool cancelled]"})
            entry["elapsed_sec"] = round(time.monotonic() - started, 6)
            self.active_operation = f"tool:{name}:cancelled"
            self._checkpoint()
            raise
        except Exception as exc:
            result = f"[tool error {name}: {type(exc).__name__}: {exc}]"
            entry["status"] = "error"
        else:
            entry["status"] = "completed"
        entry["result"] = result[:30000]
        entry["elapsed_sec"] = round(time.monotonic() - started, 6)
        self.active_operation = None
        self._checkpoint()
        return result

    async def run(self, prompt: Any, *, max_rounds: int = 12) -> tuple[str, dict[str, Any]]:
        if isinstance(prompt, dict):
            prompt = str(prompt.get("prompt") or prompt.get("task_input") or "")
        prompt = str(prompt)
        tools = [self._schema(n) for n in self.adapter.tool_names(self.task) if n in {"read_file", "write_file", "bash", "web_search", "web_fetch", "loca_mcp", "docker_bash", "docker_str_replace_editor"}]
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        aggregate = {"prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0, "estimated_cost_usd": 0.0, "tool_calls": self.trace}
        for _ in range(max(1, int(max_rounds))):
            self.active_operation = "llm_generate_with_tools"
            self._checkpoint()
            try:
                text, calls, usage = await self.client.generate_with_tools(user_prompt="", tools=tools, messages=messages)
            except asyncio.CancelledError:
                self.active_operation = "llm_generate_with_tools:cancelled"
                self._checkpoint()
                raise
            else:
                self.active_operation = None
            self.llm_usage.append(dict(usage))
            self._checkpoint()
            prompt_tokens, output_tokens, total_tokens = normalize_usage_tokens(usage)
            for key, value in zip(("prompt_tokens", "output_tokens", "total_tokens"), (prompt_tokens, output_tokens, total_tokens)):
                aggregate[key] += value
            aggregate["estimated_cost_usd"] += float(usage.get("estimated_cost_usd", 0.0) or 0.0)
            if not calls:
                return text, aggregate
            assistant = {"role": "assistant", "content": text or None, "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]}
            messages.append(assistant)
            for call in calls:
                try:
                    args = json.loads(call.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await self.execute_tool(call["name"], args)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        return "[native tool loop exhausted]", aggregate

    async def close(self) -> None:
        try:
            self.adapter.teardown_runtime(self.task, self.context)
        finally:
            await self.client.aclose()
