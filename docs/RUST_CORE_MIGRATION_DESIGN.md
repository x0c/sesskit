# Rust core migration

Status: adopted direction, 2026-11-07. Implementation and release acceptance
are tracked below; an unchecked step is not a delivered capability.

## Decision and boundaries

Gradually replace performance-critical SessKit internals with one reusable Rust
core. Extend the existing `crates/sesskit-stats` implementation rather than
starting another runtime parser in a consumer. Keep the public Python API,
normalized models, JSON envelopes, schemas, read-only history access and native
error/completion evidence compatible. The owner requested an implementation,
verification and a new release, not merely this plan.

Rust is not an end in itself. Select migrations using measured end-to-end
benefit and semantic parity, including cold process startup and installed
behavior. The existing statistics speedup combines implementation language,
less decoding, time-window pruning and concurrency; it is not an isolated
language benchmark. Python remains the differential reference during migration,
not a permanently independent second implementation after parity is established.

Native-extension rules are owned by the [shared Python standard](~/Codes/_standards/python.md#原生扩展rust--pyo3): reduce substantial native input to a
small result, release the interpreter during native work, provide a disable
switch and Python fallback, and ship prebuilt supported-platform wheels plus
source. Do not build a complete intermediate Rust JSON tree merely to recreate
that entire tree as Python dictionaries.

## Delivery stages

1. **Inventory and baseline.** Identify shared expensive paths and their
   existing contracts. Record immutable inputs, environment, end-to-end timing,
   output parity and ownership of pre-existing uncommitted changes.
2. **First replacement.** Migrate only a bounded, measured hot path with compact
   results. Reuse existing native code where its semantics match. The installed
   default path must actually use the accepted Rust implementation; an unused
   helper or a new API alone does not count as replacing the Python path.
3. **Compatibility gates.** Test native-enabled and forced-Python execution,
   invalid/truncated histories, branch ordering, error evidence, concurrent
   writes and unavailable native support. Preserve unsupported cases explicitly
   rather than weakening the public contract to fit the existing stats engine.
4. **Release and installation.** Publish versioned artifacts built only from
   the accepted committed tree, install from a verified download outside the
   checkout, then exercise the public API and CLI. Preserve unrelated working
   edits and do not include them in release artifacts.
5. **Later migration.** Expand runtime coverage and move remaining full
   conversation/activity/usage/incremental interpretation only after
   equivalent evidence exists. Keep Python compatibility wrappers; avoid a
   flag-day rewrite or a second consumer-owned parser.

## First-release scope

The first release replaces full JSON decoding on compact listing projections:

- OpenCode v2 list records: retain only user/assistant text, native finish/error,
  idle outcome and synthetic-goal evidence; do not decode tool inputs, outputs or
  encrypted reasoning into Python objects.
- Claude/Codex bounded excerpt backfill, and Codex activity-clock backfill: retain
  the fields consumed by the existing Python helpers, without widening the
  separate status/completion tail.
- A PyO3 bridge uses the same Rust library as the existing statistics engine.
  Streaming Serde visitors discard unneeded values; selected leaf values retain
  their raw JSON representation. Python still owns all runtime-specific
  interpretation and public models. No history-writing or additional cache is
  introduced.
- Native support is selected automatically when installed and can be disabled
  with `SESSKIT_NATIVE=0`. Missing native support or an unsupported JSON encoding
  uses the existing Python path. No model/account data is sent elsewhere.
- Ship supported-platform native wheels, a pure-Python compatibility wheel and
  source from the same accepted commit. Default installation must select a
  native wheel where available; the compatibility wheel is not advertised as
  accelerated.

Baseline on the current checkout: 1,084 Python tests and 50 subtests passed,
with one existing skip; both native unit tests passed without warnings. A
profiled uncached 100-per-runtime scan took 15.0 s with about 148,000 JSON loads
(3.9 s cumulative decoding); OpenCode v2 list construction was the largest
visible parse path. These are profiling observations, not promised speedups;
frozen-source and installed-path comparisons are still required.

Full six-runtime interpretation, new host policy and unrelated dirty working-tree
features are excluded. Later stages can migrate the remaining work only after
the first replacement's measured gain and compatibility gates pass.

## Measured replacement

A frozen read-only SQLite backup was used for paired OpenCode v2 list-builder
runs (100 sessions, identical full normalized-record hashes). Python took
1.491 s on the first run and 0.726 s warm; native took 0.365 and 0.372 s.
The warm comparison is about 2x for this replaced construction path, not a
claim that all session discovery is now twice as fast. A separate full
1,000-session scan produced identical normalized-record hashes in both modes;
its process/liveness and SQL overhead remain and no overall speedup is claimed.

A 100 MB synthetic tool-heavy projection dropped from 0.099–0.109 s to
0.016–0.017 s after eliminating a repeated JSON scan and borrowing immutable
Python strings while holding strong references across interpreter detachment.
Synthetic throughput is supplementary evidence, not a real-history acceptance
substitute.

## Build and installation

- Python 3.10+, Rust 1.88+ for source builds; prebuilt wheels require no compiler.
- Keep the Python binding manifest at repository-root `Cargo.toml`, with its
  Rust source under `crates/sesskit-python/src/`. A nested manifest selected
  implicitly produced a spurious missing-root-manifest diagnostic during sdist
  creation; selecting it explicitly lost the root Python project. Source
  distribution rebuild is an acceptance gate, not just archive membership.
  Internal `TASKBOARD.md` and local `build/` copies are excluded explicitly and
  rejected by the release builder. The CI toolchain installs Clippy explicitly;
  a minimal Rust profile alone does not provide that required diagnostic.
- Native/source backend: Maturin. `scripts/build_release.py --out <directory>
  --pure --sdist` builds the local native wheel, an explicit setuptools-built
  compatibility wheel and the source distribution. Build release artifacts
  from the accepted committed-source archive, not unrelated working edits.
- Linux cross-builds use the same command with `--target <triple> --zig` and
  a manylinux_2_17 floor; no local container runtime is needed.
- `.github/workflows/native-wheels.yml` builds and runs installed-wheel tests
  on native macOS/Linux ARM64 and x86_64 runners, at Python 3.10 and 3.15.
  The manual ref is checked out exactly; artifacts include source identity and
  checksums. This is a verification/build helper, not a hidden auto-publisher.
- The published `wheels.html` is a pip `--find-links` page, not a package index
  registration. `--no-index` keeps installation on the exact GitHub release;
  pip chooses a compatible native wheel, with a universal Python fallback.
  Existing consumers can still use the fixed `py3-none-any` wheel directly,
  without native acceleration. PyPI availability is unchanged.

Reference implementation inspected before coding: Serde's
[`IgnoredAny` visitor](https://github.com/serde-rs/serde/blob/master/serde_core/src/de/ignored_any.rs)
and [discarding-data example](https://serde.rs/ignored-any.html), plus PyO3's
[`word-count` detachment example](https://github.com/PyO3/pyo3/blob/main/examples/word-count/src/lib.rs).
Reuse the streaming visitor/skip and borrowed-data detachment patterns, not
runtime-specific parsing or the reference's word-count behavior. Packaging uses
Maturin's [mixed-project layout](https://www.maturin.rs/project_layout.html).
Runner labels follow the [official hosted-runner table](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
New workflow action pins were checked at their exact commits for `node24`.

## Acceptance record

- [x] Bounded implementation scope and baseline recorded.
- [x] Native/Python differential tests and complete diagnostics inspected:
  final candidate 1,108 tests + 42 subtests passed native-enabled, 1,077 tests
  + 42 subtests passed with native disabled (32 native-specific skips and one
  existing environment skip); Ruff and native Clippy/tests are clean.
- [x] Real-history public wiring checked: installed-artifact `sesskit verify
  --sample 30` passed 153/153 session loads and 150 applicable event/reader
  comparisons (three expected unsupported Kimi typed-activity projections).
  Exact installed-artifact native/Python comparisons passed for 12 sessions:
  success and abnormal histories for Claude, Codex, OpenCode, plus Pi success
  and failure, and unchanged Cursor/Kimi wiring. Kimi's native assistant-text
  mapping remains unverified rather than inferred from a substring; it is not
  used as evidence for the new profiles. One new Codex success probe passed;
  Claude controller exited before a history id was available, and OpenCode's
  controller timed out. No fresh success is claimed for those two runtimes;
  their exact persisted success/abnormal histories passed instead.
- [x] Measured improvement on the actual replaced public path, as recorded
  above with frozen sources and identical normalized outputs.
- [x] Migration source isolated, committed and pushed on `main` as `1e0a5ce`;
  unrelated working-tree changes were left outside the commit.
- [ ] Supported-platform artifacts and source distribution verified. Local
  ARM64 native and compatibility wheels install and execute, Python 3.10/3.15
  both load the extension, and the source archive rebuilds and installs its
  native wheel. The first four-runner `native-wheels` run (`42356384029`)
  built the native/compatibility wheels and source distribution and passed
  Ruff plus Rust tests on every runner, but all four jobs stopped before the
  installed-wheel Python tests because the minimal Rust toolchain lacked
  `cargo-clippy`. A working-tree follow-up adds `--component clippy`; at this
  update it is neither part of `1e0a5ce` nor rerun. No four-platform artifact
  pass is claimed.
- [ ] `v0.3.0` tag and GitHub release published; downloaded release installed
  and exercised. No `v0.3.0` release exists at this update.

Native statistics interpretation and existing parity evidence remain owned by
[NATIVE_STATS_ENGINE_DESIGN.md](NATIVE_STATS_ENGINE_DESIGN.md); session compatibility
and runtime acceptance remain owned by [CONTRACT.md](CONTRACT.md).
