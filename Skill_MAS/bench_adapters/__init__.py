"""Benchmark adapters for running Skill-MAS on the user's native benchmarks.

The adapters intentionally live outside the Meta-Team runtime.  They only
provide task loading, task prompts, workspace provisioning, and scoring; no
chairman, template registry, handoff registry, or evolution state is loaded.
"""

from .base import NativeTask, NativeEvalResult, get_adapter, list_backends

__all__ = ["NativeTask", "NativeEvalResult", "get_adapter", "list_backends"]
