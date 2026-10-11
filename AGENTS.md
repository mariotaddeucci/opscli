# AGENTS.md

Instructions for coding agents working in this repository. Humans should start with
[CONTRIBUTING.md](CONTRIBUTING.md); this file is the short operating manual both share.
Explicit instructions in a task or prompt override this file.

## Project

Curupira dispatches GitHub, Azure DevOps, Trello, and cron tasks to local coding-agent CLIs
(OpenCode, Codex, Claude Code, Cursor, Gemini CLI, GitHub Copilot CLI, Kilo CLI, pi, and
Qwen Code). The package, import, and main console script are `curupira`; `curu` is the
short alias.

Stack: Python 3.11+ (pure Python, `src/` layout), [uv](https://docs.astral.sh/uv/),
Pydantic v2 and pydantic-settings (TOML configuration), Typer (CLI), Textual (TUI),
asyncio subprocesses, httpx (GitHub GraphQL), SQLite, OpenTelemetry, pytest, Ruff,
Pyrefly (strict), and [prek](https://prek.j178.dev/) for Git hooks.

## Commands

Run everything from the repository root. CI runs the same commands and every one must pass.

```bash
uv sync --dev                                              # install the project and dev tools
uv run --no-sync prek install                              # Git pre-commit hooks (prek)
uv run --no-sync prek run --all-files                      # run all hooks on demand
uv run --no-sync pytest                                    # full test suite
uv run --no-sync pytest tests/test_cli.py -k example      # one file or test
uv run --no-sync pytest --cov                              # branch coverage, must stay >= 85%
uv run --no-sync ruff check .                              # lint (add --fix for safe fixes)
uv run --no-sync ruff format --check .                     # formatting
uv run --no-sync pyrefly check                             # strict type check
uv run --no-sync curupira --config curupira.example.toml validate
uv run --no-sync python scripts/check_packaged_readme.py   # build, OpsCli branding guard, twine
```

Git hooks are managed by [`prek`](https://prek.j178.dev/) via `.pre-commit-config.yaml`.
Ruff and Pyrefly hooks invoke `uv run --no-sync` so they match the locked project
environment (do not pin a separate Ruff wheel in the hook config).

Documentation: `uv sync --group docs`, then `uv run mkdocs build --strict`.

## Repository map

Each layer owns its contract in `base.py`; implementations depend on the contract, never
on each other.

| Path | Responsibility |
| --- | --- |
| `src/curupira/models/` | Pydantic contracts: configuration, CLI profiles, tasks, CLI payloads. |
| `src/curupira/config.py` | Loads and resolves the TOML configuration (`ApplicationSettings`). |
| `src/curupira/config_reload.py` | Watches the config file during `run --watch` / `tui` and reloads after in-flight tasks drain. |
| `src/curupira/hooks.py`, `manager.py` | Pluggy hookspecs (`curupira_coding_agent_adapters`, `curupira_triggers`) and the manager that registers built-in providers. |
| `src/curupira/tasks/` | Task discovery contracts: `Trigger`, `TaskSource`, `TaskFeed`, registry, and shared feed helpers. Built-in triggers live under `providers/` and register through Pluggy; compatibility re-exports keep `curupira.tasks.<name>` import paths working. |
| `src/curupira/plugins.py` | Stable plugin API and `curupira.triggers`/`curupira.agents` entry-point discovery; plugins import only this module. |
| `src/curupira/vcs/` | Repository checkout and worktrees: `VersionControl`. |
| `src/curupira/providers/` | Built-in providers: one package per integration (`providers/<name>/provider.py`), registered through Pluggy. A provider may contribute coding-agent adapters, triggers, or both. |
| `src/curupira/agents/` | Shared coding-agent contract (`CodingAgentCliAdapter`, including optional `auto_model` and `interactive_launch`), interactive launch specs, registry, `resolve_assistant_model`, and `create_cli_adapter`; compatibility re-exports of built-in coding-agent providers. |
| `src/curupira/clients/` | GitHub GraphQL/`gh auth token`, `az`/Trello wrappers, and `AsyncProcessRunner` (the only place that starts processes). |
| `src/curupira/storage/` | SQLite persistence for sessions and cron state. |
| `src/curupira/cli.py`, `tui/` | Typer commands and the Textual dashboard. `tui/pty_terminal.py` is the reusable `PtyTerminal` PTY widget (not wired into the layout yet). |
| `tests/` | Mirrors `src/` (`tests/providers/<name>/` for each provider); shared fakes in `tests/fakes.py`, builders in `tests/helpers.py`. |
| `docs/en/` | Canonical documentation; `docs/pt/` and `docs/es/` are translations. One page per provider in `docs/en/providers/`. |
| `main.py` | MkDocs macros; the provider table and install list come from the agent registry. |

## Code style

Follow the Zen of Python (`python -m this`): explicit, flat, and simple beats clever.

- Every function and method is fully typed; Pyrefly runs in strict mode. Avoid `Any`
  (test builders may accept `**overrides: Any`).
- Data crossing a boundary (TOML, CLI JSON, SQLite rows) is a Pydantic model, never a
  loose `dict`. Configuration models extend `ValidatedModel` (frozen, `extra="forbid"`);
  external CLI payloads are frozen models that ignore unknown fields.
- Express a validation rule once, as a reusable `Annotated` type in `models/base.py`,
  and choose between models with a `Literal` discriminator instead of `if` chains.
- Public modules, classes, and functions have Google-style docstrings. Coding-agent
  profile models document each field with `Field(description=...)` (no `Attributes:`
  block); other models list their fields under `Attributes:`.
- Mark overrides with `@override`. Raise subclasses of `curupira.errors.DispatchError`
  for expected failures.
- Never add a blanket `# noqa` or `# type: ignore`; scope a suppression to one rule and
  justify it in a comment, or fix the code.

```python
class CursorCliProfile(CliProfileBase):
    """Cursor CLI profile. Set ``provider`` to ``\"cursor\"`` in TOML."""

    provider: Literal["cursor"] = Field(
        default="cursor",
        description='Discriminator identifying the Cursor CLI. Must be "cursor".',
    )
    agent: Literal["agent", "ask", "plan"] | None = Field(
        default=None,
        description=(
            "Optional Cursor execution mode. Allowed values: agent, ask, plan. When "
            "set, the adapter passes ``--mode``; when unset, that flag is omitted."
        ),
    )


CliProfile = Annotated[
    OpenCodeCliProfile | CodexCliProfile | ClaudeCodeCliProfile | CursorCliProfile,
    Field(discriminator="provider"),
]
```

## Testing

- Cover every behavior change with a test next to the matching module in `tests/`.
- Use the fakes in `tests/fakes.py`. Tests never hit the network, call real `gh`/`az`, or
  start authenticated coding agents.
- Warnings are errors (`filterwarnings = ["error"]`); fix their cause.
- Changes to `curupira.example.toml` must keep `test_example_configuration_is_valid` green.

## Security

- Start processes only through `AsyncProcessRunner` with a `CommandRequest`: an argument
  vector, never a shell string, with a timeout and bounded output.
- Pass every SQL value as a bound parameter; identifiers come from private constants.
- Validate user-supplied paths and repository names against traversal (`..`, absolute paths).
- Never commit, log, or echo secrets or tokens. Curupira does not manage authentication;
  provider CLIs own it.
- Investigate `uv run pip-audit` findings instead of ignoring them.

## Boundaries

- **Always:** run the commands above before finishing; update `CHANGELOG.md`
  (`Unreleased`), `docs/en/`, and this file in the same change when behavior, commands,
  or structure change.
- **Ask first:** adding or upgrading dependencies, editing `.github/workflows/`, and
  breaking changes to the TOML configuration schema or CLI.
- **Never:** hand-edit `uv.lock` (use `uv lock`), bump `src/curupira/_version.py` or push
  tags, weaken lint, type, or coverage gates, or edit `docs/pt/`/`docs/es/` without the
  English source.

## Git workflow

Branch from `main`, keep one logical change per commit, and open a pull request that
fills in [.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md).

## Definition of done

The pull request checklist is satisfied: tests cover the change, every command in
[Commands](#commands) passes, and user-facing documentation and the changelog are updated.
