# Repository Guidelines

## Source Map

- `bot.py` initializes the Discord client, shared state, error handling, and automatic cog loading.
- `cogs/` contains features. Small features are modules such as `summary.py` and `role_sync.py`; larger features use packages such as `appdayi/`, `mention/`, `pending_reviewer/`, `quick_punish/`, `broadcast/`, `tagger/`, and `recognize_url/`. Keep extension setup in `setup()` and cross-feature helpers in focused modules under `cogs/shared/`.
- `paths.py` is the canonical filesystem map. `data/` holds durable databases, prompts, and configuration; `runtime/` holds rebuildable logs, temporary files, archives, and RAG data. Do not hard-code alternate paths.
- `tests/` mirrors feature behavior. Put shared fakes and temporary-workspace helpers in `tests/conftest.py`; name files `tests/test_<area>.py`. `.env.example` and `mention/*_example.json` are configuration examples.

## Setup and Common Commands

Work inside a Python 3.12+ virtual environment, as required by `uv.lock`; `.python-version` pins 3.12, and `uv` selects it automatically.

- `uv venv` — create `.venv` with the pinned interpreter.
- `uv pip install -r requirements.txt` — install runtime dependencies.
- `uv pip install pytest ruff` — install test and lint tools.
- `uv run python bot.py` — start the bot with `.env` values.
- `uv run python -m pytest` — run the full suite.
- `uv run python -m pytest tests/test_utils.py -q` — run one focused file.
- `uv run ruff check .` — run configured E, F, and Bugbear checks.

## Coding Style and Naming

Use four-space indentation, `snake_case` for functions and modules, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Type new public helpers and keep blocking file or database work off the async event loop. Ruff ignores line length, but keep statements readable. Write code comments in English.

Beyond formatting, apply these engineering principles:

- Start from first principles: before choosing an approach or an edit location, confirm the intended outcome, platform constraints, existing external contracts, evidence strength, and the conditions that must keep holding. When principles conflict, return to the original goal and the evidence to justify the trade-off; when new facts invalidate an earlier judgment, revise the design instead of defending it.
- Prefer general mechanisms to special cases. Check whether the needs share the same semantics or invariant, then extend the common mechanism; keep genuine differences explicit, and do not build a universal abstraction before a shared need exists.
- Fail fast and avoid silent degradation. When a constraint fails or a result cannot be confirmed, stop the related operation and propagate an error with context. Expected recovery paths must be semantically explicit and visible to callers; never guess success for an unknown state.
- Maintainability first. Code and docs should let a later maintainer understand, verify, and change them without relying on unwritten history; keep responsibilities, data flow, and error paths clear. New modules, abstractions, or complexity must solve a real problem, and unrelated responsibilities must not pile into a single file.
- Keep it simple: choose the simplest solution that fully solves the current problem, and follow the 60/95 rule — cover 95% of scenarios with 60% of the complexity, deferring extreme cases that lack real evidence until they actually occur. This does not excuse skipping existing external contracts or risk-proportional verification, nor spending effort on unrelated fallbacks, tests, or logic validation during rapid iteration.

## Testing Rules

Add regression tests for behavior changes. Follow the existing `unittest.TestCase` style; use `unittest.IsolatedAsyncioTestCase` or `asyncio.run()` for async code. Mock Discord, OpenAI, and network boundaries with `unittest.mock`. Use `TemporaryDirectory` or `temporary_workdir()` for files and SQLite databases. Tests must not depend on live credentials, servers, order, or existing `data/`/`runtime/` contents. No formal coverage threshold exists: cover the changed success and realistic failure paths, and apply the 60/95 rule to edge cases instead of chasing speculative boundaries. Do not add tests for unchanged code or hypothetical fallbacks the change does not introduce. Run the focused file during development and the full suite before submission.

## Commits and Pull Requests

Use the repository's Conventional Commit pattern, such as `feat(appdayi): add prompt caching`, `fix(mention): handle empty replies`, or `chore: update docs`. Keep commits scoped. Pull requests should explain user-visible effects, identify affected cogs, link issues, and report test commands. Include screenshots or redacted logs for Discord UI or message-output changes.

## Language Rules

Write code comments in English; use Simplified Chinese when communicating with users in issues, PRs, or assistant responses; use English for all communication and instructions with subagents.
