# Security Policy

## Reporting a Vulnerability

**Please report security issues privately.** Open public GitHub issues only for non-security bugs.

To report a vulnerability, email **gary@crunchy.tools** with:

- A clear description of the issue and its impact
- Steps to reproduce (or a proof-of-concept)
- Affected commit hash or release if known
- Your preferred attribution (or request to remain anonymous)

You should expect an acknowledgement within a few business days. This is a small open-source project maintained on a best-effort basis — there is no formal SLA, but credible reports will be triaged and fixed as quickly as practical.

## Scope

In scope:

- The sidecar (`server.py`, `backends/`, `auth.py`, `dossier.py`, `voice_memory.py`, `honcho_voice.py`)
- The browser client (`web/`)
- Configuration that ships in `.env.example`

Out of scope:

- Reports that assume the attacker already has local access to the machine running the sidecar
- Issues in third-party services this project integrates with (OpenAI, Supabase, Google Workspace) — please report those to the upstream vendor
- Bugs caused by changing the default configuration on a personal deployment

## Threat Model

This project is built for **personal, local use** by a single operator. The defaults reflect that:

- The HTTP listener binds to loopback only via `VOICE_ALLOWED_CIDR=127.0.0.1/32,::1/128`
- Secrets are read from the local environment or from sibling files with restrictive permissions
- The browser client never receives API keys; all third-party calls are server-side

Running outside that threat model (multi-user, multi-tenant, public network) is not a supported configuration. If you adapt the project for one of those settings, you are responsible for adding the controls that fit it.

## Roadmap

Planned improvements are tracked via GitHub issues labeled `security`.
