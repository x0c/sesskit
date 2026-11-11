"""Installation lifecycle tests; network and subprocesses are always mocked."""

from __future__ import annotations

import subprocess
from email.message import Message
from pathlib import Path
from unittest import mock

import pytest

from sesskit import lifecycle


def _release(version="0.3.2"):
    return lifecycle.Release(
        tag=f"v{version}",
        version=version,
        find_links=f"https://github.com/x0c/sesskit/releases/expanded_assets/v{version}",
        wheels=(f"sesskit-{version}-py3-none-any.whl",),
    )


def _installation():
    return lifecycle.Installation(kind="venv", python="/tmp/venv/bin/python", prefix="/tmp/venv")


def _completed(command, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, code, stdout=stdout, stderr=stderr)


def test_discover_latest_release_uses_redirect_and_expanded_assets():
    calls = []

    def fetch(url):
        calls.append(url)
        if url == lifecycle.LATEST_RELEASE_URL:
            return "https://github.com/x0c/sesskit/releases/tag/v0.3.2", ""
        return url, '<a href="/x0c/sesskit/releases/download/v0.3.2/sesskit-0.3.2-py3-none-any.whl">wheel</a>'

    release = lifecycle.discover_latest_release(fetch=fetch)
    assert release.tag == "v0.3.2"
    assert release.version == "0.3.2"
    assert release.wheels == ("sesskit-0.3.2-py3-none-any.whl",)
    assert calls == [lifecycle.LATEST_RELEASE_URL, release.find_links]


@pytest.mark.parametrize("url", [
    "https://github.com/x0c/sesskit/releases/latest",
    "https://evil.example/x0c/sesskit/releases/tag/v0.3.2",
    "https://github.com/x0c/sesskit/releases/tag/0.3.2",
    "https://github.com/x0c/sesskit/releases/tag/v0.3.2.1",
])
def test_discover_rejects_unrecognized_latest_target(url):
    with pytest.raises(lifecycle.UpgradeError) as caught:
        lifecycle.discover_latest_release(fetch=lambda _url: (url, ""))
    assert caught.value.code == "release_format_changed"


def test_release_without_matching_wheel_is_not_ready():
    def fetch(url):
        if url == lifecycle.LATEST_RELEASE_URL:
            return "https://github.com/x0c/sesskit/releases/tag/v0.3.2", ""
        return url, '<a href="/other-0.3.2-py3-none-any.whl">other</a>'

    with pytest.raises(lifecycle.UpgradeError) as caught:
        lifecycle.discover_latest_release(fetch=fetch)
    assert caught.value.code == "release_not_ready"


def test_direct_release_asset_content_type_is_rejected():
    headers = Message()
    headers["Content-Type"] = "application/octet-stream"
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.geturl.return_value = lifecycle.LATEST_RELEASE_URL
    response.headers = headers
    response.read.return_value = b"<html></html>"
    with mock.patch.object(lifecycle.urllib.request, "urlopen", return_value=response), \
         pytest.raises(lifecycle.UpgradeError) as caught:
        lifecycle._fetch_page(lifecycle.LATEST_RELEASE_URL)
    assert caught.value.code == "release_format_changed"
    assert "expanded_assets" in caught.value.hint


def test_dry_run_reports_update_without_calling_pip():
    with mock.patch.object(lifecycle, "discover_latest_release", return_value=_release()), \
         mock.patch.object(lifecycle, "_run") as run:
        result = lifecycle.upgrade(
            dry_run=True, current_version="0.3.1", installation=_installation(),
        )
    assert result["status"] == "update_available"
    assert result["dry_run"] is True
    assert result["verified"] is False
    assert result["command"][-1] == "sesskit==0.3.2"
    assert "sesskit==v0.3.2" not in result["command"]
    run.assert_not_called()


@pytest.mark.parametrize(("current", "latest", "status"), [
    ("0.3.2", "0.3.2", "unchanged"),
    ("0.3.3", "0.3.2", "ahead"),
])
def test_same_or_newer_local_version_never_calls_pip(current, latest, status):
    with mock.patch.object(lifecycle, "discover_latest_release", return_value=_release(latest)), \
         mock.patch.object(lifecycle, "_run") as run:
        result = lifecycle.upgrade(current_version=current, installation=_installation())
    assert result["status"] == status
    assert result["command"] is None
    run.assert_not_called()


