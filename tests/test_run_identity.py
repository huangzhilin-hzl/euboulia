import re

import pytest

import euboulia.run_identity as run_identity


def test_run_uid_is_ulid_shaped_and_time_sortable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_identity.secrets, "randbits", lambda bits: 0)
    monkeypatch.setattr(run_identity.time, "time_ns", lambda: 1_700_000_000_000_000_000)
    earlier = run_identity.new_run_uid()
    monkeypatch.setattr(run_identity.time, "time_ns", lambda: 1_700_000_000_001_000_000)
    later = run_identity.new_run_uid()

    assert re.fullmatch(r"run-[0-9A-HJKMNP-TV-Z]{26}", earlier)
    assert earlier < later


def test_run_name_is_optional_trimmed_display_metadata() -> None:
    assert run_identity.normalize_run_name(None) is None
    assert run_identity.normalize_run_name("  repeated baseline  ") == "repeated baseline"

    with pytest.raises(ValueError, match="must not be empty"):
        run_identity.normalize_run_name("   ")
