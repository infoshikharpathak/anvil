# Changelog

## 0.1.1 (unreleased)

- `anvil-registry` checks its config before starting: with no file (or an
  invalid one) it prints one line saying which path it looked for and how
  to point it at a config, and exits with status 2, instead of a
  traceback. New `--config PATH` option (default: `ANVIL_CONFIG`, else
  `./anvil.yaml`).
- Starting the app another way (`uvicorn anvil.registry.app:app`) without a
  config raises `ConfigError` naming `ANVIL_CONFIG`, not `FileNotFoundError`.
- The registry's FastAPI version is the installed package version.

## 0.1.0 (2026-11-23)

First release on PyPI, as `garrison-anvil` (import `anvil`), one of the
Garrison family of packages.

- **License:** Apache-2.0 (was MIT).
- **Packaging:** the registry moved from a top-level `registry` package to
  `anvil.registry`, so the wheel installs only `anvil`. Run it with
  `anvil-registry` (or `uvicorn anvil.registry.app:app`); its templates and
  static files ship in the wheel.

- **Policies:** allowed tools, sequence rules (`requires`, `requires_any`),
  strictness and trust per agent, checked on every tool call by
  `Anvil.execute`. A blocked call returns a corrective response the agent
  can act on (`CorrectionInfo`: `required_actions`, `required_any`) instead
  of raising.
- **Apps and grants:** `app.tool` and `app.agent` IDs, app boundaries
  (`CROSS_APP_DENIED`), grants for cross-app tools and delegations, and
  callers bound to an app.
- **Credentials:** app credentials injected into upstream calls and never
  shown to the model; per-tool `redact_fields` strip secrets from tool
  responses; `execute(host_headers=...)` for extra upstream headers.
- **Agent checks:** `check_agent_input`, `check_agent_output` (with a
  deterministic OTP guard and a labeled classifier that blocks or flags) and
  `check_handoff`.
- **Injection scanning:** regex, LLM or hybrid, with a pluggable async
  classifier that is told where the scanned text came from (`ScanContext`).
- **Tool responses:** parameters checked against their schema, JSON array
  responses accepted, withheld results for the host only.
- **Traces:** `trace_id` on `execute()`, recorded with every call.
- **Registry** (`pip install "garrison-anvil[registry]"`): FastAPI service for
  policies, tools and traces, per-app hashed API keys, an admin key for the
  UI and policy writes, and a server-rendered UI.

Anvil governs the agents of [Garrison](https://github.com/infoshikharpathak/garrison)
(renamed from Bastion before its first release).
