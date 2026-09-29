"""Opt-in SGLang launcher adding Torch scopes without modifying pinned sources.

The import hook also runs in Python spawn workers. Module hooks exist only during
an active profiler forward call and are always removed, including on exceptions.
CUDA Graph replay keeps its forward scope but does not invent inner module scopes.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import inspect
import json
import os
import runpy
import sys
from contextlib import ExitStack
from functools import wraps
from types import ModuleType
from typing import Any

from euboulia.profilers.semantics import PREFIX

TARGET = "sglang.srt.model_executor.model_runner"


_SOURCE_CACHE: dict[type[Any], dict[str, Any]] = {}


def module_source(cls: type[Any]) -> dict[str, Any]:
    if cls in _SOURCE_CACHE:
        return _SOURCE_CACHE[cls]
    try:
        result = {
            "file": inspect.getsourcefile(cls),
            "line": inspect.getsourcelines(cls)[1],
            "symbol": cls.__qualname__,
            "basis": "module class definition",
        }
    except (OSError, TypeError):
        result = {}
    if len(_SOURCE_CACHE) < 2048:
        _SOURCE_CACHE[cls] = result
    return result


def instrument_forward(method: Any, torch: Any) -> Any:
    @wraps(method)
    def forward(runner: Any, forward_batch: Any, *args: Any, **kwargs: Any) -> Any:
        if not torch.autograd._profiler_enabled():
            return method(runner, forward_batch, *args, **kwargs)
        sequence = getattr(runner, "_euboulia_profile_step", 0) + 1
        runner._euboulia_profile_step = sequence
        mode = getattr(getattr(forward_batch, "forward_mode", None), "name", "unknown")
        fields = {
            "phase": str(mode).lower(),
            "step": ("draft:" if getattr(runner, "is_draft_worker", False) else "target:")
            + str(sequence),
            "batch_size": getattr(forward_batch, "batch_size", None),
            "worker": "draft" if getattr(runner, "is_draft_worker", False) else "target",
        }
        # Hooks wrap Python module execution. Graph replay does not call them.
        with ExitStack() as lifetime:
            model = getattr(runner, "model", None)
            if model is not None:
                for name, module in model.named_modules():
                    if not name:
                        continue
                    contexts: list[Any] = []
                    source_info = module_source(type(module))

                    def enter(
                        mod: Any,
                        inputs: Any,
                        *,
                        path: str = name,
                        source: dict[str, Any] = source_info,
                        stack: list[Any] = contexts,
                    ) -> None:
                        context = torch.profiler.record_function(
                            PREFIX + json.dumps({"module": path, "source": source})
                        )
                        context.__enter__()
                        stack.append(context)

                    def leave(
                        mod: Any, inputs: Any, output: Any, *, stack: list[Any] = contexts
                    ) -> None:
                        if stack:
                            stack.pop().__exit__(None, None, None)

                    handle = module.register_forward_pre_hook(enter)
                    lifetime.callback(handle.remove)
                    handle = module.register_forward_hook(leave, always_call=True)
                    lifetime.callback(handle.remove)
            with torch.profiler.record_function(PREFIX + json.dumps(fields)):
                return method(runner, forward_batch, *args, **kwargs)

    return forward


class ScopeLoader(importlib.abc.Loader):
    def __init__(self, original: Any) -> None:
        self.original = original

    def create_module(self, spec: Any) -> ModuleType | None:
        return self.original.create_module(spec)  # type: ignore[no-any-return]

    def exec_module(self, module: ModuleType) -> None:
        self.original.exec_module(module)
        runner = module.ModelRunner
        runner.forward = instrument_forward(runner.forward, importlib.import_module("torch"))


class ScopeFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname != TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.loader is not None:
            spec.loader = ScopeLoader(spec.loader)
        return spec


def install() -> None:
    if os.environ.get("EUBOULIA_SEMANTIC_SCOPES") == "1" and not any(
        isinstance(finder, ScopeFinder) for finder in sys.meta_path
    ):
        sys.meta_path.insert(0, ScopeFinder())


install()
if __name__ == "__main__":
    runpy.run_module("sglang.launch_server", run_name="__main__")
