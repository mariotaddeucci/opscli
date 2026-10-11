# Contributing

**Curupira** (by Caipora Labs) is the product name. The installable package, primary console script, and Python import are `curupira`. The short CLI alias is `curu`.

## Environment setup

Requirements: Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and `gh` for live
discovery (tests use fakes and need no authentication). The package is pure Python;
building a wheel needs no compiler toolchain.

```bash
uv sync --dev
uv run --no-sync prek install
```

`uv sync` installs the Python package and the `curupira` and `curu` console scripts.
[`prek`](https://prek.j178.dev/) replaces `pre-commit` for Git hooks: after `prek install`,
each commit runs the hooks in [`.pre-commit-config.yaml`](.pre-commit-config.yaml). Ruff and
Pyrefly hooks call `uv run --no-sync`, so they use the same locked versions and project
environment as the commands in [AGENTS.md](AGENTS.md). Run every hook on demand with
`uv run --no-sync prek run --all-files`.

[AGENTS.md](AGENTS.md) is the single source for the verification commands, repository
map, code style, testing rules, and security boundaries. It is written for coding agents
and humans alike; every command listed there must pass before opening a pull request, and
CI runs the same steps. Install the documentation tools with `uv sync --group docs`, then
preview the site with `uv run mkdocs serve`.

## Packaging

Hatchling is the PEP 517 backend. It reads the version from `src/curupira/_version.py`
and keeps the `curupira` and `curu` script entry points. `uv build` produces a
pure-Python `py3-none-any` wheel and an sdist. CLI wrappers (`gh`, coding-agent
CLIs, and Azure CLI) are invoked through `AsyncProcessRunner` using
`asyncio.create_subprocess_exec` with bounded capture, timeouts, and process-group
cleanup on POSIX.

Build the distributions, reject retired OpsCli branding in the packaged README and
metadata long description (case-insensitive `opscli`), and run `twine check` with one
command:

```bash
uv run --no-sync python scripts/check_packaged_readme.py
```

CI's "Build and smoke-test distributions" job runs the same script before the install
smoke test. Use `--no-build` to check an existing `dist/` directory, or `--no-twine` to
skip the Twine step.

## Built-in GitHub triggers

GitHub issue and pull-request discovery lives under
`src/curupira/providers/github/` (`issues.py`, `pull_requests.py`). Compatibility
re-exports keep `curupira.tasks.github_issues` and
`curupira.tasks.github_pull_requests` import paths working. Typed filter models are
`IssueAutomationConfiguration` and `PullRequestAutomationConfiguration` in
`src/curupira/models/configuration.py`. Search qualifiers are compiled in
`src/curupira/clients/github_search.py`; GraphQL Search runs over `httpx` in
`github_graphql.py`. Feeds use `PollingTaskFeed` with `[settings.polling]`
(`PollingSettings`). Prompt placeholders come from `COMMON_PROMPT_FIELDS` plus
`IssueItem` / `PullRequestItem` fields in `models/items.py`.

## Extending Curupira

Curupira separates task discovery (`tasks/`), repository version control (`vcs/`), and
coding-agent CLI invocation (`agents/`) into layers whose contracts live in each
`base.py`; see the repository map in [AGENTS.md](AGENTS.md). Curupira does not manage
authentication: provider CLIs and the user's environment provide their own.

To add a task source, implement `TaskSource` and a `Trigger` that declares its
`trigger_type` and Pydantic `configuration_model`. Sources specific to one service or
company belong in a separate distribution that registers the trigger under the
`curupira.triggers` entry-point group and imports only `curupira.plugins`; see the
[plugin guide](docs/en/plugins.md). A built-in trigger lives in a provider package under
`providers/<name>/`, contributes itself through the Pluggy hook `curupira_triggers`, and
is listed in `manager.py`. One provider may contribute multiple triggers (for example
GitHub issues and pull requests) and may also contribute coding-agent adapters. A new
version-control provider implements `VersionControl.clone` and belongs in its own
issue/PR after `vcs/base.py`. A new coding-agent adapter implements
`CodingAgentCliAdapter.build_arguments`, declares its `provider`, `profile_model`,
`display_name`, and `install_url`, and contributes itself through the Pluggy hook
`curupira_coding_agent_adapters` in its provider module. Set `auto_model` only when the
CLI's official docs confirm a native automatic model value (otherwise leave the default
`None`). Override `interactive_launch` only when official CLI docs confirm an interactive
TUI invocation (cite the URL in a code comment); return a pure-data
`InteractiveLaunchSpec` (`argv`, extra `env`, `cwd`, `notes`) and omit headless-only
flags. Leave the default (`None`) when interactive mode is unverified—callers must say so
explicitly and must never invent flags. The new method has a default, so
`PLUGIN_API_VERSION` does not bump and existing plugins keep working. When its CLI reports
the session in a shape other than a `sessionID`, `session_id`, or `thread_id` JSON field,
the adapter overrides `session_id_from_line`; when the CLI instead accepts a
caller-chosen session ID, it sets `assigns_session_id = True` and passes
`request.new_session_id` to the CLI. When the final answer is not a shape the shared
`render_output` already understands, it overrides `render_output`. Adapters never start
processes or handle timeouts and output limits themselves; `run_task` and
`AsyncProcessRunner` own headless runs, and interactive specs are consumed later by the
embedded terminal panel. A built-in coding-agent adapter lives in its own package under
`providers/<name>/` (or shares a package with related triggers), is listed in
`manager.py`, and is discovered through Pluggy so `create_cli_adapter` and profile
validation find it through the registry; it belongs in its own issue/PR after
`agents/base.py`. Providers that need extra Python packages should declare an optional
dependency extra and use lazy imports so the default install stays lean; providers that
only wrap an external CLI stay in the default install. Third-party adapters register under
the `curupira.agents` entry-point group instead. A trigger can supply its own clone
mechanism through `Trigger.create_version_control`. Azure DevOps pull-request listing is
supported via `azure-cli-pull-requests`; cloning still uses the GitHub CLI version-control
adapter unless `path` points at an existing checkout. Trello card discovery is built in
through Scale-Flow's `trello-cli`; other services such as Monday fit a plugin.
Configuration accepts only the trigger types and agent providers registered by built-ins
and installed plugins.

