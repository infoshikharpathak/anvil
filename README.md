# Anvil

[![CI](https://github.com/infoshikharpathak/anvil/actions/workflows/ci.yml/badge.svg)](https://github.com/infoshikharpathak/anvil/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

Framework-agnostic tool governance for agentic AI pipelines. Enforces execution policy, validates tool responses, and keeps agents on track — without crashing the pipeline.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Anvil Registry (FastAPI service)                    │
│  - Policy storage & management                       │
│  - Trace collection & querying                       │
│  - Tool schema export (openai / anthropic / raw)     │
│  - Orphan session detection                          │
│  - Server-rendered UI (dashboard/policies/activity)  │
└──────────┬──────────────────────────┬────────────────┘
           │ 1. Fetch policy bundle   │ 3. Flush session trace
           │    (startup, one call)   │    (session end, one call)
           ▼                          │
┌──────────┴────────────────────────────────────────────┐
│  Anvil Library (in-process, pip install)               │
│  - PolicyEngine (local enforcement, no network hop)    │
│  - CallLog (per-session, per-agent call history)       │
│  - Validators (injection scan, schema check)           │
│  - Trace buffer (accumulates, flushes on end)          │
└──────────┬──────────────────────────────────────────────┘
           │ 2. Direct tool calls (one hop)
           ▼
┌─────────────────────────────────────────────────────┐
│  Real Tools (HTTP endpoints)                          │
└─────────────────────────────────────────────────────┘
```

Enforcement happens in-process, in the library. If the registry is down mid-pipeline, an already-initialized `Anvil` instance keeps running on its cached policy — the registry is only in the critical path at startup (fetch) and shutdown (trace flush).

## Quick Start

```bash
pip install -e ".[dev]"

# Terminal 1: mock tools
python examples/mock_tools.py

# Terminal 2: registry
ANVIL_CONFIG=anvil.yaml uvicorn registry.app:app --port 8100

# Terminal 3: demo
python examples/demo.py
```

Then open **http://localhost:8100/** for the registry UI (dashboard, policies, tools, activity, orphans — see below).

Or skip the registry entirely for local dev:

```python
from anvil import Anvil

anvil = Anvil.from_config("anvil.yaml", policy="strict-payments")
```

## The Corrective Response Pattern

When an agent violates policy, Anvil never raises. `execute()` always returns a `ToolResponse` — a normal return value the agent treats like any other tool output.

```python
response = await anvil.execute(
    agent_id="payment_agent",
    tool_name="send_payment",
    params={"account_id": "A", "amount": 100},
)

if not response.success and response.correction:
    print(response.correction.message)
    # "verify_account, get_balance required before 'send_payment'.
    #  Please call verify_account, get_balance first with matching account_id."
```

Three distinct outcomes on a violation:

| Outcome | `tool_executed` | `required_actions` | Meaning |
|---|---|---|---|
| **Correction** | `false` | non-empty | Not called. Agent has a recovery path. |
| **Hard block** | `false` | empty | Not called. No recovery path (disallowed tool, unknown agent, cross-app call without a grant, injected request). |
| **Response withheld** | `true` | empty | **Was called.** The side effect already happened; the response was flagged (injection or schema violation on an untrusted tool) and withheld. The agent must not retry. |

## Config Reference (`anvil.yaml`)

```yaml
tools:
  send_payment:
    endpoint: "http://localhost:8001/pay"
    method: POST
    trust: untrusted            # untrusted responses get schema-validated + injection-scanned
    timeout: 10
    parameters: {...}           # JSON Schema, exported via /tools/{name}/schema
    response_schema:
      required_fields: ["transaction_id", "status", "amount"]

policies:
  strict-payments:
    agents:
      payment_agent:
        allowed_tools: [verify_account, send_payment, get_balance]
        sequences:
          - before: send_payment
            requires: [verify_account, get_balance]
            match_fields: [account_id]   # prereqs must be for the SAME account
        strictness: strict                # strict = block; loose = warn + allow
        trust_overrides:
          get_balance: untrusted

settings:
  injection_scanning: true
  default_strictness: strict
  default_trust: untrusted
  session_ttl: 3600
  trace_fallback_dir: "./anvil_traces"
```

### Apps, grants, and credentials

Tools and agents belong to an **app**. Declare apps under `apps:`, give each one a `base_url` and optional `auth`, and put its tools under it with a `path`:

```yaml
apps:
  ironclad:
    base_url: "http://localhost:8001"
    auth:                              # optional
      header: Authorization
      secret_env: IRONCLAD_API_TOKEN   # env var name only, never the secret
      prefix: "Bearer "
    api_keys:                          # registry callers for this app (see below)
      ironclad-agent-service: "<sha256 hex of the key>"
    tools:
      get_accounts:
        path: /accounts
        method: GET
  notify:
    base_url: "http://localhost:8002"
    tools:
      send_email: { path: /email }

grants:
  - tool: notify.send_email           # let ironclad agents use this notify tool
    to_apps: [ironclad]
  # - agent: notify.dispatcher        # or let them hand off to a notify agent
  #   to_apps: [ironclad]

policies:
  ironclad-banking:
    agents:
      ironclad.banking_agent:          # agent IDs are "app.agent"
        allowed_tools:
          - get_accounts               # bare names resolve within the agent's app
          - notify.send_email          # another app's tool: needs a grant
```

- **IDs.** Tools are `app.tool` and agents are `app.agent`. Top-level `tools:` with full `endpoint`s still work and belong to the `default` app, whose IDs stay bare, so existing configs are unchanged.
- **Boundaries.** An agent may only use its own app's tools and delegate to its own app's agents, unless a grant says otherwise. Config load rejects ungranted cross-app references, and `execute()` / `check_handoff()` enforce the same rule at call time as `cross_app_denied`. That is always a hard block, whatever the strictness. A grant lifts only the app boundary: the tool still has to be in `allowed_tools`.
- **Credentials.** When forwarding to a tool, Anvil reads the tool's app secret from `secret_env` and sends it in `header`. If the variable is unset, the call fails closed as `upstream_error`. The secret is redacted from response data and error messages, so it never reaches the agent, traces, or audit logs.
- **Tool schemas.** `GET /tools/{id}/schema?format=openai|anthropic` exports the short name (`get_accounts`), because those APIs don't allow dots in function names.

Config is validated at load time and Anvil **refuses to start** on: references to unregistered tools, `allowed_tools` mismatches, cross-app references without a grant, agents or tools in undeclared apps, plaintext API keys, circular prerequisites, or `match_fields` not present in a tool's parameter schema.

`allowed_tools` and injection detection are enforced regardless of strictness. Only sequence/schema violations respect `strictness: loose`.

### Agent checks (multi-agent pipelines)

Add `allowed_delegations: [other_agent, ...]` to an agent to control which agents it may hand work to. It is enforced like `allowed_tools`: a hard block, whatever the strictness.

```python
await anvil.check_agent_input("planner", user_message, trace_id=req_id)
await anvil.check_agent_output("researcher", draft, trace_id=req_id)
await anvil.check_handoff("planner", "researcher", payload=task, trace_id=req_id)
```

Each returns `AgentCheckResult(allowed, correction)` and never raises. Input, output, and handoff payloads go through the same injection scanner as tool calls. Checks are recorded in the session trace with a `kind` field. They do not count as tool prerequisites.

## Integration with Any Framework

Anvil wraps the tool-call interface, not the orchestration loop, so it drops into LangGraph, AutoGen, CrewAI, or a raw ReAct loop identically:

```python
anvil = await Anvil.from_registry("http://localhost:8100", policy="strict-payments")
anvil.register_tool("send_payment", endpoint="http://localhost:8001/pay", trust="untrusted")

# In your framework's tool-call handler:
response = await anvil.execute(agent_id, tool_name, params)
return response.model_dump()   # feed straight back to the agent as the tool result
```

### Binding a caller to an app

```python
# Library mode: this instance only accepts agents of the ironclad app.
anvil = Anvil.from_config("anvil.yaml", policy="ironclad-banking", app="ironclad")
await anvil.execute("banking_agent", "get_accounts", params)  # -> ironclad.banking_agent
await anvil.execute("notify.dispatcher", "send_email", params)  # -> cross_app_denied

# Registry mode: the API key decides which app's policies and tools you get.
anvil = await Anvil.from_registry(
    "http://localhost:8100", policy="ironclad-banking", app="ironclad",
    client_id="ironclad-agent-service", client_key=os.environ["ANVIL_REGISTRY_KEY"],
)
```

The registry stores only SHA-256 digests (`python -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" <key>`). The client sends `X-Client-Id` plus `Authorization: Bearer <key>`. Once any app has `api_keys`, every JSON API route requires a valid key and is scoped to that app:
- **Policies:** only those whose agents all belong to the app. Anything else returns 404.
- **Tools:** the app's own tools plus tools granted to it.
- **Traces and checkouts:** only the app's own.

Policy writes must also stay inside the caller's app. With no keys configured, the registry is open, as before.

For frameworks that need tool *definitions* up front, pull them from the registry instead of hand-writing them:

```
GET /tools/send_payment/schema?format=openai
GET /tools/send_payment/schema?format=anthropic
```

## Registry UI (v2)

Server-rendered pages served directly by the registry app — Jinja2 templates, no build step, no separate frontend process. Vanilla JS handles the interactive bits (saving a policy, polling for activity) by calling the same JSON API described above.

| Page | Path | What it does |
|---|---|---|
| Dashboard | `/` | Policy/tool/session/trace/orphan counts, recent traces |
| Policies | `/ui/policies` | List all policies |
| Policy detail | `/ui/policies/{name}` | Agent summary + a raw-JSON editor that saves via the existing `PUT /policies/{name}` |
| New policy | `/ui/policies/new` | Register a new pipeline (`POST /policies`) — this is how you add a policy for a new agent/pipeline |
| Tools | `/ui/tools` | Registered tools with one-click schema export links (openai/anthropic/raw) |
| Activity | `/ui/activity` | Active sessions + recent completed traces, polled every 3s |
| Orphans | `/ui/orphans` | Sessions that checked out a policy but never sent a trace |
| Trace detail | `/ui/traces/{session_id}` | Full call timeline for one session |

**"Activity" is not a call-by-call stream.** The library only talks to the registry at session start (policy checkout) and session end (trace flush) — that's the whole point of the one-hop design (see Architecture above). So the activity feed shows sessions currently checked out plus completed traces, polled periodically; it will not show individual tool calls as they happen mid-session. The page says so.

Creating or editing a policy through the UI (or the raw API) runs the same fail-fast validation the YAML loader applies — dangling tool references and circular prerequisites are rejected with a 422, not silently accepted.

The UI pages are not authenticated, even when API keys are configured. Keep the registry UI on a trusted network. See Security Model below.

## Security Model

**Apps are the trust boundary.** A library instance bound with `app=` can't act as another app's agent. A registry caller with an app API key can only read and write that app's policies, tools and traces. Within one app, Anvil still trusts the calling code to pass the right `agent_id`: any code holding an app's instance or key can claim to be any of that app's agents. Anvil guards against agent error, misconfiguration and one app overreaching into another. It does not guard against a compromised app process.

Known gaps: the registry UI pages are unauthenticated, and with no `api_keys` configured the registry API is fully open (the pre-apps behavior).

Injection scanning is pattern-based, not ML-based — expect false negatives on novel phrasing and occasional false positives on tools that legitimately discuss prompt injection (use `injection_scanning: false` per-tool to override).

## Why Not Just Use Prompts?

Telling the model "always verify before paying" in a system prompt degrades under compounding probability — a 98%-reliable instruction followed 20 times in a session is under 70% reliable across the session, and that's before an indirect prompt injection in a tool response tries to override it deliberately. Anvil moves the control outside the model: it can't be argued out of a rule, and every decision is logged with the exact call history that triggered it — which a prompt-only approach can't give you for an audit.
