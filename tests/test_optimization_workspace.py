from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from euboulia.optimization.workspace import (
    GitWorktreeWorkspace,
    PatchLimits,
    PatchRejected,
    WorkspaceAuthorizationError,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=check,
        capture_output=True,
        text=True,
        shell=False,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    path = tmp_path / "source"
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "euboulia@example.invalid")
    _git(path, "config", "user.name", "Euboulia Tests")
    (path / "value.txt").write_text("old\n", encoding="utf-8")
    (path / "second.txt").write_text("alpha\n", encoding="utf-8")
    _git(path, "add", "value.txt", "second.txt")
    _git(path, "commit", "-m", "base")
    return path


def _workspace(repository: Path) -> GitWorktreeWorkspace:
    return GitWorktreeWorkspace.create(repository, repository.parent / "candidate")


def _valid_patch() -> str:
    return """diff --git a/value.txt b/value.txt
--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
+new
"""


def test_create_uses_detached_worktree_and_preserves_manifest(repository: Path) -> None:
    workspace = _workspace(repository)
    (repository / "value.txt").write_text("dirty source\n", encoding="utf-8")

    assert (workspace.path / "value.txt").read_text(encoding="utf-8") == "old\n"
    branch = _git(workspace.path, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    assert branch.returncode != 0
    assert _git(workspace.path, "rev-parse", "HEAD").stdout.strip() == workspace.base_commit
    assert (workspace.evidence_dir / "workspace-manifest.json").is_file()
    assert workspace.path.exists()


def test_prepare_is_read_only_and_apply_requires_explicit_authorization(
    repository: Path,
) -> None:
    workspace = _workspace(repository)
    prepared = workspace.prepare_patch(_valid_patch())

    assert (workspace.path / "value.txt").read_text(encoding="utf-8") == "old\n"
    assert prepared.check_evidence.succeeded
    with pytest.raises(WorkspaceAuthorizationError):
        workspace.apply_patch(prepared, authorize=False)
    assert (workspace.path / "value.txt").read_text(encoding="utf-8") == "old\n"

    applied = workspace.apply_patch(prepared, authorize=True)

    assert applied.succeeded
    assert (workspace.path / "value.txt").read_text(encoding="utf-8") == "new\n"
    assert "+new" in applied.diff_evidence.stdout_path.read_text(encoding="utf-8")
    assert workspace.path.exists(), "the evidence-preserving API must not auto-clean"


def test_workspace_commands_are_argv_only_and_do_not_interpret_shell_tokens(
    repository: Path,
) -> None:
    workspace = _workspace(repository)
    sentinel = workspace.path / "shell-was-run"
    hostile_argument = f"; touch {sentinel}"
    result = workspace.execute(
        [
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path('argument.txt').write_text(sys.argv[1])",
            hostile_argument,
        ]
    )

    assert result.succeeded
    assert not sentinel.exists()
    assert (workspace.path / "argument.txt").read_text(encoding="utf-8") == hostile_argument
    with pytest.raises(TypeError):
        workspace.execute("echo unsafe")


def test_workspace_commands_do_not_inherit_undeclared_environment(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EUBOULIA_UNDECLARED_SECRET", "must-not-leak")
    workspace = _workspace(repository)
    result = workspace.execute(
        [
            sys.executable,
            "-c",
            "import os,pathlib; pathlib.Path('env.txt').write_text("
            "os.environ.get('EUBOULIA_UNDECLARED_SECRET', 'missing'))",
        ]
    )

    assert result.succeeded
    assert (workspace.path / "env.txt").read_text(encoding="utf-8") == "missing"


@pytest.mark.parametrize(
    "header",
    [
        "diff --git a/value.txt b/../escape.txt",
        "diff --git a/value.txt /tmp/escape.txt",
        "diff --git a/value.txt b/.git/config",
    ],
)
def test_patch_rejects_path_escape_and_retains_rejection_evidence(
    repository: Path, header: str
) -> None:
    workspace = _workspace(repository)
    patch = f"""{header}
--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
+new
"""

    with pytest.raises(PatchRejected):
        workspace.prepare_patch(patch)

    assert list(workspace.evidence_dir.glob("patch-*.diff"))
    assert list(workspace.evidence_dir.glob("patch-*.rejected.json"))


def test_patch_rejects_new_and_existing_symlinks(repository: Path) -> None:
    workspace = _workspace(repository)
    symlink_patch = """diff --git a/link b/link
new file mode 120000
--- /dev/null
+++ b/link
@@ -0,0 +1 @@
+value.txt
"""
    with pytest.raises(PatchRejected, match="symbolic-link"):
        workspace.prepare_patch(symlink_patch)

    (workspace.path / "alias").symlink_to(workspace.path, target_is_directory=True)
    through_symlink = """diff --git a/alias/value.txt b/alias/value.txt
--- a/alias/value.txt
+++ b/alias/value.txt
@@ -1 +1 @@
-old
+new
"""
    with pytest.raises(PatchRejected, match="traverses a symlink"):
        workspace.prepare_patch(through_symlink)


def test_patch_budgets_are_enforced_before_git_apply(repository: Path) -> None:
    patch = _valid_patch()

    byte_workspace = _workspace(repository)
    with pytest.raises(PatchRejected, match="byte budget"):
        byte_workspace.prepare_patch(
            patch,
            limits=PatchLimits(max_bytes=len(patch.encode()) - 1),
        )

    line_workspace = GitWorktreeWorkspace.create(repository, repository.parent / "candidate-lines")
    with pytest.raises(PatchRejected, match="changed-line budget"):
        line_workspace.prepare_patch(patch, limits=PatchLimits(max_changed_lines=1))

    two_files = (
        patch
        + """diff --git a/second.txt b/second.txt
--- a/second.txt
+++ b/second.txt
@@ -1 +1 @@
-alpha
+beta
"""
    )
    file_workspace = GitWorktreeWorkspace.create(repository, repository.parent / "candidate-files")
    with pytest.raises(PatchRejected, match="file budget"):
        file_workspace.prepare_patch(two_files, limits=PatchLimits(max_files=1))


def test_git_apply_check_failure_and_tampering_leave_evidence(repository: Path) -> None:
    workspace = _workspace(repository)
    bad_context = _valid_patch().replace("-old", "-not-the-current-content")
    with pytest.raises(PatchRejected, match="git apply --check"):
        workspace.prepare_patch(bad_context)
    assert list(workspace.evidence_dir.glob("patch-*-check-*.stderr.log"))

    prepared = workspace.prepare_patch(_valid_patch())
    prepared.patch_path.write_text(_valid_patch().replace("+new", "+tampered"), encoding="utf-8")
    with pytest.raises(PatchRejected, match="changed after validation"):
        workspace.apply_patch(prepared, authorize=True)
