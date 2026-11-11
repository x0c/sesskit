"""SessKit installation discovery and explicit, forward-only upgrades.

This module never reads or writes agent history. Network access is limited to
GitHub release pages; installation writes occur only when ``upgrade`` is called
with ``dry_run=False`` for a supported pip-managed environment.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import shlex
import site
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path

from sesskit import __version__

LATEST_RELEASE_URL = "https://github.com/x0c/sesskit/releases/latest"
EXPANDED_ASSETS_URL = "https://github.com/x0c/sesskit/releases/expanded_assets/{tag}"
NETWORK_TIMEOUT = 30
PIP_TIMEOUT = 600
MAX_RELEASE_PAGE_BYTES = 4 * 1024 * 1024
_VERSION_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


class UpgradeError(Exception):
    """A structured, recoverable installation-lifecycle failure."""

    def __init__(self, code: str, message: str, *, hint=None, next_commands=()):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.next_commands = list(next_commands)


@dataclass(frozen=True)
class Release:
    tag: str
    version: str
    find_links: str
    wheels: tuple[str, ...]


@dataclass(frozen=True)
class Installation:
    kind: str
    python: str
    prefix: str
    pip_args: tuple[str, ...] = ()


class _WheelLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for key, value in attrs:
            if key == "href" and value:
                self.hrefs.append(value)


def _cli_command(*args: str) -> str:
    executable = os.path.abspath(sys.argv[0])
    return shlex.join([executable, *args])


def _version_parts(value: str, *, allow_tag: bool = False) -> tuple[int, int, int]:
    candidate = value.strip()
    if allow_tag:
        candidate = candidate.removeprefix("v")
    match = _VERSION_RE.fullmatch(candidate)
    if match is None:
        raise UpgradeError(
            "version_error",
            f"unsupported SessKit version: {value!r} (want X.Y.Z)",
            hint="only final numeric releases are eligible for automatic upgrade",
        )
    return tuple(int(part) for part in match.groups())


def _fetch_page(url: str) -> tuple[str, str]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "text/html", "User-Agent": f"sesskit/{__version__}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
            final_url = response.geturl()
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read(MAX_RELEASE_PAGE_BYTES + 1)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise UpgradeError(
            "release_lookup_failed",
            f"GitHub release lookup failed: {exc}",
            hint="check network access and GitHub availability, then retry the read-only check",
            next_commands=[_cli_command("upgrade", "--dry-run")],
        ) from exc
    if len(raw) > MAX_RELEASE_PAGE_BYTES:
        raise UpgradeError(
            "release_format_changed",
            "GitHub release page exceeded the supported size",
            hint="use the documented fixed-release install route or a newer SessKit adapter",
        )
    if content_type != "text/html":
        raise UpgradeError(
            "release_format_changed",
            f"GitHub returned {content_type} instead of text/html for {url}",
            hint="use the expanded_assets HTML page, not a direct release-asset download",
        )
    try:
        text = raw.decode(charset)
    except (UnicodeDecodeError, LookupError) as exc:
        raise UpgradeError("release_format_changed", f"cannot decode GitHub release page: {exc}") from exc
    return final_url, text


def _release_tag(final_url: str) -> str:
    parts = urllib.parse.urlsplit(final_url)
    match = re.fullmatch(
        r"/x0c/sesskit/releases/tag/(v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))/?",
        parts.path,
    )
    if parts.scheme != "https" or parts.netloc != "github.com" or match is None:
        raise UpgradeError(
            "release_format_changed",
            f"GitHub latest release resolved to an unrecognized URL: {final_url}",
            hint="expected https://github.com/x0c/sesskit/releases/tag/vX.Y.Z",
        )
    return match.group(1)


def _wheel_names(page: str) -> tuple[str, ...]:
    parser = _WheelLinks()
    try:
        parser.feed(page)
    except Exception as exc:  # HTMLParser failures are upstream-format failures.
        raise UpgradeError("release_format_changed", f"cannot parse GitHub release assets: {exc}") from exc
    names = []
    for href in parser.hrefs:
        path = urllib.parse.urlsplit(href).path
        name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
        if name.endswith(".whl"):
            names.append(name)
    return tuple(names)


def discover_latest_release(fetch=None) -> Release:
    """Resolve GitHub's latest redirect and validate its versioned wheel page."""
    fetch = fetch or _fetch_page
    final_url, _ = fetch(LATEST_RELEASE_URL)
    tag = _release_tag(final_url)
    version = tag[1:]
    _version_parts(version)
    find_links = EXPANDED_ASSETS_URL.format(tag=tag)
    _, assets_page = fetch(find_links)
    wheels = _wheel_names(assets_page)
    if not any(name.startswith(f"sesskit-{version}-") for name in wheels):
        raise UpgradeError(
            "release_not_ready",
            f"release {tag} has no matching SessKit wheel yet",
            hint="the release assets may still be uploading; retry the read-only check later",
            next_commands=[_cli_command("upgrade", "--dry-run")],
        )
    return Release(tag=tag, version=version, find_links=find_links, wheels=wheels)


