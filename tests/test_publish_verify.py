"""Tests for scripts/publish.py verify/check-dist/upload routes.

Local fixtures only: file:// URLs, generated wheel/sdist pairs, and a stub
`twine` executable. These tests never perform a real upload: the only upload
coverage drives a stub that records its argv, with a dummy credential.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

PUBLISH_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish.py"


def load_publish():
    spec = importlib.util.spec_from_file_location("sesskit_publish", PUBLISH_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish = load_publish()


def make_wheel(path: Path, version: str, name: str = "sesskit") -> Path:
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nSummary: test fixture\n"
    dist_info = f"{name}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", metadata)
        archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\n")
    return path


def make_sdist(
    path: Path,
    version: str,
    name: str = "sesskit",
    extra_pkg_info: list[tuple[str, str, str]] | None = None,
    arc_top: str | None = None,
) -> Path:
    """Build a minimal sdist; extra_pkg_info adds more PKG-INFO copies as (arcname, name, version); arc_top overrides the archive top directory."""
    pkg_info = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nSummary: test fixture\n"
    top = arc_top or f"{name}-{version}"
    data = pkg_info.encode("utf-8")
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(f"{top}/PKG-INFO")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        for arcname, extra_name, extra_version in extra_pkg_info or []:
            extra = f"Metadata-Version: 2.4\nName: {extra_name}\nVersion: {extra_version}\n".encode()
            extra_info = tarfile.TarInfo(arcname)
            extra_info.size = len(extra)
            archive.addfile(extra_info, io.BytesIO(extra))
    return path


def make_pair(tmp_path: Path, version: str):
    wheel = make_wheel(tmp_path / f"sesskit-{version}-py3-none-any.whl", version)
    sdist = make_sdist(tmp_path / f"sesskit-{version}.tar.gz", version)
    return wheel, sdist


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_version_parsing():
    assert publish.parse_version("0.2.23") == "0.2.23"
    assert publish.parse_version("v0.2.23") == "0.2.23"
    with pytest.raises(publish.PublishError):
        publish.parse_version("bad")
    with pytest.raises(publish.PublishError):
        publish.parse_version("1.2")


def test_release_asset_urls():
    wheel_url, sdist_url = publish.release_asset_urls("x0c/sesskit", "0.2.23")
    assert wheel_url == ("https://github.com/x0c/sesskit/releases/download/v0.2.23/sesskit-0.2.23-py3-none-any.whl")
    assert sdist_url == ("https://github.com/x0c/sesskit/releases/download/v0.2.23/sesskit-0.2.23.tar.gz")


def test_check_dist_ok(tmp_path):
    wheel, sdist = make_pair(tmp_path, "0.2.23")
    report = publish.check_dist_files(wheel, sdist, "0.2.23")
    assert report["wheel_sha256"] == sha256(wheel)
    assert report["sdist_sha256"] == sha256(sdist)


def test_check_dist_filename_mismatch(tmp_path):
    wheel, sdist = make_pair(tmp_path, "0.2.23")
    renamed = tmp_path / "sesskit-0.2.22-py3-none-any.whl"
    wheel.rename(renamed)
    with pytest.raises(publish.PublishError):
        publish.check_dist_files(renamed, sdist, "0.2.23")


def test_check_dist_wheel_version_mismatch(tmp_path):
    wheel = make_wheel(tmp_path / "sesskit-0.2.23-py3-none-any.whl", "0.2.22")
    sdist = make_sdist(tmp_path / "sesskit-0.2.23.tar.gz", "0.2.23")
    with pytest.raises(publish.PublishError, match="embedded version"):
        publish.check_dist_files(wheel, sdist, "0.2.23")


def test_check_dist_sdist_version_mismatch(tmp_path):
    wheel, _ = make_pair(tmp_path, "0.2.23")
    # Canonical member present, but its embedded version disagrees.
    sdist = make_sdist(tmp_path / "sesskit-0.2.23.tar.gz", "0.2.22", arc_top="sesskit-0.2.23")
    with pytest.raises(publish.PublishError, match="embedded version"):
        publish.check_dist_files(wheel, sdist, "0.2.23")


def test_check_dist_wrong_name(tmp_path):
    wheel = make_wheel(tmp_path / "sesskit-0.2.23-py3-none-any.whl", "0.2.23", name="other")
    sdist = make_sdist(tmp_path / "sesskit-0.2.23.tar.gz", "0.2.23")
    with pytest.raises(publish.PublishError, match="distribution name"):
        publish.check_dist_files(wheel, sdist, "0.2.23")


def test_check_dist_unreadable_artifacts(tmp_path):
    wheel = tmp_path / "sesskit-0.2.23-py3-none-any.whl"
    wheel.write_bytes(b"not a zip")
    sdist = tmp_path / "sesskit-0.2.23.tar.gz"
    sdist.write_bytes(b"not a tarball")
    with pytest.raises(publish.PublishError):
        publish.check_dist_files(wheel, sdist, "0.2.23")


def test_check_dist_sdist_wrong_top_dir(tmp_path):
    wheel, _ = make_pair(tmp_path, "0.2.23")
    sdist = make_sdist(tmp_path / "sesskit-0.2.23.tar.gz", "0.2.23", name="sesskit-x")
    # Canonical sesskit-0.2.23/PKG-INFO is absent, so the archive is rejected
    # even though an egg-info-style copy carries the right version.
    assert sdist.name == "sesskit-0.2.23.tar.gz"
    with pytest.raises(publish.PublishError, match="no top-level"):
        publish.check_dist_files(wheel, sdist, "0.2.23")


def test_check_dist_sdist_egginfo_only_rejected(tmp_path):
    wheel, _ = make_pair(tmp_path, "0.2.23")
    sdist = tmp_path / "sesskit-0.2.23.tar.gz"
    raw = b"Metadata-Version: 2.4\nName: sesskit\nVersion: 0.2.23\n"
    with tarfile.open(sdist, "w:gz") as archive:
        info = tarfile.TarInfo("sesskit-0.2.23/src/sesskit.egg-info/PKG-INFO")
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
    with pytest.raises(publish.PublishError, match="no top-level"):
        publish.check_dist_files(wheel, sdist, "0.2.23")


def test_check_dist_sdist_duplicate_pkginfo_rejected(tmp_path):
    wheel, _ = make_pair(tmp_path, "0.2.23")
    sdist = tmp_path / "sesskit-0.2.23.tar.gz"
    raw = b"Metadata-Version: 2.4\nName: sesskit\nVersion: 0.2.23\n"
    with tarfile.open(sdist, "w:gz") as archive:
        for _ in range(2):
            info = tarfile.TarInfo("sesskit-0.2.23/PKG-INFO")
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    with pytest.raises(publish.PublishError, match="duplicate PKG-INFO"):
        publish.check_dist_files(wheel, sdist, "0.2.23")


def test_check_dist_sdist_unsafe_pkginfo_path_rejected(tmp_path):
    wheel, _ = make_pair(tmp_path, "0.2.23")
    sdist = tmp_path / "sesskit-0.2.23.tar.gz"
    raw = b"Metadata-Version: 2.4\nName: sesskit\nVersion: 0.2.23\n"
    with tarfile.open(sdist, "w:gz") as archive:
        for arcname in ("sesskit-0.2.23/PKG-INFO", "sesskit-0.2.23/../evil/PKG-INFO"):
            info = tarfile.TarInfo(arcname)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    with pytest.raises(publish.PublishError, match="unsafe metadata path"):
        publish.check_dist_files(wheel, sdist, "0.2.23")


def test_sha256_tampered_asset(tmp_path):
    wheel, _ = make_pair(tmp_path, "0.2.23")
    expected = sha256(wheel)
    with open(wheel, "ab") as handle:
        handle.write(b"\x00")
    with pytest.raises(publish.PublishError, match="sha256 mismatch"):
        publish.check_sha256(wheel, expected, "wheel")


def test_verify_file_urls_check_only(tmp_path):
    wheel, sdist = make_pair(tmp_path, "0.2.23")
    work_dir = tmp_path / "work"
    rc = publish.main(
        [
            "verify",
            "--version",
            "0.2.23",
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


def test_verify_missing_asset_is_failure(tmp_path):
    wheel, sdist = make_pair(tmp_path, "0.2.23")
    sdist.unlink()  # the sdist asset is missing from the release
    rc = publish.main(
        [
            "verify",
            "--version",
            "0.2.23",
            "--wheel-url",
            wheel.as_uri(),
            "--sdist-url",
            (tmp_path / "sesskit-0.2.23.tar.gz").as_uri(),
            "--no-api-digest",
            "--check-only",
            "--work-dir",
            str(tmp_path / "work"),
        ]
    )
    assert rc == 2


def test_verify_hash_mismatch_is_failure(tmp_path):
    wheel, sdist = make_pair(tmp_path, "0.2.23")
    rc = publish.main(
        [
            "verify",
            "--version",
            "0.2.23",
            "--wheel-url",
            wheel.as_uri(),
            "--sdist-url",
            sdist.as_uri(),
            "--expected-wheel-sha256",
            "0" * 64,
            "--expected-sdist-sha256",
            sha256(sdist),
            "--check-only",
            "--work-dir",
            str(tmp_path / "work"),
        ]
    )
    assert rc == 2


def test_dry_run_is_side_effect_free(tmp_path, monkeypatch, capsys):
    before = {p.name for p in tmp_path.iterdir()}

    def _no_network(*args, **kwargs):
        raise AssertionError("dry-run must not touch the network")

    monkeypatch.setattr(publish.urllib.request, "urlopen", _no_network)
    rc = publish.main(["verify", "--version", "0.2.23", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "no upload performed" in out
    assert {p.name for p in tmp_path.iterdir()} == before


def test_check_dist_offline(monkeypatch, tmp_path):
    # check-dist must validate local artifacts with the network disabled:
    # proof of no network dependency on this route.
    make_pair(tmp_path, "0.2.23")

    def _no_network(*args, **kwargs):
        raise AssertionError("check-dist must work offline")

    monkeypatch.setattr(publish.urllib.request, "urlopen", _no_network)
    rc = publish.main(["check-dist", "--version", "0.2.23", "--dist", str(tmp_path)])
    assert rc == 0
    source = PUBLISH_SCRIPT.read_text()
    assert "mint-token" not in source


def test_check_dist_missing_file_is_failure(tmp_path, capsys):
    rc = publish.main(["check-dist", "--version", "0.2.23", "--dist", str(tmp_path)])
    assert rc == 2
    assert "missing artifact" in capsys.readouterr().err


def test_sdist_agreeing_pkginfo_copies_ok(tmp_path):
    wheel, _ = make_pair(tmp_path, "0.2.23")
    sdist = make_sdist(
        tmp_path / "sesskit-0.2.23.tar.gz",
        "0.2.23",
        extra_pkg_info=[("sesskit-0.2.23/src/sesskit.egg-info/PKG-INFO", "sesskit", "0.2.23")],
    )
    report = publish.check_dist_files(wheel, sdist, "0.2.23")
    assert report["sdist"] == "sesskit-0.2.23.tar.gz"


def test_sdist_contradictory_pkginfo_rejected(tmp_path):
    wheel, _ = make_pair(tmp_path, "0.2.23")
    sdist = make_sdist(
        tmp_path / "sesskit-0.2.23.tar.gz",
        "0.2.23",
        extra_pkg_info=[("sesskit-0.2.23/src/sesskit.egg-info/PKG-INFO", "sesskit", "0.2.22")],
    )
    with pytest.raises(publish.PublishError, match="contradictory PKG-INFO"):
        publish.check_dist_files(wheel, sdist, "0.2.23")


def test_non_utf8_wheel_metadata_is_clean_failure(tmp_path):
    dist_info = "sesskit-0.2.23.dist-info"
    wheel = tmp_path / "sesskit-0.2.23-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", b"Name: sesskit\nVersion: 0.2.23\n\xff\xfe\n")
        archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\n")
    sdist = make_sdist(tmp_path / "sesskit-0.2.23.tar.gz", "0.2.23")
    with pytest.raises(publish.PublishError, match="not valid UTF-8"):
        publish.check_dist_files(wheel, sdist, "0.2.23")


def test_non_utf8_sdist_metadata_is_clean_failure(tmp_path):
    wheel, _ = make_pair(tmp_path, "0.2.23")
    sdist = tmp_path / "sesskit-0.2.23.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        raw = b"Name: sesskit\nVersion: 0.2.23\n\xff\xfe\n"
        info = tarfile.TarInfo("sesskit-0.2.23/PKG-INFO")
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
    with pytest.raises(publish.PublishError, match="not valid UTF-8"):
        publish.check_dist_files(wheel, sdist, "0.2.23")


def test_verify_without_hash_evidence_fails_closed(tmp_path, capsys):
    wheel, sdist = make_pair(tmp_path, "0.2.23")
    rc = publish.main(
        [
            "verify",
            "--version",
            "0.2.23",
            "--wheel-url",
            wheel.as_uri(),
            "--sdist-url",
            sdist.as_uri(),
            "--no-api-digest",
            "--check-only",
            "--work-dir",
            str(tmp_path / "work"),
        ]
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "no expected sha256" in captured.err
    assert "verified sesskit" not in captured.out


def test_verify_untrusted_inspection_labels_output(tmp_path, capsys):
    wheel, sdist = make_pair(tmp_path, "0.2.23")
    rc = publish.main(
        [
            "verify",
            "--version",
            "0.2.23",
            "--wheel-url",
            wheel.as_uri(),
            "--sdist-url",
            sdist.as_uri(),
            "--no-api-digest",
            "--untrusted-inspection",
            "--check-only",
            "--work-dir",
            str(tmp_path / "work"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "UNTRUSTED" in out
    assert "verified sesskit" not in out


def test_verify_default_work_dir_cleaned_up(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    wheel, sdist = make_pair(tmp_path, "0.2.23")
    rc = publish.main(
        [
            "verify",
            "--version",
            "0.2.23",
            "--wheel-url",
            wheel.as_uri(),
            "--sdist-url",
            sdist.as_uri(),
            "--expected-wheel-sha256",
            sha256(wheel),
            "--expected-sdist-sha256",
            sha256(sdist),
            "--check-only",
        ]
    )
    assert rc == 0
    assert list(scratch.iterdir()) == []


def test_verify_failure_cleans_up_default_work_dir(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    wheel, sdist = make_pair(tmp_path, "0.2.23")
    rc = publish.main(
        [
            "verify",
            "--version",
            "0.2.23",
            "--wheel-url",
            wheel.as_uri(),
            "--sdist-url",
            sdist.as_uri(),
            "--expected-wheel-sha256",
            "0" * 64,
            "--expected-sdist-sha256",
            sha256(sdist),
            "--check-only",
        ]
    )
    assert rc == 2
    assert list(scratch.iterdir()) == []


def test_verify_requested_work_dir_retained(tmp_path):
    wheel, sdist = make_pair(tmp_path, "0.2.23")
    work_dir = tmp_path / "work"
    rc = publish.main(
        [
            "verify",
            "--version",
            "0.2.23",
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
    assert (work_dir / "sesskit-0.2.23-py3-none-any.whl").is_file()
    assert (work_dir / "sesskit-0.2.23.tar.gz").is_file()


def _run_cli(*argv: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(PUBLISH_SCRIPT), *argv], capture_output=True, check=False, text=True, timeout=120, env=env
    )


def test_cli_failures_have_no_traceback(tmp_path):
    wheel, sdist = make_pair(tmp_path, "0.2.23")
    corrupt_wheel = tmp_path / "corrupt" / "sesskit-0.2.23-py3-none-any.whl"
    corrupt_wheel.parent.mkdir()
    corrupt_wheel.write_bytes(b"not a zip")
    cases = [
        ["check-dist", "--version", "0.2.23", "--dist", str(tmp_path / "empty")],
        ["check-dist", "--version", "0.2.23", "--dist", str(corrupt_wheel.parent), "--sdist", sdist.name],
        ["verify", "--version", "bogus", "--dry-run"],
        [
            "verify",
            "--version",
            "0.2.23",
            "--wheel-url",
            wheel.as_uri(),
            "--sdist-url",
            sdist.as_uri(),
            "--no-api-digest",
            "--check-only",
            "--work-dir",
            str(tmp_path / "w"),
        ],
        ["clean-archive", "--version", "0.2.23", "--repo-root", str(tmp_path)],
        ["upload", "--version", "0.2.23", "--dist", str(tmp_path)],
    ]
    for argv in cases:
        proc = _run_cli(*argv)
        assert proc.returncode == 2, argv
        assert proc.stderr.startswith("error:"), argv
        assert "Traceback" not in proc.stderr, argv


def test_cli_unreadable_metadata_has_no_traceback(tmp_path):
    dist_info = "sesskit-0.2.23.dist-info"
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    wheel = bad_dir / "sesskit-0.2.23-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", b"Name: sesskit\nVersion: 0.2.23\n\xff\n")
        archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\n")
    sdist = make_sdist(bad_dir / "sesskit-0.2.23.tar.gz", "0.2.23")
    assert sdist.is_file()
    proc = _run_cli("check-dist", "--version", "0.2.23", "--dist", str(bad_dir))
    assert proc.returncode == 2
    assert proc.stderr.startswith("error:")
    assert "Traceback" not in proc.stderr


def test_upload_requires_explicit_confirmation(tmp_path, capsys):
    make_pair(tmp_path, "0.2.23")
    rc = publish.main(["upload", "--version", "0.2.23", "--dist", str(tmp_path)])
    assert rc == 2
    assert "confirm-upload" in capsys.readouterr().err


def test_upload_requires_env_credentials(tmp_path, monkeypatch, capsys):
    make_pair(tmp_path, "0.2.23")
    monkeypatch.delenv("TWINE_PASSWORD", raising=False)
    rc = publish.main(["upload", "--version", "0.2.23", "--dist", str(tmp_path), "--confirm-upload"])
    assert rc == 2
    assert "TWINE_PASSWORD" in capsys.readouterr().err


def test_upload_has_no_secret_options():
    parser = publish.build_parser()
    seen: set[str] = set()
    frontier: list = [parser]
    while frontier:
        current = frontier.pop()
        for act in current._actions:
            seen.update(getattr(act, "option_strings", []))
            choices = getattr(act, "choices", None)
            if isinstance(choices, dict):
                frontier.extend(choices.values())
    assert "--password" not in seen
    assert "--token" not in seen
    assert "--username" not in seen
    assert "--api-token" not in seen


def test_upload_missing_twine_is_clean_failure(tmp_path, monkeypatch, capsys):
    make_pair(tmp_path, "0.2.23")
    monkeypatch.setenv("TWINE_PASSWORD", "dummy-for-test-only")

    def _no_twine(*args, **kwargs):
        raise publish.PublishError("twine is not installed: stubbed for test")

    monkeypatch.setattr(publish, "find_twine", _no_twine)
    rc = publish.main(["upload", "--version", "0.2.23", "--dist", str(tmp_path), "--confirm-upload"])
    assert rc == 2
    assert "twine is not installed" in capsys.readouterr().err


def test_upload_drives_stub_twine_without_real_upload(tmp_path, monkeypatch, capsys):
    wheel, sdist = make_pair(tmp_path, "0.2.23")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_out = tmp_path / "stub-out"
    stub_out.mkdir()
    stub = bin_dir / "twine"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$STUB_OUT/argv"\n'
        'printenv TWINE_PASSWORD > "$STUB_OUT/password"\n'
        'printenv TWINE_USERNAME > "$STUB_OUT/username"\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("STUB_OUT", str(stub_out))
    monkeypatch.setenv("TWINE_USERNAME", "__token__")
    monkeypatch.setenv("TWINE_PASSWORD", "dummy-for-test-only")
    rc = publish.main(["upload", "--version", "0.2.23", "--dist", str(tmp_path), "--confirm-upload"])
    assert rc == 0
    assert "upload ok" in capsys.readouterr().out
    argv = (stub_out / "argv").read_text().splitlines()
    assert argv == [
        "upload",
        "--non-interactive",
        "--repository-url",
        publish.DEFAULT_UPLOAD_REPOSITORY,
        str(wheel),
        str(sdist),
    ]
    assert (stub_out / "password").read_text().strip() == "dummy-for-test-only"
    assert (stub_out / "username").read_text().strip() == "__token__"
    assert "dummy-for-test-only" not in "\n".join(argv)
