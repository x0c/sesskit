# SessKit

Read, parse, and export local coding-agent session history into one schema.

## Documentation navigation

- `README.md` / `README.zh-CN.md`: **must read** before changing public CLI/API surface, install instructions, or open-source facade — wrong install channel (e.g. claiming PyPI while unpublished) breaks stranger onboarding.
- `docs/GITHUB_DISCOVERY.md`: **must read** before changing GitHub About description, Topics, or the README first screen — skipping it reuses Corral TUI search queries, or describes SessKit as a session manager.
- `schemas/*.json` and `src/sesskit/schemas/*.json`: **must read** before changing JSON envelope fields or transcript event types — keep both copies identical; breaking the contract breaks every language consumer and Corral Homebrew’s vendored sdist.
- `docs/CONTRACT.md`: **must read** before adding a runtime parser, changing status/event enums, changing session-dict vs path load wiring, export/share write safety, listing filters (`include_missing_cwd`), Pi branch restore, **Cursor list `scan_signature` vs conversation WAL versioning**, error vs empty-transcript semantics, or verification of parse correctness — skipping it reintroduces “tests green / real show broken”, history overwrite, system-noise in previews, Corral TUI full-rescan storms from list-level WAL, or Corral recover-list regressions.

## Hard constraints (agent)

- Public load for a scanned session goes through `load_session_conversation` / `RuntimeParser.load_conversation(session)`. Parser modules remain path-based (OpenCode: db + id).
- Cursor list-level `scan_signature` must omit `store.db-wal`; conversation / `extra_version` must still include WAL (`docs/CONTRACT.md`).
- When improving SessKit for Corral consumers, co-changing Corral is allowed and expected; keep Corral’s recover-list cwd filter and soft-fail-on-missing-history unless the product owner explicitly changes them.
- After parser / load / transcript changes: run real-wiring tests and sample every installed runtime’s live history for roles, no system-noise in plain turns, and plain↔events user-text alignment (`docs/CONTRACT.md` Verification).

## Remote

- GitHub (public): `https://github.com/x0c/sesskit`