def test_upgrade_uses_normalized_pin_and_verifies_in_fresh_process():
    pip = _completed(["pip"], stdout="Successfully installed sesskit-0.3.2")
    verify = _completed(["verify"], stdout="0.3.2\n")
    with mock.patch.object(lifecycle, "discover_latest_release", return_value=_release()), \
         mock.patch.object(lifecycle, "_run", side_effect=[pip, verify]) as run:
        result = lifecycle.upgrade(current_version="0.3.1", installation=_installation())
    assert result["status"] == "updated"
    assert result["previous_version"] == "0.3.1"
    assert result["current_version"] == "0.3.2"
    assert result["verified"] is True
    pip_command = run.call_args_list[0].args[0]
    assert pip_command[:4] == ["/tmp/venv/bin/python", "-m", "pip", "install"]
    assert pip_command[-1] == "sesskit==0.3.2"
    verify_command = run.call_args_list[1].args[0]
    assert verify_command[0] == "/tmp/venv/bin/python"
    assert "importlib.metadata" in verify_command[2]


def test_pip_failure_is_structured_and_does_not_claim_verification():
    failed = _completed(["pip"], code=1, stderr="No matching distribution found")
    with mock.patch.object(lifecycle, "discover_latest_release", return_value=_release()), \
         mock.patch.object(lifecycle, "_run", return_value=failed), \
         pytest.raises(lifecycle.UpgradeError) as caught:
        lifecycle.upgrade(current_version="0.3.1", installation=_installation())
    assert caught.value.code == "upgrade_failed"
    assert "No matching distribution found" in caught.value.message
    assert caught.value.next_commands[0].startswith("/tmp/venv/bin/python -m pip install")


def test_post_install_version_mismatch_is_verification_failure():
    pip = _completed(["pip"])
    verify = _completed(["verify"], stdout="0.3.1\n")
    with mock.patch.object(lifecycle, "discover_latest_release", return_value=_release()), \
         mock.patch.object(lifecycle, "_run", side_effect=[pip, verify]), \
         pytest.raises(lifecycle.UpgradeError) as caught:
        lifecycle.upgrade(current_version="0.3.1", installation=_installation())
    assert caught.value.code == "verification_failed"


def test_foreign_system_installation_is_never_written_through_pip(tmp_path):
    root = tmp_path / "system" / "lib" / "python3.14" / "site-packages"
    root.mkdir(parents=True)
    with mock.patch.object(lifecycle.sys, "prefix", str(tmp_path / "system")), \
         mock.patch.object(lifecycle.sys, "base_prefix", str(tmp_path / "system")), \
         mock.patch.object(lifecycle, "_distribution_root", return_value=root), \
         pytest.raises(lifecycle.UpgradeError) as caught:
        lifecycle.detect_installation()
    assert caught.value.code == "unsupported_installation"
    assert caught.value.next_commands == []


def test_real_homebrew_prefix_gets_owner_upgrade_route(tmp_path):
    root = tmp_path / "site-packages"
    root.mkdir()
    with mock.patch.object(lifecycle.sys, "prefix", "/opt/homebrew"), \
         mock.patch.object(lifecycle.sys, "base_prefix", "/opt/homebrew"), \
         mock.patch.object(lifecycle, "_distribution_root", return_value=root), \
         pytest.raises(lifecycle.UpgradeError) as caught:
        lifecycle.detect_installation()
    assert caught.value.code == "unsupported_installation"
    assert caught.value.next_commands == ["brew update && brew upgrade sesskit"]


def test_virtualenv_installation_is_supported(tmp_path):
    root = tmp_path / "lib" / "site-packages"
    root.mkdir(parents=True)
    with mock.patch.object(lifecycle.sys, "prefix", str(tmp_path)), \
         mock.patch.object(lifecycle.sys, "base_prefix", "/usr"), \
         mock.patch.object(lifecycle, "_distribution_root", return_value=root):
        installation = lifecycle.detect_installation()
    assert installation.kind == "venv"
    assert installation.pip_args == ()
    assert Path(installation.prefix) == tmp_path
