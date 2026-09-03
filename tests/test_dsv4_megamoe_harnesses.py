from __future__ import annotations

import json
from pathlib import Path

import pytest

from euboulia.harnesses.sglang import prepare_sharegpt_exact


def test_prepare_sharegpt_exact_is_exact_and_deterministic(tmp_path: Path) -> None:
    class CharacterTokenizer:
        def encode(self, text: str) -> list[int]:
            return [ord(character) for character in text]

        def __call__(
            self,
            text: str,
            *,
            return_offsets_mapping: bool,
            add_special_tokens: bool,
        ) -> dict[str, list[object]]:
            assert return_offsets_mapping is True
            assert add_special_tokens is True
            return {
                "input_ids": list(self.encode(text)),
                "offset_mapping": [(index, index + 1) for index in range(len(text))],
            }

    source = tmp_path / "sharegpt.json"
    source.write_text(
        json.dumps(
            [
                {
                    "conversations": [
                        {"from": "human", "value": "a" * 200},
                        {"from": "gpt", "value": "answer-a"},
                    ]
                },
                {
                    "conversations": [
                        {"from": "human", "value": "b" * 200},
                        {"from": "gpt", "value": "answer-b"},
                    ]
                },
            ]
        ),
        encoding="utf-8",
    )

    manifests = []
    for output_name in ("first", "second"):
        settings = prepare_sharegpt_exact.PreparationSettings(
            source=source,
            tokenizer_path=tmp_path / "model",
            output_dir=tmp_path / output_name,
            lengths=(4, 8),
            samples=2,
            seed=1,
        )
        manifest = prepare_sharegpt_exact.prepare_datasets(
            settings,
            tokenizer_loader=lambda _: CharacterTokenizer(),
        )
        manifests.append(manifest)
        for length in (4, 8):
            dataset_path = settings.output_dir / f"sharegpt_isl{length}_n2.json"
            dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
            assert len(dataset) == 2
            assert {
                len(CharacterTokenizer().encode(row["conversations"][0]["value"]))
                for row in dataset
            } == {length}

    first_datasets = manifests[0]["datasets"]
    second_datasets = manifests[1]["datasets"]
    assert isinstance(first_datasets, dict)
    assert isinstance(second_datasets, dict)
    for length_key in ("4", "8"):
        assert first_datasets[length_key]["dataset_sha256"] == second_datasets[length_key][
            "dataset_sha256"
        ]
        assert first_datasets[length_key]["samples"] == second_datasets[length_key][
            "samples"
        ]

    with pytest.raises(FileExistsError, match="manifest already exists"):
        prepare_sharegpt_exact.prepare_datasets(
            prepare_sharegpt_exact.PreparationSettings(
                source=source,
                tokenizer_path=tmp_path / "model",
                output_dir=tmp_path / "first",
                lengths=(4,),
                samples=1,
            ),
            tokenizer_loader=lambda _: CharacterTokenizer(),
        )
