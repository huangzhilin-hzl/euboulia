import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from euboulia.adapters import AdapterCommand, BaseAdapter, BenchmarkType
from euboulia.campaign import CampaignSafetyError, plan_campaign, run_campaign
from euboulia.config import CampaignConfig, load_config
from euboulia.ledger import ExperimentLedger
from euboulia.models import ExperimentStatus


class FakeAdapter(BaseAdapter):
    framework = "vllm"

    def __init__(self, *, manages_service_lifecycle: bool = False) -> None:
        self.manages_service_lifecycle = manages_service_lifecycle

    def build_command(
        self,
        mode: str | BenchmarkType,
        workload: Mapping[str, object],
        parameters: Mapping[str, object],
        base_args: Sequence[str],
        result_path: str | Path,
    ) -> AdapterCommand:
        del workload, base_args
        result = Path(result_path)
        payload = {
            "completed": 10,
            "failed": 0,
            "output_throughput": parameters["throughput"],
            "success_rate": parameters.get("success_rate", 1.0),
        }
        script = (
            "import json,sys; "
            "from pathlib import Path; "
            "Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')"
        )
        return AdapterCommand(
            framework=self.framework,
            benchmark_type=BenchmarkType.SERVE,
            argv=(sys.executable, "-c", script, str(result), json.dumps(payload)),
            result_path=result,
            manages_service_lifecycle=self.manages_service_lifecycle,
        )


def campaign_config(tmp_path: Path, *, baseline_success: float = 1.0) -> CampaignConfig:
    path = tmp_path / "campaign.yaml"
    path.write_text(
        f"""
schema_version: 1
name: unit-campaign
framework: vllm
workload:
  name: fixed
  model: test/model
  input_tokens: 128
  output_tokens: 32
  concurrency: 4
  num_prompts: 10
  endpoint: http://127.0.0.1:8000
  dataset: random
benchmark:
  mode: serve
  base_args: []
  result_filename: result.json
candidates:
  - id: baseline
    parameters:
      throughput: 100
      success_rate: {baseline_success}
    env:
      TEST_SECRET: do-not-persist
  - id: faster
    parameters:
      throughput: 110
      success_rate: 1.0
gates:
  correctness:
    metric: success_rate
    minimum: 1.0
  performance:
    metric: output_throughput
    direction: maximize
    min_relative_improvement: 0.05
    max_regression: 0.0
execution:
  artifacts_dir: artifacts
  ledger: artifacts/ledger.jsonl
  timeout_seconds: 30
  env: {{}}
""",
        encoding="utf-8",
    )
    return load_config(path)


def test_plan_is_side_effect_free_and_redacts_environment(tmp_path: Path) -> None:
    config = campaign_config(tmp_path)

    plans = plan_campaign(config, adapter=FakeAdapter())

    assert plans[0].candidate.environment == {"TEST_SECRET": "<redacted>"}
    assert not config.execution.artifacts_dir.exists()


def test_run_records_baseline_and_accepted_candidate(tmp_path: Path) -> None:
    config = campaign_config(tmp_path)

    result = run_campaign(
        config,
        execute=True,
        adapter=FakeAdapter(),
        run_id="test-run",
    )

    assert result.accepted == 1
    assert result.rejected == 0
    assert result.failed == 0
    assert [item.status for item in result.experiments] == [
        ExperimentStatus.SUCCEEDED,
        ExperimentStatus.SUCCEEDED,
    ]
    snapshots = ExperimentLedger(config.execution.ledger).read_all()
    assert len(snapshots) == 4
    assert snapshots[-1].verdict is not None
    assert snapshots[-1].verdict.accepted
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in config.execution.artifacts_dir.rglob("*")
        if path.is_file()
    )
    assert "do-not-persist" not in persisted


def test_invalid_baseline_cancels_remaining_candidates(tmp_path: Path) -> None:
    config = campaign_config(tmp_path, baseline_success=0.9)

    result = run_campaign(
        config,
        execute=True,
        adapter=FakeAdapter(),
        run_id="bad-baseline",
    )

    assert result.stopped_reason == "baseline correctness gate failed"
    assert result.experiments[-1].status is ExperimentStatus.CANCELLED
    assert len(ExperimentLedger(config.execution.ledger).read_all()) == 3


def test_lifecycle_command_is_plan_only(tmp_path: Path) -> None:
    config = campaign_config(tmp_path)

    with pytest.raises(CampaignSafetyError, match="service lifecycle"):
        run_campaign(
            config,
            execute=True,
            adapter=FakeAdapter(manages_service_lifecycle=True),
            run_id="blocked",
        )

    assert not config.execution.artifacts_dir.exists()


def test_adapter_managed_output_cannot_be_overridden(tmp_path: Path) -> None:
    config = campaign_config(tmp_path)
    config = replace(
        config,
        benchmark=replace(
            config.benchmark,
            base_args=("--output-json", "/tmp/escape.json"),
        ),
    )

    with pytest.raises(CampaignSafetyError, match="adapter-managed option"):
        plan_campaign(config, adapter=FakeAdapter())
