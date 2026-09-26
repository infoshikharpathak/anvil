"""TraceBuffer — builds the session trace payload and flushes it to the
registry (or a local fallback file if the registry is unreachable)."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import httpx

from .call_log import CallLog
from .models import CallResult, RecordedCall


class TraceBuffer:
    def __init__(
        self,
        session_id: str,
        policy_name: str,
        call_log: CallLog,
        fallback_dir: str = "./anvil_traces",
    ) -> None:
        self.session_id = session_id
        self.policy_name = policy_name
        self.call_log = call_log
        self.fallback_dir = fallback_dir
        self.started_at = _iso_now()

    def build_payload(self) -> dict[str, Any]:
        calls = self.call_log.clear_session(self.session_id)
        summary = _summarize(calls)
        return {
            "session_id": self.session_id,
            "policy": self.policy_name,
            "started_at": self.started_at,
            "ended_at": _iso_now(),
            "calls": [_call_to_dict(c) for c in calls],
            "summary": summary,
        }

    async def flush(self, registry_url: str | None) -> bool:
        """Build the trace and POST it to the registry. Returns True if sent
        successfully; on any failure (or no registry configured), writes the
        trace to a local fallback file and returns False."""
        payload = self.build_payload()

        if registry_url:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(f"{registry_url}/traces", json=payload)
                    resp.raise_for_status()
                return True
            except Exception:
                pass

        self._write_fallback(payload)
        return False

    def _write_fallback(self, payload: dict[str, Any]) -> None:
        directory = Path(self.fallback_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.session_id}.json"
        path.write_text(json.dumps(payload, indent=2))


def _call_to_dict(call: RecordedCall) -> dict[str, Any]:
    d = {
        "tool": call.tool,
        "agent": call.agent,
        "params": call.params,
        "result": call.result.value,
        "timestamp": call.timestamp,
    }
    if call.latency_ms is not None:
        d["latency_ms"] = call.latency_ms
    if call.violation is not None:
        d["violation"] = call.violation.value
    if call.correction_message is not None:
        d["correction_message"] = call.correction_message
    return d


def _summarize(calls: list[RecordedCall]) -> dict[str, Any]:
    violations_by_type: dict[str, int] = {}
    successful = blocked = withheld = 0
    for c in calls:
        if c.result == CallResult.SUCCESS:
            successful += 1
        elif c.result == CallResult.BLOCKED:
            blocked += 1
        elif c.result == CallResult.RESPONSE_WITHHELD:
            withheld += 1
        if c.violation:
            violations_by_type[c.violation.value] = violations_by_type.get(c.violation.value, 0) + 1

    return {
        "total_calls": len(calls),
        "successful": successful,
        "blocked": blocked,
        "response_withheld": withheld,
        "violations_by_type": violations_by_type,
    }


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
