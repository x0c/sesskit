# TASKBOARD — 多 Agent 并行协作看板

> 规则见 agentsync 全局 docs/AGENT_TASKBOARD_GUIDE.md。只编辑自己的条目；完成后删除。

| 任务 | 状态 | 影响范围 | 开始 | 最近更新 | 备注 |
|---|---|---|---|---|---|
| SessKit verification repair A | 进行中 | src/sesskit/conformance.py, src/sesskit/cli.py (verify only), tests/test_conformance.py, docs/CONTRACT.md, README.md, README.zh-CN.md | 2026-09-30 11:26 | 2026-09-30 11:26 | OpenCode Luna medium; coordinator owns dispatch and release |
| SessKit portable regression B | 验证中 | tests/test_runtime_goldens.py, docs/RUNTIME_PARSING_KNOWLEDGE_BASE.md, pyproject.toml (version only), src/sesskit/__init__.py (version only) | 2026-09-30 11:26 | 2026-09-30 11:36 | v0.2.2 metadata and English draft notes ready; Low-friction owns other pyproject fields; coordinator owns ship |
| Codex framing-eviction empty-ID fix | 已完成 | src/sesskit/parsers/codex.py（assistant弱推断永空id）、tests/test_completion_id.py（D3改强终局fixture+弱显示/空id+驱逐用例）；不动其它parser/typed/Corral产品代码/proxy/iOS/版本pin | 2026-10-01 | 2026-10-01 | 42KB驱逐仍DONE显示+空id零推送；真终局可通知且稳定/互异；517全绿+ruff绿；hub/notifier逐段验证；report追加framing-eviction独立节 |
| Codex text-only terminal empty-ID fix | 已完成 | src/sesskit/parsers/codex.py（删text-only回退）、tests/test_completion_id.py（text/error文本有锚全无断言空id）；不动其它parser/completion_id_for/Corral产品代码/proxy/iOS/版本pin | 2026-10-01 | 2026-10-01 | 文本有锚全无仍DONE显示+空id零推送；真锚终局1推送；525全绿+ruff绿；report native-terminal节内correction段 |
| Codex native terminal identity fix | 已完成 | src/sesskit/parsers/codex.py（_terminal_fingerprint：event id优先/turn+ts/保守空）、tests/test_completion_id.py（CodexTerminalIdentityTests 6用例）、_native_event_id注释修正；不动completion_id_for/其它provider/Corral产品代码/proxy/iOS/版本pin | 2026-10-01 | 2026-10-01 | 同会话两真终局同文互异（hub实测1+1）；legacy时间戳回退稳定/互异；523全绿+ruff绿；report native-terminal独立节 |

| Low-friction environment A | 进行中 | pyproject.toml dev tooling and reproducible environment metadata only; no parser/conformance/contract edits | 11:31 | 2026-09-30 11:31 | OpenCode Luna medium; dispatch 20260930-113144-low-friction; coordinator serializes integration/release; do not remove foreign rows |