When adding a coding-agent provider, add its package under
`src/curupira/providers/<provider>/`, a matching test package under
`tests/providers/<provider>/`, a page at `docs/en/providers/<provider>.md`, and one line
under "Providers and agents" in the `mkdocs.yml` nav. Keep that nav line and the
provider's entry in the README "Providers and native options" list in alphabetical order
by display name. The provider table on `docs/en/providers.md` and the coding-agent CLIs in
the installation requirements are generated from the agent registry (`display_name`,
`executable`, and `install_url`) during the MkDocs build, so they update automatically.
Other tools, such as forge CLIs, are listed by hand in `docs/data/requirements.toml`.
Trigger-only providers need the package, `manager.py` entry, and tests, but not a
coding-agent docs page.

## Documentation translations

Edit the canonical English pages in `docs/en/` first. When English documentation changes,
open a follow-up pull request to synchronize the corresponding Portuguese (`docs/pt/`) and
Spanish (`docs/es/`) pages. Generated Pydantic reference stays canonical in English; other
languages should link to it rather than manually translating generated fields.

## Documentation brand tokens

The docs theme uses the Caipora Labs palette. These hex values are the brand tokens.
Do not add other brand colors without a new decision. The Material overrides live in
`docs/stylesheets/extra.css`.

| Token | Hex | Role |
| --- | --- | --- |
| `primary` | `#F7931F` | orange brand |
| `primary-deep` | `#EA6114` | contrast / CTAs |
| `skin` | `#8E4F26` | Caipora brown |
| `accent` | `#39873B` | leaf / success |
| `neutral-0` | `#FEFDFC` | background |
| `neutral-900` | `#1A1A1A` | text |

On the light scheme, the header uses `primary` with `neutral-900` text, and primary
buttons use `primary-deep`. Body links use `skin`, which stays readable on `neutral-0`.
The leaf `accent` is the hover color. The dark scheme swaps the neutrals and uses
`primary` for links. The optional product accent (`#014FC9` / `#011E58`) is not
applied on the docs theme.

## PtyTerminal manual checks

Automated tests cover the `PtyTerminal` widget with fake children (`bash -c`, `cat`,
and small Python scripts) under Textual's `run_test`. The following still need a real
machine and are not exercised in CI:

- Drive a real coding-agent CLI inside the widget (Claude Code, Codex, OpenCode, or
  Cursor) and confirm interactive input, redraw, and resize feel correct.
- Kill the Curupira host process hard (for example `SIGKILL`) while a PTY child is
  running and confirm whether an orphan remains; the widget's `atexit` hook cannot run
  in that case.
- Spot-check on macOS: `pty.fork`, window-size updates (`TIOCSWINSZ` / `SIGWINCH`), and
  process-group cleanup.

Windows is unsupported in v1; the widget should render the placeholder instead of
spawning a child.

