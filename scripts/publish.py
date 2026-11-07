#!/usr/bin/env python3
"""SessKit release-artifact helper (read-only except an explicit upload).

This script never builds from or publishes a dirty working tree:

- ``verify`` downloads the immutable wheel+sdist attached to an already
  published GitHub release, then checks filenames, version, sha256 digests,
  embedded metadata, and (unless ``--check-only``) a clean install into a
  throwaway virtual environment.
- ``check-dist`` validates local wheel+sdist files (the workflow's
  pre-publish gate) without any network or upload.
- ``clean-archive`` exports a clean ``git archive`` of an exact release tag
  into a separate directory. It reads committed objects only and never
  overwrites, hides, or moves the working tree.
- ``upload`` publishes validated local artifacts with twine. It requires the
  explicit ``--confirm-upload`` flag and takes credentials only from the
  environment (``TWINE_USERNAME``/``TWINE_PASSWORD``); there are no password,
  token, or username options, so secrets can never appear on argv.

Uploads happen only through ``upload`` (local, explicit) or the GitHub
Actions workflow (``.github/workflows/publish-pypi.yml``) via PyPI Trusted
Publishing (OIDC, no stored API token).

Exit codes: 0 success, 2 any validation/usage failure. All known failure
modes (missing files, unreadable metadata, bad archives, hash mismatches)
report as ``error: ...`` on stderr; tracebacks indicate unexpected bugs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from email.parser import Parser
from itertools import product
from pathlib import Path

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
WHEEL_NAME = "sesskit-{version}-py3-none-any.whl"
SDIST_NAME = "sesskit-{version}.tar.gz"
GITHUB_RELEASE_BASE = "https://github.com/{repo}/releases/download/v{version}"
GITHUB_API_RELEASE = "https://api.github.com/repos/{repo}/releases/tags/v{version}"
DEFAULT_UPLOAD_REPOSITORY = "https://upload.pypi.org/legacy/"
NETWORK_TIMEOUT = 60
SUBPROCESS_TIMEOUT = 600


class PublishError(Exception):
    """Fatal validation failure (exit code 2)."""


def parse_version(value: str) -> str:
    version = value.strip().lstrip("v")
    if not VERSION_RE.match(version):
        raise PublishError(f"refusing unparseable version: {value!r} (want X.Y.Z)")
    return version


def release_asset_urls(repo: str, version: str) -> tuple[str, str]:
    base = GITHUB_RELEASE_BASE.format(repo=repo, version=version)
    wheel = WHEEL_NAME.format(version=version)
    sdist = SDIST_NAME.format(version=version)
    return f"{base}/{wheel}", f"{base}/{sdist}"


def download(url: str, dest: Path) -> Path:
    try:
        with urllib.request.urlopen(url, timeout=NETWORK_TIMEOUT) as response, open(dest, "wb") as handle:
            shutil.copyfileobj(response, handle)
    except Exception as exc:  # network, HTTP, or filesystem failure
        raise PublishError(f"download failed for {url}: {exc}") from exc
    if dest.stat().st_size == 0:
        raise PublishError(f"downloaded empty file from {url}")
    return dest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PublishError(f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def check_sha256(path: Path, expected: str, label: str) -> str:
    actual = sha256_file(path)
    want = expected.lower().removeprefix("sha256:")
    if actual.lower() != want:
        raise PublishError(f"{label} sha256 mismatch for {path.name}: expected {expected}, got {actual}")
    return actual


def fetch_github_digests(repo: str, version: str) -> dict[str, str]:
    """Read-only lookup of the asset digests recorded by the GitHub API."""
    url = GITHUB_API_RELEASE.format(repo=repo, version=version)
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
            payload = json.load(response)
    except Exception as exc:
        raise PublishError(f"GitHub API lookup failed for {url}: {exc}") from exc
    digests: dict[str, str] = {}
    for asset in payload.get("assets", []):
        name = asset.get("name", "")
        digest = asset.get("digest", "")
        if name and digest.startswith("sha256:"):
            digests[name] = digest.split(":", 1)[1]
    return digests


def _metadata_fields(raw: bytes, label: str) -> tuple[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublishError(f"{label}: metadata is not valid UTF-8: {exc}") from exc
    fields = Parser().parsestr(text)
    return fields.get("Name", ""), fields.get("Version", "")


def wheel_metadata_version(wheel_path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            metadata_names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise PublishError(
                    f"{wheel_path.name}: want exactly one .dist-info/METADATA, found {len(metadata_names)}"
                )
            raw = archive.read(metadata_names[0])
    except zipfile.BadZipFile as exc:
        raise PublishError(f"{wheel_path.name}: not a readable wheel: {exc}") from exc
    except OSError as exc:
        raise PublishError(f"{wheel_path.name}: cannot read file: {exc}") from exc
    return _metadata_fields(raw, f"wheel {wheel_path.name}")


def sdist_metadata_version(sdist_path: Path, version: str) -> tuple[str, str, str]:
    """Return (name, version, top-level directory) from the sdist.

    The canonical member ``sesskit-<version>/PKG-INFO`` must exist as a
    regular file; an egg-info-only archive is rejected. Every other PKG-INFO
    copy in the archive must agree with it; contradictory, duplicate, or
    unsafe (absolute/parent-traversing) metadata paths are rejected instead
    of silently trusting the first copy found.
    """
    canonical = f"sesskit-{version}/PKG-INFO"
    try:
        with tarfile.open(sdist_path, "r:gz") as archive:
            members = archive.getmembers()
            pkg_infos = [m for m in members if m.name.endswith("PKG-INFO")]
            if not pkg_infos:
                raise PublishError(f"{sdist_path.name}: no PKG-INFO found in sdist")
            for member in pkg_infos:
                if not member.name or member.name.startswith("/") or ".." in member.name.split("/"):
                    raise PublishError(f"{sdist_path.name}: unsafe metadata path {member.name!r}")
            arc_names = [m.name for m in pkg_infos]
            if len(set(arc_names)) != len(arc_names):
                raise PublishError(f"{sdist_path.name}: duplicate PKG-INFO paths in sdist")
            canonical_members = [m for m in pkg_infos if m.name == canonical]
            if not canonical_members:
                raise PublishError(f"{sdist_path.name}: no top-level {canonical} in sdist")
            if not canonical_members[0].isreg():
                raise PublishError(f"{sdist_path.name}: {canonical} is not a regular file")
            seen: dict[tuple[str, str], str] = {}
            for member in pkg_infos:
                handle = archive.extractfile(member)
                if handle is None:
                    raise PublishError(f"{sdist_path.name}: cannot read {member.name}")
                try:
                    raw = handle.read()
                except (OSError, EOFError) as exc:
                    raise PublishError(f"{sdist_path.name}: cannot read {member.name}: {exc}") from exc
                seen[_metadata_fields(raw, f"sdist {sdist_path.name}")] = member.name
            if len(seen) > 1:
                detail = ", ".join(
                    f"{name or '?'} {found or '?'} in {loc}"
                    for (name, found), loc in sorted(seen.items(), key=lambda kv: kv[1])
                )
                raise PublishError(f"{sdist_path.name}: contradictory PKG-INFO copies: {detail}")
            (name, found_version), _ = seen.popitem()
    except tarfile.TarError as exc:
        raise PublishError(f"{sdist_path.name}: not a readable sdist: {exc}") from exc
    except OSError as exc:
        raise PublishError(f"{sdist_path.name}: cannot read file: {exc}") from exc
    return name, found_version, f"sesskit-{version}"


def check_metadata(name: str, version: str, want_version: str, label: str) -> None:
    if name != "sesskit":
        raise PublishError(f"{label}: distribution name is {name!r}, want 'sesskit'")
    if version != want_version:
        raise PublishError(f"{label}: embedded version is {version!r}, want {want_version!r}")


def check_native_wheel(wheel_path: Path) -> None:
    """Require native contents and WHEEL tags matching the advertised filename."""
    parts = wheel_path.name.removesuffix(".whl").split("-")
    expected_tags = {"-".join(tag) for tag in product(*(part.split(".") for part in parts[-3:]))}
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            names = archive.namelist()
            metadata = [name for name in names if name.endswith(".dist-info/WHEEL")]
            if len(metadata) != 1:
                raise PublishError(f"{wheel_path.name}: want exactly one WHEEL metadata file")
            fields = Parser().parsestr(archive.read(metadata[0]).decode("utf-8"))
            if fields.get("Root-Is-Purelib", "").lower() != "false":
                raise PublishError(f"{wheel_path.name}: native wheel is mislabeled as pure Python")
            if not expected_tags.issubset(set(fields.get_all("Tag", []))):
                raise PublishError(f"{wheel_path.name}: filename and WHEEL tags disagree")
            if not any(name.startswith("sesskit/_native") and name.endswith((".so", ".pyd")) for name in names):
                raise PublishError(f"{wheel_path.name}: native extension is missing")
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise PublishError(f"{wheel_path.name}: cannot inspect native wheel: {exc}") from exc


def check_dist_files(wheel_path: Path, sdist_path: Path, version: str) -> dict[str, str]:
    """Validate a local wheel+sdist pair. No network, no side effects."""
    wheel_pattern = rf"sesskit-{re.escape(version)}-(?:[0-9][A-Za-z0-9_]*-)?[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\.whl"
    if re.fullmatch(wheel_pattern, wheel_path.name) is None:
        raise PublishError(f"wheel filename {wheel_path.name!r} does not match version {version}")
    if sdist_path.name != SDIST_NAME.format(version=version):
        raise PublishError(f"sdist filename {sdist_path.name!r} does not match version {version}")
    wheel_name, wheel_version = wheel_metadata_version(wheel_path)
    check_metadata(wheel_name, wheel_version, version, f"wheel {wheel_path.name}")
    if wheel_path.name != WHEEL_NAME.format(version=version):
        check_native_wheel(wheel_path)
    sdist_name, sdist_version, top_dir = sdist_metadata_version(sdist_path, version)
    check_metadata(sdist_name, sdist_version, version, f"sdist {sdist_path.name}")
    want_top = f"sesskit-{version}"
    if top_dir != want_top:
        raise PublishError(f"sdist top-level directory is {top_dir!r}, want {want_top!r}")
    return {
        "wheel": wheel_path.name,
        "wheel_sha256": sha256_file(wheel_path),
        "sdist": sdist_path.name,
        "sdist_sha256": sha256_file(sdist_path),
    }


def clean_install_smoke(wheel_path: Path, version: str, work_dir: Path) -> None:
    """Install the wheel into a throwaway venv and import it. No uploads."""
    venv_dir = work_dir / "smoke-venv"
    proc = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True,
        check=False,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    if proc.returncode != 0:
        raise PublishError(f"venv creation failed: {proc.stderr.strip()}")
    venv_python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
    uv = shutil.which("uv")
    if uv is not None:
        install_cmd = [uv, "pip", "install", "--python", str(venv_python), str(wheel_path)]
    else:
        install_cmd = [str(venv_python), "-m", "pip", "install", str(wheel_path)]
    proc = subprocess.run(install_cmd, capture_output=True, check=False, text=True, timeout=SUBPROCESS_TIMEOUT)
    if proc.returncode != 0:
        raise PublishError(f"clean install failed: {proc.stderr.strip()[-2000:]}")
    native = wheel_path.name != WHEEL_NAME.format(version=version)
    check = (
        "import sesskit; "
        "from sesskit.native_projection import native_available; "
        f"assert sesskit.__version__ == {version!r}, sesskit.__version__; "
        f"assert native_available() is {native!r}, native_available(); "
        "print('smoke-ok', sesskit.__version__, 'native', native_available())"
    )
    proc = subprocess.run(
        [str(venv_python), "-c", check], capture_output=True, check=False, text=True, timeout=SUBPROCESS_TIMEOUT
    )
    if proc.returncode != 0 or "smoke-ok" not in proc.stdout:
        raise PublishError(f"installed wheel failed smoke import: {proc.stderr.strip()[-2000:]}")


def run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=str(repo_root), capture_output=True, check=False, text=True, timeout=SUBPROCESS_TIMEOUT
        )
    except OSError as exc:
        raise PublishError(f"git is not available: {exc}") from exc


def git_tree_state(repo_root: Path) -> str:
    proc = run_git(repo_root, "status", "--porcelain")
    if proc.returncode != 0:
        raise PublishError(f"git status failed in {repo_root}: {proc.stderr.strip()}")
    return proc.stdout


def resolve_expected_hashes(args: argparse.Namespace, version: str) -> tuple[str | None, str | None]:
    expected_wheel = args.expected_wheel_sha256
    expected_sdist = args.expected_sdist_sha256
    if not args.no_api_digest and not (expected_wheel and expected_sdist):
        digests = fetch_github_digests(args.repo, version)
        expected_wheel = expected_wheel or digests.get(WHEEL_NAME.format(version=version))
        expected_sdist = expected_sdist or digests.get(SDIST_NAME.format(version=version))
    return expected_wheel, expected_sdist


def cmd_verify(args: argparse.Namespace) -> int:
    version = parse_version(args.version)
    if args.dry_run:
        wheel_url, sdist_url = (
            (args.wheel_url, args.sdist_url)
            if args.wheel_url and args.sdist_url
            else release_asset_urls(args.repo, version)
        )
        print(f"dry-run: would verify sesskit {version}")
        print(f"dry-run: wheel {wheel_url}")
        print(f"dry-run: sdist {sdist_url}")
        print(f"dry-run: check-only={args.check_only} api-digest={not args.no_api_digest}")
        print("dry-run: no download, no install, no upload performed")
        return 0
    wheel_url = args.wheel_url
    sdist_url = args.sdist_url
    if not wheel_url or not sdist_url:
        default_wheel, default_sdist = release_asset_urls(args.repo, version)
        wheel_url = wheel_url or default_wheel
        sdist_url = sdist_url or default_sdist
    expected_wheel, expected_sdist = resolve_expected_hashes(args, version)
    trusted = bool(expected_wheel and expected_sdist)
    if not trusted and not args.untrusted_inspection:
        raise PublishError(
            "no expected sha256 for wheel/sdist and no API digest: refusing to print 'verified' without hash evidence; pass --expected-*-sha256, drop --no-api-digest, or pass --untrusted-inspection for an explicitly untrusted inspection"
        )
    if args.work_dir:
        work_dir = Path(args.work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        return _verify_into(args, version, wheel_url, sdist_url, expected_wheel, expected_sdist, trusted, work_dir)
    with tempfile.TemporaryDirectory(prefix="sesskit-publish-") as tmp:
        return _verify_into(args, version, wheel_url, sdist_url, expected_wheel, expected_sdist, trusted, Path(tmp))


def _verify_into(
    args: argparse.Namespace,
    version: str,
    wheel_url: str,
    sdist_url: str,
    expected_wheel: str | None,
    expected_sdist: str | None,
    trusted: bool,
    work_dir: Path,
) -> int:
    wheel_path = download(wheel_url, work_dir / wheel_url.rsplit("/", 1)[-1])
    sdist_path = download(sdist_url, work_dir / sdist_url.rsplit("/", 1)[-1])
    if expected_wheel:
        check_sha256(wheel_path, expected_wheel, "wheel")
    if expected_sdist:
        check_sha256(sdist_path, expected_sdist, "sdist")
    report = check_dist_files(wheel_path, sdist_path, version)
    if trusted:
        print(f"verified sesskit {version} from {args.repo}")
    else:
        print(f"inspected sesskit {version} from {args.repo} (UNTRUSTED: no sha256 evidence; do not consume)")
    for key, value in report.items():
        print(f"  {key}: {value}")
    if not args.check_only:
        clean_install_smoke(wheel_path, version, work_dir)
        print("  clean-install smoke: ok")
    else:
        print("  clean-install smoke: skipped (--check-only)")
    return 0


def _local_artifact_paths(dist_dir: Path, version: str, wheel: str | None, sdist: str | None) -> tuple[Path, Path]:
    wheel_path = dist_dir / (wheel or WHEEL_NAME.format(version=version))
    sdist_path = dist_dir / (sdist or SDIST_NAME.format(version=version))
    for path in (wheel_path, sdist_path):
        if not path.is_file():
            raise PublishError(f"missing artifact: {path}")
    return wheel_path, sdist_path


def cmd_check_dist(args: argparse.Namespace) -> int:
    version = parse_version(args.version)
    wheel_path, sdist_path = _local_artifact_paths(Path(args.dist), version, args.wheel, args.sdist)
    report = check_dist_files(wheel_path, sdist_path, version)
    if args.expected_wheel_sha256:
        check_sha256(wheel_path, args.expected_wheel_sha256, "wheel")
    if args.expected_sdist_sha256:
        check_sha256(sdist_path, args.expected_sdist_sha256, "sdist")
    print(f"local dist ok for sesskit {version} in {args.dist}")
    for key, value in report.items():
        print(f"  {key}: {value}")
    return 0


def cmd_clean_archive(args: argparse.Namespace) -> int:
    version = parse_version(args.version)
    repo_root = Path(args.repo_root).resolve()
    tag = f"v{version}"
    proc = run_git(repo_root, "rev-parse", "--verify", f"refs/tags/{tag}")
    if proc.returncode != 0:
        raise PublishError(f"tag {tag} does not exist in {repo_root}")
    if args.out:
        out_dir = Path(args.out).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        return _archive_into(repo_root, tag, version, out_dir, retain=True)
    with tempfile.TemporaryDirectory(prefix=f"sesskit-{version}-archive-") as tmp:
        return _archive_into(repo_root, tag, version, Path(tmp), retain=False)


def _archive_into(repo_root: Path, tag: str, version: str, out_dir: Path, retain: bool) -> int:
    archive_path = out_dir / f"sesskit-{version}-git-archive.tar.gz"
    # Committed tag objects only: the working tree (dirty or not) is never read.
    proc = run_git(repo_root, "archive", "--format=tar.gz", f"--output={archive_path}", tag)
    if proc.returncode != 0 or not archive_path.is_file():
        raise PublishError(f"git archive failed for {tag}: {proc.stderr.strip()}")
    state = git_tree_state(repo_root)
    if state.strip():
        print(f"note: working tree has uncommitted changes; archive is from {tag} only")
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            names = archive.getnames()
            pyproject = [n for n in names if n.rsplit("/", 1)[-1] == "pyproject.toml"]
            if not pyproject:
                raise PublishError(f"archive of {tag} has no pyproject.toml")
            handle = archive.extractfile(pyproject[0])
            if handle is None:
                raise PublishError(f"cannot read archived pyproject.toml from {tag}")
            try:
                text = handle.read().decode("utf-8")
            except (UnicodeDecodeError, OSError, EOFError) as exc:
                raise PublishError(f"cannot read archived pyproject.toml from {tag}: {exc}") from exc
            match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if not match or match.group(1) != version:
                found = match.group(1) if match else "<none>"
                raise PublishError(f"archived pyproject version is {found!r}, want {version!r}")
    except tarfile.TarError as exc:
        raise PublishError(f"archive of {tag} is unreadable: {exc}") from exc
    except OSError as exc:
        raise PublishError(f"archive of {tag} is unreadable: {exc}") from exc
    size = archive_path.stat().st_size
    if retain:
        print(f"clean archive ok: {archive_path} ({size} bytes from {tag})")
    else:
        print(f"clean archive verified for {tag} ({size} bytes; temporary output removed, pass --out to retain)")
    return 0


def find_twine() -> list[str]:
    twine = shutil.which("twine")
    if twine is not None:
        return [twine]
    proc = subprocess.run(
        [sys.executable, "-m", "twine", "--version"],
        capture_output=True,
        check=False,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    if proc.returncode == 0:
        return [sys.executable, "-m", "twine"]
    raise PublishError("twine is not installed: run `python -m pip install twine` (or `uv tool install twine`) first")


def cmd_upload(args: argparse.Namespace) -> int:
    version = parse_version(args.version)
    if not args.confirm_upload:
        raise PublishError("refusing to upload without --confirm-upload")
    wheel_path, sdist_path = _local_artifact_paths(Path(args.dist), version, args.wheel, args.sdist)
    # Validate before anything leaves this machine.
    report = check_dist_files(wheel_path, sdist_path, version)
    if args.expected_wheel_sha256:
        check_sha256(wheel_path, args.expected_wheel_sha256, "wheel")
    if args.expected_sdist_sha256:
        check_sha256(sdist_path, args.expected_sdist_sha256, "sdist")
    if not os.environ.get("TWINE_PASSWORD"):
        raise PublishError(
            "refusing to upload without TWINE_PASSWORD in the environment; "
            "load TWINE_USERNAME/TWINE_PASSWORD from your secret store "
            "(never pass secrets on argv: this command has no password/token options)"
        )
    twine = find_twine()
    cmd = [
        *twine,
        "upload",
        "--non-interactive",
        "--repository-url",
        args.repository_url,
        str(wheel_path),
        str(sdist_path),
    ]
    print(f"uploading sesskit {version} to {args.repository_url}")
    for key, value in report.items():
        print(f"  {key}: {value}")
    proc = subprocess.run(cmd, capture_output=True, check=False, text=True, timeout=SUBPROCESS_TIMEOUT)
    if proc.returncode != 0:
        raise PublishError(f"twine upload failed: {proc.stderr.strip()[-2000:]}")
    print(f"upload ok: sesskit {version}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify SessKit release artifacts; uploads only via explicit `upload`."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="verify published GitHub release assets")
    verify.add_argument("--version", required=True, help="release version X.Y.Z")
    verify.add_argument("--repo", default="x0c/sesskit")
    verify.add_argument("--wheel-url", default=None)
    verify.add_argument("--sdist-url", default=None)
    verify.add_argument("--expected-wheel-sha256", default=None)
    verify.add_argument("--expected-sdist-sha256", default=None)
    verify.add_argument("--no-api-digest", action="store_true", help="do not query the GitHub API for asset digests")
    verify.add_argument(
        "--untrusted-inspection",
        action="store_true",
        help="proceed without hash evidence; output is labeled UNTRUSTED, never 'verified'",
    )
    verify.add_argument("--check-only", action="store_true")
    verify.add_argument("--dry-run", action="store_true")
    verify.add_argument(
        "--work-dir",
        default=None,
        help="retain downloads here; default is a temporary directory that is always cleaned up",
    )
    verify.set_defaults(func=cmd_verify)

    check = sub.add_parser("check-dist", help="validate local wheel+sdist files")
    check.add_argument("--version", required=True)
    check.add_argument("--dist", required=True)
    check.add_argument("--wheel", default=None)
    check.add_argument("--sdist", default=None)
    check.add_argument("--expected-wheel-sha256", default=None)
    check.add_argument("--expected-sdist-sha256", default=None)
    check.set_defaults(func=cmd_check_dist)

    archive = sub.add_parser("clean-archive", help="export a clean git archive of a tag")
    archive.add_argument("--version", required=True)
    archive.add_argument("--repo-root", default=".")
    archive.add_argument(
        "--out",
        default=None,
        help="retain the archive here; default is a temporary directory that is always cleaned up",
    )
    archive.set_defaults(func=cmd_clean_archive)

    upload = sub.add_parser("upload", help="upload validated local artifacts with twine (explicit only)")
    upload.add_argument("--version", required=True)
    upload.add_argument("--dist", required=True)
    upload.add_argument("--wheel", default=None)
    upload.add_argument("--sdist", default=None)
    upload.add_argument("--repository-url", default=DEFAULT_UPLOAD_REPOSITORY)
    upload.add_argument("--expected-wheel-sha256", default=None)
    upload.add_argument("--expected-sdist-sha256", default=None)
    upload.add_argument(
        "--confirm-upload",
        action="store_true",
        help="required explicit opt-in; without it the command refuses to upload",
    )
    upload.set_defaults(func=cmd_upload)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.func(args)
    except PublishError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
