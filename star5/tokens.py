"""Real model token usage per turn, read from the provider's usage data on every LangChain call.

A context variable holds the meter of the running job; LangChain picks up the handler for every
LLM call made in that asyncio task and in the tasks it spawns. Embedding calls are not included.
"""

import threading
from contextlib import contextmanager
from contextvars import ContextVar

try:
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.tracers.context import register_configure_hook
except ImportError:  # pragma: no cover - the app always has LangChain
    BaseCallbackHandler = object
    register_configure_hook = None


class TokenMeter(BaseCallbackHandler):
    run_inline = True

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.input = 0
        self.output = 0
        self.calls = 0

    def add(self, input_tokens, output_tokens):
        with self.lock:
            self.input += int(input_tokens or 0)
            self.output += int(output_tokens or 0)
            self.calls += 1

    def on_llm_end(self, response, **kwargs):
        usage = None
        try:
            usage = getattr(response.generations[0][0].message, "usage_metadata", None)
        except (AttributeError, IndexError):
            pass
        if usage:
            self.add(usage.get("input_tokens"), usage.get("output_tokens"))
            return
        token_usage = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
        self.add(token_usage.get("prompt_tokens"), token_usage.get("completion_tokens"))

    def snapshot(self):
        with self.lock:
            return {"tokens_in": self.input, "tokens_out": self.output, "llm_calls": self.calls}


_current = ContextVar("star5_token_meter", default=None)
if register_configure_hook is not None:
    register_configure_hook(_current, inheritable=True)


@contextmanager
def metered():
    meter = TokenMeter()
    token = _current.set(meter)
    try:
        yield meter
    finally:
        _current.reset(token)
