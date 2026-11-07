"""Native-wheel discovery must keep the explicit Python compatibility path."""

from __future__ import annotations

import importlib.util
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_release", ROOT / "scripts" / "build_release.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
publish_spec = importlib.util.spec_from_file_location("publish_native", ROOT / "scripts" / "publish.py")
publisher = importlib.util.module_from_spec(publish_spec)
publish_spec.loader.exec_module(publisher)


def test_release_index_is_exact_version_and_supports_native_and_fallback(tmp_path):
    for name in ("sesskit-0.3.0-py3-none-any.whl", "sesskit-0.3.0-cp310-abi3-macosx_11_0_arm64.whl",
                 "sesskit-0.2.25-py3-none-any.whl", "SHA256SUMS"):
        (tmp_path / name).write_bytes(b"fixture")
    index = builder.write_index(tmp_path, "0.3.0").read_text(encoding="utf-8")
    assert index.count("<a href=") == 2
    assert "0.2.25" not in index
    assert "SHA256SUMS" not in index
    assert 'href="https://github.com/x0c/sesskit/releases/download/v0.3.0/' in index
    assert "cp310-abi3" in index and "py3-none-any" in index


def test_cargo_and_python_versions_match():
    import sesskit

    assert builder.version() == sesskit.__version__
    cargo = (ROOT / "Cargo.toml").read_text(encoding="utf-8")
    assert f'version = "{sesskit.__version__}"' in cargo
    assert '"abi3-py310"' in cargo


def test_source_archive_rejects_internal_coordination_and_build_copies(tmp_path):
    source = tmp_path / "sesskit-0.3.0.tar.gz"
    required = ("pyproject.toml", "Cargo.toml", "Cargo.lock",
                "crates/sesskit-python/src/lib.rs", "crates/sesskit-stats/src/projection.rs")
    with tarfile.open(source, "w:gz") as archive:
        for name in (*required, "TASKBOARD.md", "build/lib/sesskit/cli.py"):
            member = tarfile.TarInfo(f"sesskit-0.3.0/{name}")
            member.size = 0
            archive.addfile(member)
    with pytest.raises(RuntimeError, match="internal/build"):
        builder.check_sdist(source, "0.3.0")
    with tarfile.open(source, "w:gz") as archive:
        for name in required:
            member = tarfile.TarInfo(f"sesskit-0.3.0/{name}")
            member.size = 0
            archive.addfile(member)
    builder.check_sdist(source, "0.3.0")


@pytest.mark.parametrize("fault", [None, "missing", "tag", "pure"])
def test_native_artifact_gate_checks_contents_and_filename_tags(tmp_path, fault):
    path = tmp_path / "sesskit-0.3.0-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
    with zipfile.ZipFile(path, "w") as archive:
        tags = ["cp310-abi3-manylinux_2_17_x86_64", "cp310-abi3-manylinux2014_x86_64"]
        if fault == "tag":
            tags = ["cp310-abi3-win_amd64"]
        metadata = f"Wheel-Version: 1.0\nRoot-Is-Purelib: {'true' if fault == 'pure' else 'false'}\n"
        metadata += "".join(f"Tag: {tag}\n" for tag in tags)
        archive.writestr("sesskit-0.3.0.dist-info/WHEEL", metadata)
        if fault != "missing":
            archive.writestr("sesskit/_native.abi3.so", b"synthetic binary")
    if fault is None:
        publisher.check_native_wheel(path)
    else:
        with pytest.raises(publisher.PublishError):
            publisher.check_native_wheel(path)
