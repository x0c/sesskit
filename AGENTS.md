# SessKit

Read, parse, and export local coding-agent session history into one schema.

Standalone Python library plus JSON CLI covering six runtimes (`claude`, `codex`, `opencode`, `kimi`, `cursor`, `pi`). No downstream-host coupling: no downstream product names, host-specific paths, or host-owned semantics belong in docs, code, or schemas.

## 文档导航

- Read the documents whose described content is relevant to the current task.

- `README.md` / `README.zh-CN.md`: public CLI/API surface, install instructions, and open-source facade.
- `docs/GITHUB_DISCOVERY.md`: About description, Topics, and README first-screen rules with need-query baselines; skipping it reuses session-manager search queries or misdescribes SessKit as a session manager.
- `docs/CONTRACT.md`: session contract — envelope, schemas, load wiring, listing modes, write safety, status tags and abnormal endings, and verification.
- `schemas/*.json` and `src/sesskit/schemas/*.json`: JSON envelope fields and transcript event types; both copies stay identical.
- `docs/RUNTIME_PARSING_KNOWLEDGE_BASE.md`: Native history parsing for six runtimes: scan, plain-turn load, liveness, versioning, and per-runtime success versus abnormal signals.
- `docs/UNIFIED_ABSTRACTION_KNOWLEDGE_BASE.md`: Unified abstraction over all runtimes: session and conversation models, transcript and typed activity events, outcomes, errors, tool results, evidence, and JSON Schemas.
- `docs/ABSTRACTION_REDESIGN.md`: Target cross-runtime abstraction: current design problems, adapter/capability, turn, typed event and error-taxonomy design, invariants, staged migration, and research evidence.
- `docs/LISTING_CLI_EXPORT_KNOWLEDGE_BASE.md`: Listing, search, show, export, share, and describe surface: JSON envelope, reference resolution, excerpt rules, and atomic write safety.

## Hard constraints (agent)

- Classify ownership before changing session behavior. This library owns native history interpretation, normalized session/message/activity schemas, real activity clocks and native completion/error evidence. Downstream products own presentation, sorting/grouping policy, hosting, remote delivery, generated titles, attention and notification decisions. Fix reusable interpretation here; never add downstream product behavior or dependencies, and never require a consumer to maintain a second parser. Host-specific adaptation belongs in explicit extension contracts. Publish versioned artifacts for consumer dependency handoff.
- Public load for a scanned session goes through `load_session_conversation` / `RuntimeParser.load_conversation(session)`. Parser modules remain path-based (OpenCode: db + id).
- Cursor list-level `scan_signature` must omit `store.db-wal`; conversation / `extra_version` must still include WAL (`docs/CONTRACT.md`).
- Keep the default missing-`cwd` drop for resume-style listings and the missing-history error (`ConversationLoadError` → error envelope); never disguise unreadable history as an empty success.
- After parser / load / transcript / status-inference changes: run real-wiring tests **and** fine-grained live checks per installed runtime — success path plus abnormal path when reproducible; assert `status_tag`, `last_agent_msg`, and `load_events` on the **exact** session file/id (`docs/CONTRACT.md` Verification + Status tags). Fixture-only or single-runtime sampling is not enough.
- Abnormal endings that exist in native history (quota, rate limit, provider error, abort) must surface through SessKit; never map error-bearing “complete” markers to `STATUS_DONE`, and never drop error-only assistant turns from `load_events`.

## 领域地图（doc-init）

<!-- 覆盖度复核基线：2026-09-29 · 源码指纹 扫描 78 文件 / Python 38 / 0 子模块 · 基线提交 137946f -->

| 领域 | 入口锚点 |
|------|---------|
| Runtime session parsing | src/sesskit/parsers/ |
| Unified abstraction layer | src/sesskit/models.py, src/sesskit/registry.py, src/sesskit/transcript.py, src/sesskit/activity.py |
| Listing, CLI and export surface | src/sesskit/cli.py, src/sesskit/registry.py, schemas/ |
| Distribution and versioning | pyproject.toml, schemas/ |

## 待补充知识库（doc-init backlog）

- [待补充] Distribution versioning KB — Entry anchor: pyproject.toml; Content summary: Install channels, release sdist and dual-copy schema versioning, and the mandatory parser verification workflow.

## Remote

- GitHub (public): `https://github.com/x0c/sesskit`
