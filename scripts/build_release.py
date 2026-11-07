#!/usr/bin/env python3
"""Build native and compatibility artifacts from one accepted source tree.

Run from a clean committed-source archive, never a dirty checkout for delivery.
Maturin is the native/source backend; setuptools is invoked explicitly only for
our pure-Python compatibility wheel. There are no uploads or credential inputs.
"""

from __future__ import annotations

import argparse
import html
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def version() -> str:
    # This builder also runs on Python 3.10, where tomllib is not in stdlib.
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith('version = "'):
            return line.split('"')[1]
    raise RuntimeError("project version missing")


def run(*command: str, env: dict | None = None) -> None:
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def check_sdist(path: Path, release_version: str) -> None:
    """Reject build copies or internal coordination files in published source."""
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
    required = ("pyproject.toml", "Cargo.toml", "Cargo.lock",
                "crates/sesskit-python/src/lib.rs", "crates/sesskit-stats/src/projection.rs")
    for name in required:
        if f"sesskit-{release_version}/{name}" not in names:
            raise RuntimeError(f"source distribution is missing {name}")
    forbidden = [name for name in names if "/build/" in name or name.endswith("/TASKBOARD.md")]
    if forbidden:
        raise RuntimeError(f"source distribution contains internal/build files: {forbidden[:3]}")


def write_index(out: Path, release_version: str) -> Path:
    links = []
    for path in sorted(out.glob(f"sesskit-{release_version}-*.whl")):
        url = f"https://github.com/x0c/sesskit/releases/download/v{release_version}/{path.name}"
        links.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(path.name)}</a><br>')
    if not links:
        raise RuntimeError("no version-matched wheels to index")
    index = out / "wheels.html"
    index.write_text("<!doctype html><html><body>\n" + "\n".join(links) + "\n</body></html>\n", encoding="utf-8")
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--target")
    parser.add_argument("--zig", action="store_true")
    parser.add_argument("--pure", action="store_true", help="also build the explicit pure-Python fallback wheel")
    parser.add_argument("--sdist", action="store_true", help="also build a complete native-source distribution")
    parser.add_argument("--index-only", action="store_true", help="index already-built version-matched wheels")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    release_version = version()
    if not args.index_only:
        command = [sys.executable, "-m", "maturin", "build", "--release", "--locked", "--out", str(out)]
        if args.target:
            command.extend(["--target", args.target])
        if args.zig:
            command.extend(["--zig", "--compatibility", "manylinux_2_17"])
        run(*command)
        if args.pure:
            # Maturin develop can leave an ignored extension in src/. Refuse to
            # label that mixed tree as a universal pure-Python wheel.
            extensions = list((ROOT / "src" / "sesskit").glob("*.so"))
            extensions.extend((ROOT / "src" / "sesskit").glob("*.pyd"))
            if extensions:
                raise RuntimeError("pure wheel requires a clean source archive without development extensions")
            run(sys.executable, "-c", "from setuptools.build_meta import build_wheel; "
                f"build_wheel({str(out)!r})")
            pure = out / f"sesskit-{release_version}-py3-none-any.whl"
            with zipfile.ZipFile(pure) as archive:
                if any(name.endswith((".so", ".pyd", ".dylib")) for name in archive.namelist()):
                    raise RuntimeError("compatibility wheel unexpectedly contains native code")
        if args.sdist:
            run(sys.executable, "-m", "maturin", "sdist", "--out", str(out))
            check_sdist(out / f"sesskit-{release_version}.tar.gz", release_version)
    index = write_index(out, release_version)
    print(f"built/indexed sesskit {release_version}: {index}")


if __name__ == "__main__":
    main()
