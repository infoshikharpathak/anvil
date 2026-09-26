# Anvil

Framework-agnostic tool governance for agentic AI pipelines. Enforces execution policy, validates tool responses, and keeps agents on track — without crashing the pipeline.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Anvil Registry (FastAPI service)                    │
│  - Policy storage & management                       │
│  - Trace collection & querying                       │
│  - Tool schema export (openai / anthropic / raw)     │
│  - Orphan session detection                          │
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
uvicorn registry.app:app --port 8100

# Terminal 3: demo
python examples/demo.py
```

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
| **Hard block** | `false` | empty | Not called. No recovery path (disallowed tool, unknown agent, injected request). |
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

Config is validated at load time and Anvil **refuses to start** on: references to unregistered tools, `allowed_tools` mismatches, circular prerequisites, or `match_fields` not present in a tool's parameter schema.

`allowed_tools` and injection detection are enforced regardless of strictness. Only sequence/schema violations respect `strictness: loose`.

## Integration with Any Framework

Anvil wraps the tool-call interface, not the orchestration loop, so it drops into LangGraph, AutoGen, CrewAI, or a raw ReAct loop identically:

```python
anvil = await Anvil.from_registry("http://localhost:8100", policy="strict-payments")
anvil.register_tool("send_payment", endpoint="http://localhost:8001/pay", trust="untrusted")

# In your framework's tool-call handler:
response = await anvil.execute(agent_id, tool_name, params)
return response.model_dump()   # feed straight back to the agent as the tool result
```

For frameworks that need tool *definitions* up front, pull them from the registry instead of hand-writing them:

```
GET /tools/send_payment/schema?format=openai
GET /tools/send_payment/schema?format=anthropic
```

## Security Model (v1)

Anvil v1 trusts the calling code to pass a correct `agent_id`. There is no authentication on the library or registry — any code using the library can claim to be any agent. **This is a deliberate v1 scope decision.** Anvil guards against agent error and misconfiguration, not adversarial agents or compromised application code. Authenticated agent identity is v2.

Injection scanning is pattern-based, not ML-based — expect false negatives on novel phrasing and occasional false positives on tools that legitimately discuss prompt injection (use `injection_scanning: false` per-tool to override).

## Why Not Just Use Prompts?

Telling the model "always verify before paying" in a system prompt degrades under compounding probability — a 98%-reliable instruction followed 20 times in a session is under 70% reliable across the session, and that's before an indirect prompt injection in a tool response tries to override it deliberately. Anvil moves the control outside the model: it can't be argued out of a rule, and every decision is logged with the exact call history that triggered it — which a prompt-only approach can't give you for an audit.
