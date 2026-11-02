"""Structural tests for .github/workflows/publish-pypi.yml.

Parsed with stdlib only (no YAML dependency): action pins, least-privilege
permissions, helper/tag-checkout separation, env-passed tag, exact artifact
handoff, and triggers are asserted from the raw lines. These tests never
upload or contact the network.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish-pypi.yml"

# Reviewed commit SHAs (2026-11-01, via `git ls-remote` on the tags in comments).
EXPECTED_PINS = {
    "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "pypa/gh-action-pypi-publish": "7245906fb61fa33a13554f9d5fb856fe61758086",
}
USES_RE = re.compile(r"uses:\s*(\S+)")


def raw_lines() -> list[str]:
    return WORKFLOW.read_text().splitlines()


def code_lines() -> list[str]:
    """Workflow lines with full-line comments removed and trailing comments cut."""
    lines = []
    for raw in raw_lines():
        if raw.strip().startswith("#"):
            continue
        lines.append(raw.split("#", 1)[0].rstrip())
    return lines


def run_block_lines() -> list[str]:
    """Lines inside `run: |` script blocks (indentation-tracked)."""
    blocks: list[str] = []
    in_block = False
    block_indent = 0
    for raw in raw_lines():
        stripped = raw.strip()
        if stripped.startswith("#") or not stripped:
            continue
        indent = len(raw) - len(raw.lstrip())
        start = re.match(r"^run:\s*\|\s*$", stripped)
        if start:
            in_block = True
            block_indent = indent
            continue
        if in_block:
            if indent <= block_indent:
                in_block = False
            else:
                blocks.append(raw)
    return blocks


def job_slices() -> dict[str, list[str]]:
    """Map job name -> its lines (2-space headers under `jobs:`, comments dropped)."""
    slices: dict[str, list[str]] = {}
    current: str | None = None
    in_jobs = False
    for raw in raw_lines():
        if re.match(r"^jobs:\s*$", raw):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if raw.strip().startswith("#"):
            continue
        header = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", raw)
        if header:
            current = header.group(1)
            slices[current] = []
            continue
        if current is not None:
            slices[current].append(raw)
    return slices


def test_workflow_actions_pinned_to_reviewed_shas():
    refs = [USES_RE.search(line).group(1) for line in code_lines() if USES_RE.search(line)]
    assert len(refs) == 8, refs  # 3x checkout, setup-python, 2x upload-artifact, download, pypi-publish
    found: dict[str, str] = {}
    for ref in refs:
        match = re.fullmatch(r"([\w.-]+/[\w.-]+)@([0-9a-f]{40})", ref)
        assert match is not None, f"unpinned action reference: {ref}"
        found[match.group(1)] = match.group(2)
    assert found == EXPECTED_PINS


def test_workflow_has_no_floating_refs():
    for line in code_lines():
        match = USES_RE.search(line)
        if match:
            assert "@v" not in match.group(1), line
            assert "@release/" not in match.group(1), line
            assert "@main" not in match.group(1), line


def test_workflow_least_privilege():
    text = "\n".join(code_lines())
    assert re.search(r"^permissions:\s*\{\}\s*$", text, re.MULTILINE) is not None
    assert text.count("id-token: write") == 1
    slices = job_slices()
    assert set(slices) == {"build", "validate-release", "publish"}
    build = "\n".join(slices["build"])
    validate = "\n".join(slices["validate-release"])
    publish = "\n".join(slices["publish"])
    for producer in (build, validate):
        assert "contents: read" in producer
        assert "id-token" not in producer
    assert "id-token: write" in publish
    assert "contents: write" not in publish
    assert "actions: read" in publish
    assert "needs:" in publish and "build" in publish and "validate-release" in publish
    assert "needs.build.result" in publish and "needs.validate-release.result" in publish
    assert "environment: pypi" in publish


def publish_gate(build_result: str, validate_result: str) -> bool:
    """Python mirror of the publish job's `if:` gate (kept in sync by test below)."""
    return (build_result == "success" and validate_result == "skipped") or (
        validate_result == "success" and build_result == "skipped"
    )


def test_workflow_publish_gate_is_exactly_one_success_and_one_skip():
    text = "\n".join(job_slices()["publish"])
    assert "needs.build.result == 'success'" in text
    assert "needs.validate-release.result == 'success'" in text
    assert text.count("== 'skipped'") == 2
    # Full truth table over every needs-result GitHub can report: only the
    # two exact (success, skipped) pairings may publish. In particular a
    # success paired with failure/cancelled must never publish.
    outcomes = ("success", "failure", "cancelled", "skipped")
    allowed = {("success", "skipped"), ("skipped", "success")}
    for build in outcomes:
        for validate in outcomes:
            assert publish_gate(build, validate) == ((build, validate) in allowed), (build, validate)
    # Both producers succeeding (impossible per trigger, but must not publish
    # if it ever happened) and both skipped are also denied.
    assert not publish_gate("success", "success")
    assert not publish_gate("skipped", "skipped")


def test_workflow_helper_never_comes_from_tag_tree():
    text = "\n".join(code_lines())
    # Helper checkout pins the explicit trusted revision twice (both producers).
    assert text.count("ref: ${{ github.sha }}") == 2
    # Single-file sparse checkout requires cone mode off on both helper
    # steps: cone mode rejects a file pattern with 128 ("is not a directory").
    assert text.count("sparse-checkout-cone-mode: false") == 2
    # Every helper invocation uses the trusted helper checkout, never a bare
    # `scripts/publish.py` that would resolve inside the tag checkout.
    for line in run_block_lines():
        assert not re.search(r"(^|\s)python scripts/publish\.py", line), line
    assert text.count("helper/scripts/publish.py") == 2


def test_workflow_tag_arrives_via_env_with_strict_validation():
    for line in code_lines():
        if "inputs.tag" in line:
            assert re.match(r"^\s*[A-Z_]+:\s*\$\{\{\s*inputs\.tag\s*\}\}\s*$", line), line
    scripts = "\n".join(run_block_lines())
    assert "inputs.tag" not in scripts
    assert "REQUESTED_TAG" in scripts
    assert 'case "${TAG}"' in scripts


def test_workflow_exact_artifact_handoff():
    text = "\n".join(code_lines())
    assert text.count("name: sesskit-dist") == 3  # handoff twice (build + validate-release), fetch once
    assert "if-no-files-found: error" in text


def test_workflow_triggers_cover_dispatch_of_existing_tag():
    text = WORKFLOW.read_text()
    assert '"v*"' in text
    assert "workflow_dispatch:" in text
    assert 'default: "v0.2.23"' in text
    assert "inputs.tag" in text
    assert "0.2.24" not in text


def test_workflow_has_no_secret_wiring():
    code = "\n".join(code_lines()).lower()
    assert "secrets." not in code
    assert "password" not in code
    assert "username" not in code
    assert "twine" not in code
