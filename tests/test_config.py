from pathlib import Path

import pytest

from euboulia.config import ConfigError, load_config

VALID_CONFIG = """
schema_version: 1
name: smoke
framework: vllm
workload:
  name: short
  model: test/model
  input_tokens: 128
  output_tokens: 32
  concurrency: 4
  num_prompts: 16
  endpoint: http://127.0.0.1:8000
  dataset: random
benchmark:
  mode: serve
  base_args: [--ignore-eos]
  result_filename: result.json
candidates:
  - id: baseline
    name: Baseline
    parameters: {}
    env: {}
    patch: null
  - id: c8
    parameters:
      max_concurrency: 8
gates:
  correctness:
    metric: success_rate
    minimum: 1.0
  performance:
    metric: output_throughput
    direction: maximize
    min_relative_improvement: 0.02
    max_regression: 0.01
    noise_tolerance: 0.005
execution:
  artifacts_dir: artifacts
  ledger: artifacts/experiments.jsonl
  timeout_seconds: 60
  env:
    TOKENIZERS_PARALLELISM: "false"
"""


def write_config(tmp_path: Path, text: str = VALID_CONFIG) -> Path:
    path = tmp_path / "campaign.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_resolves_paths_and_baseline(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))

    assert config.framework == "vllm"
    assert config.baseline.candidate_id == "baseline"
    assert config.candidates[1].parameters == {"max_concurrency": 8}
    assert config.gates.performance.noise_tolerance == 0.005
    assert config.execution.artifacts_dir == (tmp_path / "artifacts").resolve()
    assert config.execution.ledger == (tmp_path / "artifacts/experiments.jsonl").resolve()


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("schema_version: 1", "schema_version: 2", "unsupported schema_version"),
        ("framework: vllm", "framework: other", "framework must be"),
        ("direction: maximize", "direction: sideways", "direction must be"),
        ("id: c8", "id: baseline", "duplicate candidate id"),
        ("result_filename: result.json", "result_filename: ../result.json", "plain filename"),
    ],
)
def test_invalid_config_fails_closed(tmp_path: Path, old: str, new: str, message: str) -> None:
    path = write_config(tmp_path, VALID_CONFIG.replace(old, new))

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_ledger_must_stay_within_artifact_directory(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        VALID_CONFIG.replace(
            "ledger: artifacts/experiments.jsonl", "ledger: elsewhere/experiments.jsonl"
        ),
    )

    with pytest.raises(ConfigError, match="ledger must be located inside"):
        load_config(path)


def test_artifact_directory_cannot_be_campaign_directory(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, VALID_CONFIG.replace("artifacts_dir: artifacts", "artifacts_dir: .")
    )

    with pytest.raises(ConfigError, match="campaign directory"):
        load_config(path)
