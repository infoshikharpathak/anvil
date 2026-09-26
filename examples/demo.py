"""Simulates an agent calling tools through the Anvil library.

Run order:
    Terminal 1: python examples/mock_tools.py        # :8001
    Terminal 2: uvicorn registry.app:app --port 8100  # (after `pip install -e .`)
    Terminal 3: python examples/demo.py
"""

from __future__ import annotations

import asyncio
import json

from anvil import Anvil
from anvil.models import ToolResponse

REGISTRY_URL = "http://localhost:8100"


def show(label: str, response: ToolResponse) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(response.model_dump(), indent=2, default=str))


async def main() -> None:
    payment_agent = await Anvil.from_registry(REGISTRY_URL, policy="strict-payments")
    print(f"session_id = {payment_agent.session_id}")

    # 1. Corrective response: send_payment called with no prerequisites.
    resp = await payment_agent.execute(
        "payment_agent", "send_payment", {"account_id": "A", "amount": 50, "currency": "USD"}
    )
    show("1. Corrective response (no prerequisites)", resp)
    assert not resp.success and resp.correction.required_actions

    # 2. Happy path: verify -> balance -> payment, all for account A.
    resp = await payment_agent.execute("payment_agent", "verify_account", {"account_id": "A"})
    show("2a. verify_account(A)", resp)

    resp = await payment_agent.execute("payment_agent", "get_balance", {"account_id": "A"})
    show("2b. get_balance(A)", resp)

    resp = await payment_agent.execute(
        "payment_agent", "send_payment", {"account_id": "A", "amount": 50, "currency": "USD"}
    )
    show("2c. send_payment(A) — happy path", resp)
    assert resp.success and resp.tool_executed

    # 3. match_fields enforcement: prereqs were for A, payment now for B.
    resp = await payment_agent.execute(
        "payment_agent", "send_payment", {"account_id": "B", "amount": 25, "currency": "USD"}
    )
    show("3. match_fields enforcement (prereqs were for A, not B)", resp)
    assert not resp.success and resp.correction.violation == "missing_prerequisite"

    # 4. Disallowed tool: notification_agent tries to call send_payment.
    notification_agent = await Anvil.from_registry(REGISTRY_URL, policy="strict-payments")
    resp = await notification_agent.execute(
        "notification_agent", "send_payment", {"account_id": "A", "amount": 1, "currency": "USD"}
    )
    show("4. Disallowed tool (hard block)", resp)
    assert not resp.success and not resp.correction.required_actions

    # 5. Injection detection: response withheld, side effect already committed.
    resp = await payment_agent.execute("payment_agent", "verify_account", {"account_id": "INJECTED"})
    resp = await payment_agent.execute("payment_agent", "get_balance", {"account_id": "INJECTED"})
    resp = await payment_agent.execute(
        "payment_agent", "send_payment", {"account_id": "INJECTED", "amount": 10, "currency": "USD"}
    )
    show("5. Injection detected in response (tool_executed=True, response withheld)", resp)
    assert not resp.success and resp.tool_executed

    # 6. Session end + trace flush.
    sent_to_registry = await payment_agent.end_session()
    print(f"\n--- 6. Session trace flushed to registry: {sent_to_registry} ---")
    await notification_agent.end_session()

    import httpx

    async with httpx.AsyncClient() as client:
        trace_resp = await client.get(f"{REGISTRY_URL}/traces/{payment_agent.session_id}")
        print(json.dumps(trace_resp.json(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