`pyte` (LGPL-3.0) is a dynamic runtime dependency of this MIT-licensed project; it is
not vendored or statically linked. The PTY reader reads up to 1 KiB, feeds pyte in
256-byte slices under a 5 ms per-tick budget (checked before and after each feed),
then yields (`remove_reader` / `asyncio.sleep(0)` / re-add). Rendering caches Rich
styles, coalesces identical adjacent cells into one segment, rebuilds only dirty
rows into Textual strips, and refreshes those rows at about 30 fps (region refresh
so the compositor stays on the partial-update path). Scrollback uses a bounded
deque on `Screen.index` rather than `pyte.HistoryScreen` (whose per-event
`__getattribute__` wrapper dominated feed time); the deque clears on `reset` and
`resize`. When the child exits, `Process exited (N)` overlays the last content row
so it stays inside the visible height; if the child finished without a trailing
newline on that row, the overlay covers that line's text.

Re-measure with (prints per-run rows plus a min-max summary):

```bash
uv run --no-sync python scripts/measure_pty_throughput.py --seconds 20 --runs 3
```

Throughput and loop latency vary by host and load. Figures below are **min-max
across consecutive runs** of Textual `run_test` size `(120, 40)`, 20 s sample,
1 ms ticker, on Linux 6.12.94+ with Python 3.11.17 (workloads did not finish in
that window). Do not treat a single-run point as authoritative. Loop **max** is a
noisy scheduling/GC tail and is **not** a latency goal; the only hard target when
changing this path is **p99** (`yes` / `seq` < 50 ms, dense `cat` < 100 ms).

| Machine | Workload | Throughput | p99 (min-max) | max (noisy; not a goal) |
| --- | --- | --- | --- | --- |
| 4 CPUs (Intel Xeon, 15 GiB) | `yes \| head -c 50000000` | 0.345-0.371 MB/s | 16.2-17.2 ms | 30.4-173.1 ms |
| 4 CPUs | `seq 2000000` | 0.522-0.561 MB/s | 19.6-21.7 ms | 33.3-40.7 ms |
| 4 CPUs | `cat` of a 40 MiB file | 0.814-0.850 MB/s | 54.4-62.3 ms | 108.1-125.5 ms |
| 8 CPUs (Linux 6.12.94, Python 3.11.17) | `yes \| head -c 50000000` | 0.258-0.277 MB/s | 18.6-20.0 ms | 33.7-328.6 ms |
| 8 CPUs | `seq 2000000` | 0.346-0.374 MB/s | 26.3-28.3 ms | 47.1-67.3 ms |
| 8 CPUs | `cat` of a 40 MiB file | 0.505-0.619 MB/s | 76.1-82.6 ms | 164.0-221.5 ms |

Envelope across both machines (do not document a narrower band than this without
re-measuring both): `yes` 0.258-0.371 MB/s (p99 16.2-20.0 ms), `seq`
0.346-0.561 MB/s (p99 19.6-28.3 ms), `cat` 0.505-0.850 MB/s (p99 54.4-82.6 ms).
Automated tests cover the `yes` flood (p99 < 50 ms); a denser `cat`-style flood
was flaky under CPU load, so it stays as a measurement-script workload only.

Update these ranges when changing the reader or render path (re-run with
`--runs 3` on each class of machine you care about and widen the table).

### PtyTerminal lifecycle caveats (documented, not changed)

- If the Curupira host is killed with `SIGKILL`, the widget's `atexit` / `on_unmount`
  cleanup does not run. A child that ignores `SIGHUP`, and any grandchildren, can
  survive and be reparented (typically to PID 1).
- Grandchildren that call `setsid` leave the child's process group; force-shutdown
  only signals the child's process group, so those session leaders survive until a
  clean host close or an external kill.
- `_shutdown_child` (unmount / restart / atexit) may block the event loop for up to
  about 0.1 s (`_KILL_GRACE_SECONDS`) between `SIGHUP` and `SIGKILL` while polling
  `waitpid`.
- Outbound PTY writes buffer on `EAGAIN` and retry via `loop.add_writer` (bounded to
  1 MiB); older builds discarded the remainder of the buffer on `EAGAIN`.

## Dependency audits

`pip-audit` runs in CI against the synced development environment:

```bash
uv run pip-audit
```

Investigate every finding: upgrade the affected constraint in `pyproject.toml`,
re-sync the lockfile, and re-run the full verification suite. If a finding is not
exploitable in this project (for example, a dev-only tool with no network path to
untrusted input), document the reason in the pull request instead of adding a
permanent ignore.

