# GitHub discovery (SessKit)

**Must read** before changing the public GitHub About description, Topics, or README first screen. Ranking method and “learn from winners” rules live in the global agentsync doc `GITHUB_STAR_GROWTH_GUIDE.md` §4; this file freezes **SessKit’s** need queries and the 2026-09-12 baseline.

SessKit is a **library + JSON CLI** that parses and exports local session files. It is **not** Corral (a TUI / session manager). Do not reuse Corral search queries (`claude code session manager`, `claude code session history`) as this repo’s KPI, and do not describe SessKit as a session manager.

Do not rename the repository unless the product owner explicitly asks.

## Frozen need queries (Best Match, no `--sort`)

| Person is stuck | Query they would type | 2026-09-12 pool | SessKit rank |
|---|---|---:|---|
| Claude Code chat is files on disk; need to parse them | `parse claude code session files` | 12 | not in pool |
| Need a JSON dump of local Claude Code conversations, not a TUI | `export claude code conversation json` | 7 | not in pool |
| Codex CLI history is local; need to parse those session files | `parse codex cli session files` | 1 | not in pool |
| Cursor agent chats are on disk; need to parse them in a script | `parse cursor agent conversation files` | 0 | not in pool |
| Need a parser for Claude Code transcripts, not a session manager | `claude code transcript parser` | 33 | **#11** |

Re-measure with `gh api "search/repositories?q=…&per_page=100"` (no `sort`), sleep 2s between queries, page to ~300 when the pool is large. Same-day retest after an About change is not a failure.

## Winner wording (do copy the *kind* of sentence)

On parse/export queries, the front of the pool says **parse / export / session files / JSONL / transcript parser / Python library**. Names are often the need (`claude-session-parser`, `cc-history-export`, `agent-session-parser`). Almost none of those winners are TUIs.

Do **not** copy their product claims (usage dashboards, Chrome exporters for claude.ai, Claude↔Codex resume converters).

## Winner tricks (types only)

| Type | Seen on | Adopt for SessKit? |
|---|---|---|
| Name/description = the query | `nitsanavni/session`, `BobDLA/agent-session-parser` | Yes — About + first README sentence (cannot rename) |
| Install in the first screen | `claude-session-parser`, `claude-code-data`, `claude-transcripts` | Yes |
| Explicit non-goal / “not a wrapper / not a watcher” | `agent-session-parser` | Yes — “not a TUI” |
| Support matrix | `ai-chat-extractor`, `agent-session-parser` | Yes — runtimes we actually parse |
| Screenshot / GIF | `tracebook` (a dashboard) | No — this repo has no UI |
| Crates/npm badges | parsers that are actually published | License badge only until PyPI exists |

## About fields (separate from git)

Pushing README does **not** update GitHub About. After changing description/topics, use the API (`gh api -X PATCH` / `PUT …/topics`). Keep Topics aligned with real platforms (no `windows` until Windows is supported).
