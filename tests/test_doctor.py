from euboulia.doctor import DoctorCheck, required_checks_pass


def test_required_checks_pass_ignores_optional_tools() -> None:
    checks = (
        DoctorCheck("python", True, "3.11", required=True),
        DoctorCheck("ncu", False, "not found"),
    )

    assert required_checks_pass(checks)


def test_required_checks_fail_closed() -> None:
    checks = (DoctorCheck("python", False, "3.10", required=True),)

    assert not required_checks_pass(checks)
