"""Build deterministic exact-token-length ShareGPT datasets and a hash manifest."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

DEFAULT_LENGTHS = (1024, 16384, 32768, 65536, 131072, 262144)


class Tokenizer(Protocol):
    def encode(self, text: str) -> Sequence[int]: ...

    def __call__(
        self,
        text: str,
        *,
        return_offsets_mapping: bool,
        add_special_tokens: bool,
    ) -> Mapping[str, Sequence[object]]: ...


TokenizerLoader = Callable[[str], Tokenizer]


class AutoTokenizerFactory(Protocol):
    def from_pretrained(self, path: str, *, trust_remote_code: bool) -> Tokenizer: ...


@dataclass(frozen=True, slots=True)
class PreparationSettings:
    source: Path
    tokenizer_path: Path
    output_dir: Path
    lengths: tuple[int, ...] = DEFAULT_LENGTHS
    samples: int = 16
    seed: int = 1


def prepare_datasets(
    settings: PreparationSettings,
    *,
    tokenizer_loader: TokenizerLoader | None = None,
) -> dict[str, object]:
    if settings.samples < 1:
        raise ValueError("samples must be positive")
    if not settings.lengths or any(length < 1 for length in settings.lengths):
        raise ValueError("lengths must contain only positive integers")
    manifest_path = settings.output_dir / "manifest.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        raise FileExistsError(f"dataset manifest already exists: {manifest_path}")
    source_text = settings.source.read_text(encoding="utf-8")
    source: object = json.loads(source_text)
    if not isinstance(source, list):
        raise ValueError("ShareGPT source must be a JSON list")
    rows = _usable_rows(source)
    random.Random(settings.seed).shuffle(rows)
    loader = _default_tokenizer_loader if tokenizer_loader is None else tokenizer_loader
    tokenizer = loader(str(settings.tokenizer_path))
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, object] = {
        "source_path": str(settings.source),
        "source_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
        "tokenizer_path": str(settings.tokenizer_path),
        "tokenizer_class": tokenizer.__class__.__name__,
        "seed": settings.seed,
        "samples_per_length": settings.samples,
        "datasets": {},
    }
    datasets = cast(dict[str, object], manifest["datasets"])
    for target_tokens in sorted(set(settings.lengths)):
        dataset: list[dict[str, object]] = []
        sample_metadata: list[dict[str, object]] = []
        prompt_hashes: set[str] = set()
        for sample_index in range(settings.samples):
            candidate, completion, source_rows = _build_candidate(
                tokenizer,
                rows,
                sample_index,
                settings.samples,
                target_tokens,
            )
            prompt, prompt_ids = _exact_prefix(tokenizer, candidate, target_tokens)
            prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
            if prompt_hash in prompt_hashes:
                raise ValueError(
                    f"ISL {target_tokens} produced duplicate prompt {sample_index}"
                )
            prompt_hashes.add(prompt_hash)
            dataset.append(
                {
                    "id": (
                        f"sharegpt-isl{target_tokens}-sample{sample_index}-seed{settings.seed}"
                    ),
                    "conversations": [
                        {"from": "human", "value": prompt},
                        {"from": "gpt", "value": completion},
                    ],
                }
            )
            sample_metadata.append(
                {
                    "sample_index": sample_index,
                    "input_tokens": len(prompt_ids),
                    "source_rows_concatenated": source_rows,
                    "prompt_utf8_sha256": prompt_hash,
                    "prompt_token_ids_sha256": hashlib.sha256(
                        ",".join(map(str, prompt_ids)).encode("ascii")
                    ).hexdigest(),
                }
            )
        output_path = settings.output_dir / (
            f"sharegpt_isl{target_tokens}_n{settings.samples}.json"
        )
        if output_path.exists() or output_path.is_symlink():
            raise FileExistsError(f"dataset output already exists: {output_path}")
        output_text = json.dumps(dataset, ensure_ascii=False, indent=2) + "\n"
        output_path.write_text(output_text, encoding="utf-8")
        datasets[str(target_tokens)] = {
            "path": str(output_path),
            "dataset_sha256": hashlib.sha256(output_text.encode()).hexdigest(),
            "samples": sample_metadata,
        }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _usable_rows(source: Sequence[object]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for raw_row in source:
        if not isinstance(raw_row, Mapping):
            continue
        conversations = raw_row.get("conversations", raw_row.get("conversation", []))
        if not isinstance(conversations, list) or len(conversations) < 2:
            continue
        first, second = conversations[:2]
        if not isinstance(first, Mapping) or not isinstance(second, Mapping):
            continue
        prompt = first.get("value")
        completion = second.get("value")
        if isinstance(prompt, str) and prompt and isinstance(completion, str) and completion:
            rows.append((prompt, completion))
    if not rows:
        raise ValueError("ShareGPT source contains no usable two-turn rows")
    return rows


def _encode(tokenizer: Tokenizer, text: str) -> list[int]:
    return list(tokenizer.encode(text))


def _exact_prefix(
    tokenizer: Tokenizer, candidate: str, target_tokens: int
) -> tuple[str, list[int]]:
    encoded = tokenizer(
        candidate,
        return_offsets_mapping=True,
        add_special_tokens=True,
    )
    ids = encoded.get("input_ids")
    offsets = encoded.get("offset_mapping")
    if ids is None or offsets is None or len(ids) < target_tokens:
        raise ValueError(f"candidate has fewer than {target_tokens} tokens")
    try:
        end = max(
            int(cast(int | str, cast(Sequence[object], offset)[1]))
            for offset in offsets[:target_tokens]
        )
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("tokenizer returned invalid offset_mapping") from exc
    end = max(1, min(end, len(candidate)))
    prompt = candidate[:end]
    prompt_ids = _encode(tokenizer, prompt)
    if len(prompt_ids) < target_tokens:
        while end < len(candidate) and len(prompt_ids) < target_tokens:
            end += 1
            prompt = candidate[:end]
            prompt_ids = _encode(tokenizer, prompt)
    elif len(prompt_ids) > target_tokens:
        while end > 1 and len(prompt_ids) > target_tokens:
            end -= 1
            prompt = candidate[:end]
            prompt_ids = _encode(tokenizer, prompt)
    if len(prompt_ids) != target_tokens:
        raise ValueError(
            f"could not obtain exact length {target_tokens}; got {len(prompt_ids)}"
        )
    return prompt, prompt_ids


def _build_candidate(
    tokenizer: Tokenizer,
    rows: Sequence[tuple[str, str]],
    sample_index: int,
    sample_count: int,
    target_tokens: int,
) -> tuple[str, str, int]:
    parts: list[str] = []
    cursor = sample_index
    character_count = 0
    next_token_check = target_tokens * 3
    while True:
        prompt, completion = rows[cursor % len(rows)]
        parts.append(prompt)
        character_count += len(prompt) + 2
        cursor += sample_count
        if character_count < next_token_check:
            continue
        candidate = "\n\n".join(parts)
        candidate_tokens = len(_encode(tokenizer, candidate))
        if candidate_tokens >= target_tokens + 16:
            return candidate, completion, len(parts)
        missing_tokens = target_tokens + 16 - candidate_tokens
        characters_per_token = character_count / max(candidate_tokens, 1)
        next_token_check = character_count + max(
            10_000, int(missing_tokens * characters_per_token * 1.1)
        )


def _default_tokenizer_loader(path: str) -> Tokenizer:
    try:
        transformers = importlib.import_module("transformers")
    except ModuleNotFoundError as exc:
        raise RuntimeError("transformers is required to prepare ShareGPT datasets") from exc
    auto_tokenizer = cast(AutoTokenizerFactory, transformers.AutoTokenizer)
    return auto_tokenizer.from_pretrained(path, trust_remote_code=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--lengths", type=int, nargs="+", default=DEFAULT_LENGTHS)
    parser.add_argument("--samples", type=int, default=16)
    parser.add_argument("--seed", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    manifest = prepare_datasets(
        PreparationSettings(
            source=args.source,
            tokenizer_path=args.tokenizer,
            output_dir=args.output_dir,
            lengths=tuple(args.lengths),
            samples=args.samples,
            seed=args.seed,
        )
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
