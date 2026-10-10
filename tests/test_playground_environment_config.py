from pathlib import Path
from typing import Any

import yaml

from euboulia.playground.config import load_config
from euboulia.playground.server import public_config


def configuration(tmp_path: Path, profiles: dict[str, Any] | None = None) -> Path:
    pod = tmp_path / "pod.yaml"
    pod.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "spec": {"containers": [{"name": "gpu", "image": "test/image:latest"}]},
            }
        )
    )
    raw: dict[str, Any] = {
        "version": 1,
        "storage": str(tmp_path / "records"),
        "clusters": {
            "test": {
                "context": "test",
                "pod_template": str(pod),
                "gpu_access": "shared-nvidia-runtime",
            }
        },
    }
    if profiles is not None:
        raw["profiles"] = profiles
    path = tmp_path / "playground.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


def test_default_has_one_environment_with_tirx_and_all_existing_dsl_dependencies(
    tmp_path: Path,
) -> None:
    config = load_config(configuration(tmp_path))
    assert list(config.profiles) == ["gpu"]
    profile = config.profiles["gpu"]
    assert profile.system_site_packages
    packages = " ".join(profile.packages)
    assert "nvidia-cutlass-dsl" in packages
    assert "tilelang" in packages
    assert "apache-tvm[cuda]" in packages
    assert "apache-tvm-ffi" in packages
    browser = public_config(config)
    assert len(browser["profiles"]) == 1
    assert browser["profiles"][0]["name"] == "gpu"
    assert "torch" in browser["profiles"][0]["code"]
    compile(browser["profiles"][0]["code"], "solution.py", "exec")


def test_explicit_legacy_profiles_remain_supported(tmp_path: Path) -> None:
    names = ["cutedsl", "tilelang", "python"]
    config = load_config(configuration(tmp_path, {name: {"packages": []} for name in names}))
    assert list(config.profiles) == names
    assert len(public_config(config)["profiles"]) == 3
