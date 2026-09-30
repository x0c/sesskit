# TASKBOARD — 多 Agent 并行协作看板

> 规则见 agentsync 全局 docs/AGENT_TASKBOARD_GUIDE.md。只编辑自己的条目；完成后删除。

| 任务 | 状态 | 影响范围 | 开始 | 最近更新 | 备注 |
|---|---|---|---|---|---|
| SessKit verification repair A | 进行中 | src/sesskit/conformance.py, src/sesskit/cli.py (verify only), tests/test_conformance.py, docs/CONTRACT.md, README.md, README.zh-CN.md | 2026-09-30 11:26 | 2026-09-30 11:26 | OpenCode Luna medium; coordinator owns dispatch and release |
| SessKit portable regression B | 验证中 | tests/test_runtime_goldens.py, docs/RUNTIME_PARSING_KNOWLEDGE_BASE.md, pyproject.toml (version only), src/sesskit/__init__.py (version only) | 2026-09-30 11:26 | 2026-09-30 11:36 | v0.2.2 metadata and English draft notes ready; Low-friction owns other pyproject fields; coordinator owns ship |
| SessKit 0.2.5 turn-end lifecycle shipment (delegated) | 进行中 | src/sesskit/activity.py（task_complete恒发lifecycle，已落）、tests/test_runtime_goldens.py（2断言机械跟进，已落，待B复核）、docs/UNIFIED_ABSTRACTION_KNOWLEDGE_BASE.md（typed契约段）、pyproject.toml + src/sesskit/__init__.py（0.2.5 patch bump）、GitHub Release资产、Corral sesskit_dep pin/lock/装机 | 05:10 | 2026-10-01 05:10 | Coordinator brief明确委托本次依赖交付；0.2.4已发布不得覆盖；B行goldens文件有2断言机械触碰（行为变更的诚实成本，已跑全套）；parsers/codex.py等外人改动只集成不碰；发版后删本行 |

| Low-friction environment A | 进行中 | pyproject.toml dev tooling and reproducible environment metadata only; no parser/conformance/contract edits | 11:31 | 2026-09-30 11:31 | OpenCode Luna medium; dispatch 20260930-113144-low-friction; coordinator serializes integration/release; do not remove foreign rows |