def _distribution_root() -> Path | None:
    try:
        root = importlib.metadata.distribution("sesskit").locate_file("")
    except importlib.metadata.PackageNotFoundError:
        return None
    return Path(str(root)).resolve()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def detect_installation() -> Installation:
    """Return the pip target that owns this process, or refuse foreign ownership."""
    prefix = Path(sys.prefix).resolve()
    base_prefix = Path(sys.base_prefix).resolve()
    root = _distribution_root()
    if prefix != base_prefix:
        lowered = str(prefix).lower()
        kind = "pipx" if "pipx" in lowered else "uv-tool" if "/uv/tools/" in lowered else "venv"
        installation = Installation(kind=kind, python=sys.executable, prefix=str(prefix))
    else:
        user_site = Path(site.getusersitepackages()).resolve()
        if root is None or not _inside(root, user_site):
            next_commands = []
            hint = "use the package manager that owns this installation"
            if "homebrew" in str(prefix).lower() or str(prefix).startswith("/usr/local"):
                hint = "this is a Homebrew-owned installation; upgrade it through Homebrew"
                next_commands = ["brew update && brew upgrade sesskit"]
            raise UpgradeError(
                "unsupported_installation",
                f"cannot safely self-upgrade installation owned by {prefix}",
                hint=hint + "; supported self-upgrades are venv, pipx, uv-tool, and pip --user",
                next_commands=next_commands,
            )
        installation = Installation(
            kind="pip-user", python=sys.executable, prefix=str(prefix), pip_args=("--user",),
        )
    target = root if root is not None else Path(sys.prefix)
    if not os.access(target, os.W_OK):
        raise UpgradeError(
            "installation_not_writable",
            f"SessKit installation is not writable: {target}",
            hint="use the owning package manager or install SessKit in a user-writable virtual environment",
        )
    return installation


def _pip_command(installation: Installation, release: Release) -> list[str]:
    return [
        installation.python,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--only-binary",
        ":all:",
        *installation.pip_args,
        "--no-index",
        "--find-links",
        release.find_links,
        f"sesskit=={release.version}",
    ]


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def _pip_failure(result: subprocess.CompletedProcess, command: list[str]) -> UpgradeError:
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    detail = "\n".join(output.splitlines()[-12:])
    message = f"pip upgrade failed with exit code {result.returncode}"
    if detail:
        message += f":\n{detail}"
    return UpgradeError(
        "upgrade_failed",
        message,
        hint="the previous installation was not verified as replaced; inspect the pip error before retrying",
        next_commands=[shlex.join(command)],
    )


def _verify_installed(installation: Installation, expected: str) -> str:
    command = [
        installation.python,
        "-c",
        "from importlib.metadata import version; print(version('sesskit'))",
    ]
    try:
        result = _run(command, NETWORK_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpgradeError("verification_failed", f"cannot verify upgraded SessKit: {exc}") from exc
    installed = result.stdout.strip()
    if result.returncode != 0 or installed != expected:
        detail = result.stderr.strip() or installed or "no version returned"
        raise UpgradeError(
            "verification_failed",
            f"post-upgrade version mismatch: expected {expected}, got {detail}",
            hint="do not assume the upgrade succeeded; inspect the active Python installation",
            next_commands=[shlex.join(command)],
        )
    return installed


def _installed_version() -> str:
    """Read package metadata on every call, including after an in-process upgrade."""
    try:
        return importlib.metadata.version("sesskit")
    except importlib.metadata.PackageNotFoundError:
        return __version__


def upgrade(*, dry_run: bool = False, current_version: str | None = None, installation=None) -> dict:
    """Check or perform one explicit upgrade, returning JSON-serializable facts."""
    installation = installation or detect_installation()
    current_version = current_version or _installed_version()
    _version_parts(current_version)
    release = discover_latest_release()
    current_key = _version_parts(current_version)
    latest_key = _version_parts(release.version)
    status = "update_available" if latest_key > current_key else "ahead" if latest_key < current_key else "unchanged"
    command = _pip_command(installation, release) if status == "update_available" else None
    result = {
        "status": status,
        "previous_version": current_version,
        "current_version": current_version,
        "latest_version": release.version,
        "release_tag": release.tag,
        "dry_run": dry_run,
        "verified": False,
        "install": asdict(installation),
        "release_page": LATEST_RELEASE_URL,
        "find_links": release.find_links,
        "command": command,
    }
    if dry_run or status != "update_available":
        return result
    try:
        completed = _run(command, PIP_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise UpgradeError(
            "upgrade_failed",
            f"pip upgrade timed out after {PIP_TIMEOUT} seconds; outcome is unverified",
            hint="inspect the active package version before deciding whether to retry",
            next_commands=[_cli_command("version")],
        ) from exc
    except OSError as exc:
        raise UpgradeError("upgrade_failed", f"cannot execute pip: {exc}") from exc
    if completed.returncode != 0:
        raise _pip_failure(completed, command)
    installed = _verify_installed(installation, release.version)
    result.update(status="updated", current_version=installed, verified=True)
    return result
