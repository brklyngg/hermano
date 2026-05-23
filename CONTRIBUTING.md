# Contributing

Thanks for your interest. This project is a personal voice sidecar; contributions are welcome but the scope is deliberately small. Please read this whole file before opening a PR.

## Before you start

- **Issues first.** Open an issue describing the change before writing a large PR. Small fixes (typos, obvious bugs, doc tweaks) can go straight to PR.
- **Read `docs/ARCHITECTURE.md`.** It explains why the project is split into a fast narrow-toolkit path and a single slow `deep_research` path. Changes that re-route fast traffic through the slow path will be rejected.
- **No new dependencies without discussion.** The runtime intentionally stays close to `aiohttp` + `httpx` + `python-dotenv`.

## Dev setup

```bash
git clone https://github.com/brklyngg/hermano
cd hermano
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # edit OPENAI_API_KEY at minimum; leave AGENT_API_BASE=stub:// for the no-backend quickstart
python server.py           # http://127.0.0.1:8090
```

The bundled `backends/stub_agent.py` lets you exercise the full `deep_research` SSE flow without a real backend.

## Verification (must pass before opening a PR)

There is no test suite. The honest minimum:

```bash
python3 -m py_compile server.py dossier.py events.py transcripts.py auth.py voice_memory.py honcho_voice.py backends/*.py
node --check web/app.js
```

CI runs the same checks on every PR (see `.github/workflows/ci.yml`).

For changes that touch the voice flow, exercise it manually: start a call, ask a question that exercises any tool you changed, watch `logs/calls/<conv_id>.ndjson` for the dispatched call and reply.

## Commits

- **Conventional commits.** `feat:`, `fix:`, `refactor:`, `docs:`, `chore:`. Optional scope: `feat(dossier): …`.
- One logical change per commit. PRs with multiple unrelated commits will be asked to split.
- Commit message body should explain the **why** when it's not obvious from the diff.

## Pull requests

- Title: short, conventional-commit style.
- Body: what changed, why, how to verify. The PR template in `.github/pull_request_template.md` is the canonical checklist.
- Keep PRs scoped. A 50-line PR gets reviewed; a 500-line PR sits.

## Style

- **Python:** TypeScript-ish discipline — prefer `const`-equivalent immutability, early returns, functions under 50 lines, no broad `except`.
- **JS:** Vanilla, no build step. Match the existing patterns in `web/app.js`.
- **Comments:** explain *why*, not *what*. The code says what.
- **No emojis in code, commits, or comments.**

## Out of scope

- Reformatting passes that don't change behavior.
- Adding test frameworks or large refactors without a prior issue.
- Changes that bundle "while I was in there" cleanups with the actual fix.

## Questions

Open a [Discussion](https://github.com/brklyngg/hermano/discussions) or a draft PR with a `[help wanted]` prefix.
