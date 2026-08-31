"""Evidence-backed rule planner and deterministic patch catalog.

The planner is the "architect": it selects and explains a declared change. It
does not edit a repository. The patch workspace is the independent "editor"
that treats every selected patch as untrusted input and validates it again.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from euboulia.optimization.contracts import (
    AnalysisReport,
    ChangeKind,
    ChangeProposal,
    MemoryEntry,
    StageContext,
)


class PatchCatalogError(ValueError):
    """Raised when a catalog cannot be interpreted deterministically."""


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PatchCatalogError(f"{path} must be a non-empty string")
    return value.strip()


def _string_tuple(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise PatchCatalogError(f"{path} must be a non-empty list of strings")
    return tuple(_text(item, f"{path}[{index}]") for index, item in enumerate(value))


@dataclass(frozen=True, slots=True)
class PatchCatalogEntry:
    entry_id: str
    title: str
    rationale: str
    triggers: tuple[str, ...]
    patch_path: Path
    patch_sha256: str
    predicted_metric: str | None = None
    risk: str = "medium"

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        index: int,
        catalog_path: Path,
    ) -> PatchCatalogEntry:
        path = f"entries[{index}]"
        allowed = {
            "id",
            "title",
            "rationale",
            "triggers",
            "patch",
            "predicted_metric",
            "risk",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise PatchCatalogError(f"{path} contains unknown field(s): {', '.join(unknown)}")
        patch_value = Path(_text(value.get("patch"), f"{path}.patch")).expanduser()
        patch_path = (
            patch_value
            if patch_value.is_absolute()
            else (catalog_path.parent / patch_value).resolve()
        )
        if not patch_path.is_file():
            raise PatchCatalogError(f"{path}.patch is not a file: {patch_path}")
        patch_bytes = patch_path.read_bytes()
        predicted = value.get("predicted_metric")
        if predicted is not None:
            predicted = _text(predicted, f"{path}.predicted_metric")
        return cls(
            entry_id=_text(value.get("id"), f"{path}.id"),
            title=_text(value.get("title"), f"{path}.title"),
            rationale=_text(value.get("rationale"), f"{path}.rationale"),
            triggers=_string_tuple(value.get("triggers"), f"{path}.triggers"),
            patch_path=patch_path,
            patch_sha256=hashlib.sha256(patch_bytes).hexdigest(),
            predicted_metric=predicted,
            risk=_text(value.get("risk", "medium"), f"{path}.risk"),
        )


@dataclass(frozen=True, slots=True)
class PatchCatalog:
    entries: tuple[PatchCatalogEntry, ...]
    source: Path

    @classmethod
    def load(cls, path: str | Path) -> PatchCatalog:
        source = Path(path).expanduser().resolve()
        try:
            raw: object = yaml.safe_load(source.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PatchCatalogError(f"invalid YAML in {source}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise PatchCatalogError("patch catalog must be a mapping")
        unknown = sorted(set(raw) - {"schema_version", "entries"})
        if unknown:
            raise PatchCatalogError(
                f"patch catalog contains unknown field(s): {', '.join(unknown)}"
            )
        if raw.get("schema_version") != 1:
            raise PatchCatalogError("patch catalog schema_version must be 1")
        raw_entries = raw.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise PatchCatalogError("patch catalog entries must be a non-empty list")
        entries: list[PatchCatalogEntry] = []
        for index, raw_entry in enumerate(raw_entries):
            if not isinstance(raw_entry, Mapping) or not all(
                isinstance(key, str) for key in raw_entry
            ):
                raise PatchCatalogError(f"entries[{index}] must be a string-keyed mapping")
            entries.append(
                PatchCatalogEntry.from_mapping(raw_entry, index=index, catalog_path=source)
            )
        entry_ids = tuple(entry.entry_id for entry in entries)
        if len(set(entry_ids)) != len(entry_ids):
            raise PatchCatalogError("patch catalog entry ids must be unique")
        digests = tuple(entry.patch_sha256 for entry in entries)
        if len(set(digests)) != len(digests):
            raise PatchCatalogError("patch catalog must not contain duplicate patch content")
        return cls(entries=tuple(entries), source=source)

    def get(self, entry_id: str) -> PatchCatalogEntry:
        for entry in self.entries:
            if entry.entry_id == entry_id:
                return entry
        raise KeyError(entry_id)


class RulePlanner:
    """Match analyzer finding categories to reviewed catalog entries."""

    def __init__(
        self,
        catalog: PatchCatalog,
        *,
        max_proposals: int = 1,
        reject_duplicates: bool = True,
    ) -> None:
        if isinstance(max_proposals, bool) or not isinstance(max_proposals, int):
            raise TypeError("max_proposals must be an integer")
        if max_proposals <= 0:
            raise ValueError("max_proposals must be positive")
        self.catalog = catalog
        self.max_proposals = max_proposals
        self.reject_duplicates = reject_duplicates

    def propose(
        self,
        report: AnalysisReport,
        recalled: tuple[MemoryEntry, ...],
        context: StageContext,
    ) -> tuple[ChangeProposal, ...]:
        del context  # reserved for future scope-aware policies
        attempted_proposals = {memory.proposal_id for memory in recalled}
        attempted_patches = {
            memory.patch_digest for memory in recalled if memory.patch_digest is not None
        }
        findings = sorted(report.findings, key=lambda finding: finding.confidence, reverse=True)
        proposals: list[ChangeProposal] = []
        selected_entries: set[str] = set()
        for finding in findings:
            for entry in self.catalog.entries:
                if entry.entry_id in selected_entries:
                    continue
                if "*" not in entry.triggers and finding.category not in entry.triggers:
                    continue
                proposal_seed = f"{report.analysis_id}\0{entry.entry_id}\0{entry.patch_sha256}"
                proposal_id = f"proposal-{hashlib.sha256(proposal_seed.encode()).hexdigest()[:16]}"
                if self.reject_duplicates and (
                    proposal_id in attempted_proposals or entry.patch_sha256 in attempted_patches
                ):
                    continue
                proposals.append(
                    ChangeProposal(
                        proposal_id=proposal_id,
                        analysis_id=report.analysis_id,
                        title=entry.title,
                        rationale=entry.rationale,
                        change_kind=ChangeKind.PATCH_CATALOG,
                        catalog_entry_id=entry.entry_id,
                        predicted_metric=entry.predicted_metric,
                        risk=entry.risk,
                        metadata={
                            "finding_id": finding.finding_id,
                            "finding_category": finding.category,
                            "finding_confidence": finding.confidence,
                            "patch_path": str(entry.patch_path),
                            "patch_sha256": entry.patch_sha256,
                        },
                    )
                )
                selected_entries.add(entry.entry_id)
                if len(proposals) >= self.max_proposals:
                    return tuple(proposals)
        return tuple(proposals)


__all__ = [
    "PatchCatalog",
    "PatchCatalogEntry",
    "PatchCatalogError",
    "RulePlanner",
]
