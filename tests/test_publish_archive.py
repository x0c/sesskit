"""Tests for scripts/publish.py clean-archive route.

Uses a throwaway git repository. The archive must always reflect the committed
tag — never the dirty working tree — and the working tree must be left alone.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

PUBLISH_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish.py"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def load_publish():
    spec = importlib.util.spec_from_file_location("sesskit_publish_archive", PUBLISH_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish = load_publish()


def run_git(repo: Path, *args: str) -> None:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, check=False, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr


@pytest.fixture()
def tagged_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "test")
    (repo / "pyproject.toml").write_text('[project]\nname = "sesskit"\nversion = "0.9.99"\n')
    (repo / "pkg.txt").write_text("committed\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "release commit")
    run_git(repo, "tag", "v0.9.99")
    return repo


@needs_git
def test_clean_archive_from_tag(tagged_repo, tmp_path):
    out = tmp_path / "out"
    rc = publish.main(["clean-archive", "--version", "0.9.99", "--repo-root", str(tagged_repo), "--out", str(out)])
    assert rc == 0
    archive = out / "sesskit-0.9.99-git-archive.tar.gz"
    assert archive.is_file()
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        assert any(n.endswith("pyproject.toml") for n in names)
        pkg = [n for n in names if n.endswith("pkg.txt")]
        assert len(pkg) == 1
        handle = tar.extractfile(pkg[0])
        assert handle is not None
        assert handle.read().decode() == "committed\n"


@needs_git
def test_clean_archive_ignores_dirty_tree(tagged_repo, tmp_path, capsys):
    dirty = tagged_repo / "pkg.txt"
    dirty.write_text("dirty-uncommitted\n")
    (tagged_repo / "untracked.txt").write_text("new\n")
    out = tmp_path / "out"
    rc = publish.main(["clean-archive", "--version", "0.9.99", "--repo-root", str(tagged_repo), "--out", str(out)])
    assert rc == 0
    assert "uncommitted changes" in capsys.readouterr().out
    with tarfile.open(out / "sesskit-0.9.99-git-archive.tar.gz", "r:gz") as tar:
        pkg = [n for n in tar.getnames() if n.endswith("pkg.txt")]
        handle = tar.extractfile(pkg[0])
        assert handle is not None
        # Archive holds the committed bytes, not the dirty ones.
        assert handle.read().decode() == "committed\n"
        assert not any(n.endswith("untracked.txt") for n in tar.getnames())
    # Working tree is preserved exactly: nothing overwritten, hidden, or moved.
    assert dirty.read_text() == "dirty-uncommitted\n"
    assert (tagged_repo / "untracked.txt").read_text() == "new\n"


@needs_git
def test_clean_archive_missing_tag(tagged_repo, tmp_path, capsys):
    rc = publish.main(
        ["clean-archive", "--version", "0.9.98", "--repo-root", str(tagged_repo), "--out", str(tmp_path / "out")]
    )
    assert rc == 2
    assert "does not exist" in capsys.readouterr().err


@needs_git
def test_clean_archive_version_mismatch(tagged_repo, tmp_path, capsys):
    # Tag v0.9.98 points at a commit whose pyproject still says 0.9.99.
    run_git(tagged_repo, "tag", "v0.9.98")
    rc = publish.main(
        ["clean-archive", "--version", "0.9.98", "--repo-root", str(tagged_repo), "--out", str(tmp_path / "out")]
    )
    assert rc == 2
    assert "pyproject version" in capsys.readouterr().err


@needs_git
def test_clean_archive_default_out_cleaned_up(tagged_repo, tmp_path, monkeypatch, capsys):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    rc = publish.main(["clean-archive", "--version", "0.9.99", "--repo-root", str(tagged_repo)])
    assert rc == 0
    assert "temporary output removed" in capsys.readouterr().out
    assert list(scratch.iterdir()) == []


@needs_git
def test_clean_archive_failure_leaves_no_temp(tagged_repo, tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    # Missing tag fails before any archive directory is created.
    rc = publish.main(["clean-archive", "--version", "0.9.98", "--repo-root", str(tagged_repo)])
    assert rc == 2
    assert list(scratch.iterdir()) == []