## Releases

Versioning is `MAJOR.MINOR.PATCH`. The single version source is
`src/curupira/_version.py`; the build backend reads it, and the CLI reports it.
Built wheels and sdists use that string as-is, so the Git tag and the file match
character for character after the tag's leading `v`.

### Release-candidate checklist

Run this checklist before tagging either a `.devN` rehearsal or a stable cut.
`publish.yml` publishes tags that contain `.dev` to real PyPI (environment `pypi`),
so the checklist must pass before any tag is pushed. A maintainer decides when to tag;
do not tag, publish, or release from automation that skips these steps.

1. Set `__version__` in `src/curupira/_version.py` and move `CHANGELOG.md` entries as
   appropriate for the cut.
2. Run the full verification suite from [AGENTS.md](AGENTS.md) (`pytest`, Ruff, Pyrefly,
   example-config validate, and docs build when docs changed).
3. Build and gate packaging metadata:

```bash
uv run --no-sync python scripts/check_packaged_readme.py
```

4. Inspect the packaged long description that PyPI will render (for example
   `tar -xOf dist/curupira-*.tar.gz '*/PKG-INFO' | sed -n '/^$/,$p'` or the
   `METADATA` payload inside the wheel) and confirm it says Curupira, not OpsCli.
5. Wait for CI green on that commit, then a maintainer tags and pushes
   `vX.Y.Z.devN` or `vX.Y.Z`.

### PyPI development rehearsal

Tag `vX.Y.Z.devN` publishes package `X.Y.Z.devN` (PEP 440) to PyPI. Bump the version
in `src/curupira/_version.py`, commit that change, and tag the same commit. Tag
`v0.1.0.dev0` already exists and must not be reused. The next rehearsal is
`0.1.0.dev1` with tag `v0.1.0.dev1`.

1. Set `__version__` to the next unused `X.Y.Z.devN` and commit.
2. Wait until CI is green on that commit. The tag workflow publishes only after the
   Ubuntu CI checks (lint, type check, Python test matrix, and distribution smoke
   tests) have succeeded for the tagged SHA.
3. Tag that commit and push the tag:

```bash
git tag v0.1.0.dev1
git push origin v0.1.0.dev1
```

`publish.yml` builds a pure-Python wheel and sdist once, then install-smoke-tests that
wheel on `ubuntu-latest` with `curupira --config curupira.example.toml validate`. The
publish job checks that the distribution version equals the tag without its leading
`v` and uploads with Trusted Publishing (`id-token: write`, environment `pypi`). Use
the canonical dotted form `vX.Y.Z.devN`. PyPI keeps an uploaded file, so each
rehearsal needs a new suffix.

Tags that contain `.dev` do not open a GitHub Release (`release.yml` still skips
them). `testpypi.yml` is unchanged: the same `v*.dev*` tags, and a manual
`workflow_dispatch` from `main`, still target TestPyPI. The rehearsal that gates the
first stable publish is the real PyPI upload from `publish.yml`.

Stable `vX.Y.Z` tags keep the production path below. Issue #103 publishes `0.1.0`
from a commit whose `__version__` is `0.1.0`.

To cut a release:

1. Move the `Unreleased` entries in `CHANGELOG.md` into a new version section.
2. Bump `__version__` in `src/curupira/_version.py` to the stable `X.Y.Z` version.
3. Complete the [release-candidate checklist](#release-candidate-checklist) (verification
   suite, `scripts/check_packaged_readme.py`, inspect packaged README).
4. Rehearse with a `vX.Y.Z.devN` tag on PyPI (see above) before the first production
   publication.
5. Tag the validated commit as `vX.Y.Z` and push the tag. The `publish.yml` workflow
   publishes that version to PyPI; the `release.yml` workflow attaches the distributions
   to the matching GitHub release.

PyPI publishing uses Trusted Publishing (OIDC), so no API tokens are stored. Before
publishing, a PyPI maintainer registers this repository as a trusted publisher for the
`curupira` project with GitHub owner `caipora-labs`, repository `curupira`, workflow
filename `publish.yml`, and environment `pypi`. Development tags `vX.Y.Z.devN` use
that same publisher. `testpypi.yml` remains a separate workflow with environment
`testpypi`, workflow filename `testpypi.yml`, and audience `testpypi`. It uploads to
`https://test.pypi.org/legacy/` and smoke-tests that installation. The publish
workflow checks that the Ubuntu CI checks (lint, type check, Python test matrix,
and distribution smoke tests) succeeded for the commit
(`scripts/require_ci_checks.py`).
