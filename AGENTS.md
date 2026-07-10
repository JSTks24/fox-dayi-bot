# Repository Guidelines

## Source Map

- `bot.py` initializes the Discord client, shared state, error handling, and automatic cog loading.
- `cogs/` contains features. Small features are modules such as `summary.py` and `role_sync.py`; larger features use packages such as `appdayi/`, `mention/`, `pending_reviewer/`, `quick_punish/`, `broadcast/`, `tagger/`, and `recognize_url/`. Keep extension setup in `setup()` and cross-feature helpers in focused modules under `cogs/shared/`.
- `paths.py` is the canonical filesystem map. `data/` holds durable databases, prompts, and configuration; `runtime/` holds rebuildable logs, temporary files, archives, and RAG data. Do not hard-code alternate paths.
- `tests/` mirrors feature behavior. Put shared fakes and temporary-workspace helpers in `tests/conftest.py`; name files `tests/test_<area>.py`. `.env.example` and `mention/*_example.json` are configuration examples.

## Setup and Common Commands

Work inside a Python 3.12+ virtual environment, as required by `uv.lock`.

- `python -m pip install -r requirements.txt` — install runtime dependencies.
- `python -m pip install pytest ruff` — install test and lint tools.
- `python bot.py` — start the bot with `.env` values.
- `python -m pytest` — run the full suite.
- `python -m pytest tests/test_utils.py -q` — run one focused file.
- `ruff check .` — run configured E, F, and Bugbear checks.

## Coding Style and Naming

Use four-space indentation, `snake_case` for functions and modules, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Type new public helpers and keep blocking file or database work off the async event loop. Ruff ignores line length, but keep statements readable. Write code comments in English.

## Testing Rules

Add regression tests for behavior changes. Follow the existing `unittest.TestCase` style; use `unittest.IsolatedAsyncioTestCase` or `asyncio.run()` for async code. Mock Discord, OpenAI, and network boundaries with `unittest.mock`. Use `TemporaryDirectory` or `temporary_workdir()` for files and SQLite databases. Tests must not depend on live credentials, servers, order, or existing `data/`/`runtime/` contents. No formal coverage threshold exists; cover success, error, and boundary cases. Run the focused file during development and the full suite before submission.

## Commits and Pull Requests

Use the repository's Conventional Commit pattern, such as `feat(appdayi): add prompt caching`, `fix(mention): handle empty replies`, or `chore: update docs`. Keep commits scoped. Pull requests should explain user-visible effects, identify affected cogs, link issues, and report test commands. Include screenshots or redacted logs for Discord UI or message-output changes.

## Language Rules

Write code comments in English; use Simplified Chinese when communicating with users in issues, PRs, or assistant responses; use English for all communication and instructions with subagents.
