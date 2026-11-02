"""Sparse-checkout regression for the workflow's helper steps.

The helper is fetched with `sparse-checkout: scripts/publish.py` — a single
file, not a directory. Current Git rejects cone-mode file arguments; older
Git accepts them but includes sibling files. Both helper steps therefore use
`sparse-checkout-cone-mode: false` (the
official single-file form). These tests run the actual git commands on a
fixture committed repo and then execute the sparsely-checked-out helper —
not just YAML string assertions (those live in test_publish_workflow.py).
No network, no upload.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_publish_verify import make_pair

PUBLISH_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish.py"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=False, text=True, timeout=120)


@pytest.fixture()
def helper_repo(tmp_path):
    """Committed repo containing the real helper plus unrelated files."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "helper-repo"
    repo.mkdir()
    assert run_git("init", cwd=repo).returncode == 0
    assert run_git("config", "user.email", "test@example.com", cwd=repo).returncode == 0
    assert run_git("config", "user.name", "test", cwd=repo).returncode == 0
    (repo / "scripts").mkdir()
    (repo / "scripts" / "publish.py").write_bytes(PUBLISH_SCRIPT.read_bytes())
    (repo / "scripts" / "unrelated.py").write_text("# not part of the helper checkout\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "other.md").write_text("unrelated\n")
    assert run_git("add", ".", cwd=repo).returncode == 0
    assert run_git("commit", "-m", "helper plus unrelated files", cwd=repo).returncode == 0
    return repo


def sparse_clone(repo: Path, dest: Path, no_cone: bool) -> subprocess.CompletedProcess[str]:
    assert run_git("clone", str(repo), str(dest), cwd=repo.parent).returncode == 0
    mode = "--no-cone" if no_cone else "--cone"
    assert run_git("sparse-checkout", "init", mode, cwd=dest).returncode == 0
    args = ["sparse-checkout", "set"]
    if no_cone:
        args.append("--no-cone")
    args.append("scripts/publish.py")
    return run_git(*args, cwd=dest)


@needs_git
def test_cone_mode_cannot_guarantee_single_file_checkout(helper_repo, tmp_path):
    dest = tmp_path / "cone-clone"
    proc = sparse_clone(helper_repo, dest, no_cone=False)
    if proc.returncode == 0:
        # Git 2.34 accepts the argument as a cone and overselects its parent.
        assert (dest / "scripts" / "unrelated.py").is_file()
    else:
        assert proc.returncode == 128
        assert "not a directory" in proc.stderr or "skip-checks" in proc.stderr
        assert not (dest / "scripts" / "publish.py").exists()


@needs_git
def test_no_cone_checks_out_single_file_and_helper_runs(helper_repo, tmp_path):
    # The workflow's form (`sparse-checkout-cone-mode: false`): only the
    # helper lands in the worktree, and it executes for real.
    dest = tmp_path / "helper"
    proc = sparse_clone(helper_repo, dest, no_cone=True)
    assert proc.returncode == 0, proc.stderr
    helper = dest / "scripts" / "publish.py"
    assert helper.is_file()
    assert not (dest / "docs" / "other.md").exists()
    assert not (dest / "scripts" / "unrelated.py").exists()
    dist = tmp_path / "dist"
    dist.mkdir()
    make_pair(dist, "0.2.23")
    check = subprocess.run(
        [sys.executable, str(helper), "check-dist", "--version", "0.2.23", "--dist", str(dist)],
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )
    assert check.returncode == 0, check.stderr
    assert "local dist ok" in check.stdout
