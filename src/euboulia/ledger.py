"""Append-only JSONL storage for experiment snapshots."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from .models import Experiment

try:  # pragma: no cover - exercised on Unix; fallback keeps Windows usable
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class LedgerCorruptionError(ValueError):
    """Raised when an existing ledger line cannot be decoded."""

    def __init__(self, path: Path, line_number: int, message: str) -> None:
        self.path = path
        self.line_number = line_number
        super().__init__(f"{path}:{line_number}: {message}")


class ExperimentLedger:
    """A durable, append-only sequence of :class:`Experiment` snapshots.

    Reusing an ``experiment_id`` is allowed: appending another snapshot records
    a lifecycle transition without rewriting history. ``get`` returns the most
    recent snapshot and ``history`` returns every matching snapshot.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        create_parents: bool = True,
        fsync: bool = False,
    ) -> None:
        self.path = Path(path)
        self.fsync = fsync
        if create_parents:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, experiment: Experiment) -> Experiment:
        """Append one complete JSON object and return the supplied snapshot."""

        if not isinstance(experiment, Experiment):
            raise TypeError("experiment must be an Experiment")
        line = experiment.to_json() + "\n"
        # flock protects readers/writers sharing this convention. O_APPEND plus
        # one write prevents an in-process seek from overwriting prior records.
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            payload = line.encode("utf-8")
            written = 0
            while written < len(payload):
                count = os.write(fd, payload[written:])
                if count <= 0:  # defensive: os.write normally raises instead
                    raise OSError("short write while appending experiment ledger")
                written += count
            if self.fsync:
                os.fsync(fd)
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return experiment

    def iter_experiments(self) -> Iterator[Experiment]:
        """Yield snapshots in append order, validating every non-empty line."""

        if not self.path.exists():
            return
        if not self.path.is_file():
            raise IsADirectoryError(self.path)
        with self.path.open("r", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        yield Experiment.from_json(line)
                    except (TypeError, ValueError, KeyError) as exc:
                        raise LedgerCorruptionError(self.path, line_number, str(exc)) from exc
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read_all(self) -> list[Experiment]:
        return list(self.iter_experiments())

    def history(self, experiment_id: str) -> list[Experiment]:
        return [
            experiment
            for experiment in self.iter_experiments()
            if experiment.experiment_id == experiment_id
        ]

    def get(self, experiment_id: str) -> Experiment | None:
        latest: Experiment | None = None
        for experiment in self.iter_experiments():
            if experiment.experiment_id == experiment_id:
                latest = experiment
        return latest

    def latest(self) -> Experiment | None:
        latest: Experiment | None = None
        for experiment in self.iter_experiments():
            latest = experiment
        return latest

    def __iter__(self) -> Iterator[Experiment]:
        return self.iter_experiments()

    def __len__(self) -> int:
        return sum(1 for _ in self.iter_experiments())


# Short alias for callers that already operate in an experiment-only context.
Ledger = ExperimentLedger


__all__ = ["ExperimentLedger", "Ledger", "LedgerCorruptionError"]
