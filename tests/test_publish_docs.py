"""Doc checks for the owned README maintainer sections.

Scope is the maintainer section only ("Release verification" /
"发布校验"): foreign usage examples with `<session-id>` placeholders are
out of scope. These tests assert the documented publishing commands are
syntactically valid shell, reference real repo paths, carry no literal
secret, and describe OIDC accurately. No upload or network happens here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README_EN = REPO / "README.md"
README_ZH = REPO / "README.zh-CN.md"
PUBLISH_SCRIPT = REPO / "scripts" / "publish.py"
WORKFLOW = REPO / ".github" / "workflows" / "publish-pypi.yml"

EN_HEADING = "## Release verification (maintainers)"
ZH_HEADING = "## 发布校验（维护者）"


def section_text(readme: Path, heading: str) -> str:
    text = readme.read_text(encoding="utf-8")
    start = text.index(heading)
    rest = text[start:]
    nxt = rest.find("\n## ", 1)
    return rest[:nxt] if nxt != -1 else rest


def bash_blocks(section: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", section, re.DOTALL)


def test_maintainer_sections_exist():
    en = section_text(README_EN, EN_HEADING)
    zh = section_text(README_ZH, ZH_HEADING)
    assert bash_blocks(en), "EN maintainer section has no bash blocks"
    assert bash_blocks(zh), "ZH maintainer section has no bash blocks"


def test_maintainer_bash_blocks_have_valid_syntax():
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - macOS/Linux CI always has bash
        import pytest

        pytest.skip("bash not available")
    for readme, heading in ((README_EN, EN_HEADING), (README_ZH, ZH_HEADING)):
        for block in bash_blocks(section_text(readme, heading)):
            proc = subprocess.run([bash, "-n"], input=block, capture_output=True, check=False, text=True, timeout=60)
            assert proc.returncode == 0, f"{readme.name}: bash -n failed: {proc.stderr}\n{block}"


def test_maintainer_bash_blocks_have_no_placeholders_or_literal_secrets():
    for readme, heading in ((README_EN, EN_HEADING), (README_ZH, ZH_HEADING)):
        for block in bash_blocks(section_text(readme, heading)):
            assert "<" not in block and ">" not in block, f"{readme.name}: placeholder in docs:\n{block}"
            assert "TWINE_PASSWORD=<" not in block, f"{readme.name}: literal secret placeholder"
            for line in block.splitlines():
                stripped = line.strip()
                if stripped.startswith("export TWINE_PASSWORD="):
                    raise AssertionError(f"{readme.name}: secret must not be exported literally: {stripped}")
    for readme, heading in ((README_EN, EN_HEADING), (README_ZH, ZH_HEADING)):
        section = section_text(readme, heading)
        assert "--password" not in section
        assert "--token" not in section


def test_maintainer_docs_use_secret_store():
    for readme, heading in ((README_EN, EN_HEADING), (README_ZH, ZH_HEADING)):
        section = section_text(readme, heading)
        assert "add-generic-password -s pypi-sesskit -a __token__ -w" in section, (
            f"{readme.name}: missing one-time secret-store command"
        )
        assert "find-generic-password" in section, f"{readme.name}: missing secret-store instruction"
        assert 'TWINE_PASSWORD="$(' in section, f"{readme.name}: secret must come from substitution, not a literal"


def test_maintainer_referenced_paths_exist():
    for readme, heading in ((README_EN, EN_HEADING), (README_ZH, ZH_HEADING)):
        section = section_text(readme, heading)
        assert "scripts/publish.py" in section
        assert ".github/workflows/publish-pypi.yml" in section
        assert PUBLISH_SCRIPT.is_file()
        assert WORKFLOW.is_file()


def test_maintainer_runnable_existing_version_commands():
    en = section_text(README_EN, EN_HEADING)
    zh = section_text(README_ZH, ZH_HEADING)
    for section in (en, zh):
        assert "verify --version 0.2.23 --check-only" in section
        assert "check-dist --version 0.2.23 --dist dist" in section
        assert "upload --version 0.2.23 --dist dist --confirm-upload" in section
        assert "0.2.24" not in section


def test_maintainer_oidc_wording_is_accurate():
    en = section_text(README_EN, EN_HEADING)
    zh = section_text(README_ZH, ZH_HEADING)
    assert "no stored API token" in en
    assert "— no tokens." not in en and "(no tokens)" not in en
    assert "无预存 API" in zh
    assert "无令牌" not in zh


def test_documented_subcommands_exist():
    for sub in ("verify", "check-dist", "upload"):
        proc = subprocess.run(
            [sys.executable, str(PUBLISH_SCRIPT), sub, "--help"],
            capture_output=True,
            check=False,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, sub
