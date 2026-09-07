"""Exercise scope lifecycle with a fake Torch surface; no GPU dependency."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from euboulia.profilers.sglang_launcher import instrument_forward


class Module:
    def __init__(self):
        self.pre = []
        self.post = []

    def register_forward_pre_hook(self, fn):
        self.pre.append(fn)
        return SimpleNamespace(remove=lambda: self.pre.remove(fn))

    def register_forward_hook(self, fn, *, always_call):
        assert always_call
        self.post.append(fn)
        return SimpleNamespace(remove=lambda: self.post.remove(fn))

    def __call__(self, fail=False):
        for hook in self.pre:
            hook(self, ())
        try:
            if fail:
                raise RuntimeError("kernel failed")
        finally:
            for hook in self.post:
                hook(self, (), None)


@pytest.mark.parametrize("fail", [False, True])
def test_transient_module_scopes_and_exception_cleanup(fail):
    active = False
    recorded, stack = [], []

    @contextmanager
    def record(name):
        recorded.append(name)
        stack.append(name)
        try:
            yield
        finally:
            assert stack.pop() == name

    torch = SimpleNamespace(
        autograd=SimpleNamespace(_profiler_enabled=lambda: active),
        profiler=SimpleNamespace(record_function=record),
    )
    layer = Module()
    runner = SimpleNamespace(model=SimpleNamespace(named_modules=lambda: [("layers.0.moe", layer)]))
    batch = SimpleNamespace(forward_mode=SimpleNamespace(name="MIXED"), batch_size=2)

    def original(self, forward_batch):
        layer(fail)
        return "result"

    wrapped = instrument_forward(original, torch)
    if not fail:
        assert wrapped(runner, batch) == "result"
        assert recorded == [] and layer.pre == []
    active = True
    if fail:
        with pytest.raises(RuntimeError, match="kernel failed"):
            wrapped(runner, batch)
    else:
        assert wrapped(runner, batch) == "result"
    assert '"phase": "mixed"' in recorded[0]
    assert '"module": "layers.0.moe"' in recorded[1]
    assert layer.pre == layer.post == stack == []


def test_launcher_import_hook_survives_spawn(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    package = tmp_path / "sglang"
    runner_dir = package / "srt" / "model_executor"
    runner_dir.mkdir(parents=True)
    for folder in (package, package / "srt", runner_dir):
        (folder / "__init__.py").write_text("")
    (runner_dir / "model_runner.py").write_text(
        "class ModelRunner:\n    def forward(self, forward_batch): return 1\n"
    )
    (tmp_path / "torch.py").write_text("")
    (package / "worker.py").write_text(
        "def check():\n    from sglang.srt.model_executor.model_runner import ModelRunner\n"
        "    assert hasattr(ModelRunner.forward, '__wrapped__')\n"
        "    print('worker scopes installed')\n"
    )
    (package / "launch_server.py").write_text(
        "from multiprocessing import get_context\nfrom sglang.worker import check\n"
        "if __name__ == '__main__':\n    check()\n"
        "    p=get_context('spawn').Process(target=check)\n"
        "    p.start()\n    p.join(10)\n    assert p.exitcode == 0\n"
    )
    env = {
        **os.environ,
        "EUBOULIA_SEMANTIC_SCOPES": "1",
        "PYTHONPATH": os.pathsep.join([str(tmp_path), str(Path(__file__).parents[1] / "src")]),
    }
    result = subprocess.run(
        [sys.executable, "-m", "euboulia.profilers.sglang_launcher"],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("worker scopes installed") == 2
