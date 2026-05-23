## What

<!-- One or two sentences. -->

## Why

<!-- Motivation. Link to an issue if there is one. -->

## How to verify

<!-- The exact commands or steps a reviewer should run. -->

## Checklist

- [ ] `python -m py_compile server.py dossier.py events.py transcripts.py auth.py voice_memory.py honcho_voice.py backends/*.py` passes
- [ ] `node --check web/app.js` passes
- [ ] If this touches the voice flow, I exercised it manually and checked `logs/calls/<conv_id>.ndjson`
- [ ] Commit messages follow conventional-commit style
- [ ] No new runtime dependencies (or, if there are, they were discussed in an issue first)
