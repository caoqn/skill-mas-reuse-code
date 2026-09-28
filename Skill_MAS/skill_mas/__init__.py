"""Unified Skill_MAS pipeline builders.

The client is loaded lazily so dataset discovery and static CLI commands do
not require the optional HTTP/OpenAI runtime dependencies.
"""

__all__ = ["AsyncOpenAIClient"]


def __getattr__(name: str):
    if name == "AsyncOpenAIClient":
        from .openai_async_client import AsyncOpenAIClient
        return AsyncOpenAIClient
    raise AttributeError(name)
