"""Reasoning effort chosen for a turn, applied to every model call made by its job.

A context variable holds the turn's effort; asyncio tasks spawned by the job inherit it. The hook
wraps GPT Researcher's GenericLLMProvider.from_provider, through which every chat model is built
(research, brief, report chat), and adds "reasoning_effort" for OpenAI-compatible providers. The
foundry-proxy turns it into Claude's output_config.effort or OpenAI's reasoning.effort.
"""

from contextlib import contextmanager
from contextvars import ContextVar

EFFORTS = ("low", "medium", "high", "xhigh", "max")
OPENAI_PROVIDERS = ("openai", "azure_openai")

_current = ContextVar("star5_reasoning_effort", default="")
_installed = False


def current():
    return _current.get()


@contextmanager
def reasoning(effort):
    """Empty effort = the model's default (medium on Claude Opus 5.5)."""
    token = _current.set(effort if effort in EFFORTS else "")
    try:
        yield
    finally:
        _current.reset(token)


def apply(provider, kwargs):
    effort = _current.get()
    if effort and provider in OPENAI_PROVIDERS:
        kwargs["reasoning_effort"] = effort
    return kwargs


def install():
    global _installed
    if _installed:
        return
    try:
        from gpt_researcher.llm_provider.generic.base import GenericLLMProvider
    except ImportError:  # pragma: no cover - the app always has GPT Researcher
        return
    original = GenericLLMProvider.from_provider.__func__

    def from_provider(cls, provider, chat_log=None, verbose=True, **kwargs):
        return original(cls, provider, chat_log=chat_log, verbose=verbose, **apply(provider, kwargs))

    GenericLLMProvider.from_provider = classmethod(from_provider)
    _installed = True
