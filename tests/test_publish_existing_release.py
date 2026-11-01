"""Old-tag flow regression: the selected release tag lacks scripts/publish.py.

Regression for the failed first-PyPI dispatch: checking out an old tag and
running `python scripts/publish.py` from that checkout cannot work because
the helper postdates the tag. The supported flow validates the tag's
already-published immutable assets with the helper from the trusted workflow
revision (outside the tag checkout) and hands the validated files off —
never rebuilding or relabeling them. No upload happens anywhere here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest
from test_publish_verify import make_pair

PUBLISH_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish.py"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def load_publish():
    spec = importlib.util.spec_from_file_location("sesskit_publish_existing", PUBLISH_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish = load_publish()


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, check=False, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def old_tag_repo(tmp_path):
    """Git repo whose release tag predates the publish helper (no scripts/)."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "old-repo"
    repo.mkdir()
    run_git(repo, "init")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "test")
    (repo / "pyproject.toml").write_text('[project]\nname = "sesskit"\nversion = "0.9.98"\n')
    (repo / "pkg.txt").write_text("old release\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "old release without publish helper")
    run_git(repo, "tag", "v0.9.98")
    return repo


@needs_git
def test_tag_source_lacks_helper(old_tag_repo):
    # This is the exact reported failure: the helper is not in the old tag.
    proc = subprocess.run(
        ["git", "cat-file", "-e", "v0.9.98:scripts/publish.py"],
        cwd=str(old_tag_repo),
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 128


@needs_git
def test_old_tag_flow_validates_published_assets(old_tag_repo, tmp_path, monkeypatch, capsys):
    """End-to-end old-tag flow: helper from outside the tag checkout validates
    the tag's already-published assets and hands them off. No rebuild, no upload."""
    published = tmp_path / "published"
    published.mkdir()
    wheel, sdist = make_pair(published, "0.9.98")
    # The helper under test lives outside the tag checkout (trusted revision),
    # exactly like the workflow's helper checkout.
    assert PUBLISH_SCRIPT not in old_tag_repo.rglob("publish.py")
    work_dir = tmp_path / "validated"
    rc = publish.main(
        [
            "verify",
            "--version",
            "0.9.98",
            "--wheel-url",
            wheel.as_uri(),
            "--sdist-url",
            sdist.as_uri(),
            "--expected-wheel-sha256",
            sha256(wheel),
            "--expected-sdist-sha256",
            sha256(sdist),
            "--check-only",
            "--work-dir",
            str(work_dir),
        ]
    )
    assert rc == 0
    assert "verified sesskit 0.9.98" in capsys.readouterr().out
    # Handoff: the validated files (not a rebuild) pass the publisher gate.
    assert publish.main(["check-dist", "--version", "0.9.98", "--dist", str(work_dir)]) == 0
    # Nothing in this flow uploads: no twine invocation is possible without
    # the explicit upload command, which is never called here.
    monkeypatch.delenv("TWINE_PASSWORD", raising=False)
