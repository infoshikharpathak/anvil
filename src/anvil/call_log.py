"""Per-session, per-agent in-memory call history.

Used by PolicyEngine to check prerequisites (including match_fields), and by
TraceBuffer as the source of truth for the session trace.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .models import CallResult, RecordedCall


@dataclass
class _SessionEntry:
    calls: list[RecordedCall] = field(default_factory=list)
    last_touched: float = field(default_factory=time.monotonic)


class CallLog:
    def __init__(self, session_ttl: int = 3600) -> None:
        self._session_ttl = session_ttl
        # keyed by (session_id, agent_id)
        self._store: dict[tuple[str, str], _SessionEntry] = {}

    def record(self, session_id: str, call: RecordedCall) -> None:
        key = (session_id, call.agent)
        entry = self._store.setdefault(key, _SessionEntry())
        entry.calls.append(call)
        entry.last_touched = time.monotonic()

    def calls_for(self, session_id: str, agent_id: str) -> list[RecordedCall]:
        self._expire()
        entry = self._store.get((session_id, agent_id))
        return list(entry.calls) if entry else []

    def has_satisfied(
        self,
        session_id: str,
        agent_id: str,
        tool_name: str,
        match_fields: list[str],
        current_params: dict,
    ) -> bool:
        """True if `tool_name` was executed (success or response_withheld — the
        side effect happened either way) with matching params for match_fields."""
        for call in self.calls_for(session_id, agent_id):
            if call.tool != tool_name:
                continue
            if call.result not in (CallResult.SUCCESS, CallResult.RESPONSE_WITHHELD):
                continue
            if all(call.params.get(f) == current_params.get(f) for f in match_fields):
                return True
        return False

    def history_names(self, session_id: str, agent_id: str) -> list[str]:
        return [
            c.tool
            for c in self.calls_for(session_id, agent_id)
            if c.result in (CallResult.SUCCESS, CallResult.RESPONSE_WITHHELD)
        ]

    def clear_session(self, session_id: str) -> list[RecordedCall]:
        """Remove and return all calls for a session (across agents), for trace flush."""
        keys = [k for k in self._store if k[0] == session_id]
        calls: list[RecordedCall] = []
        for k in keys:
            calls.extend(self._store.pop(k).calls)
        calls.sort(key=lambda c: c.timestamp)
        return calls

    def _expire(self) -> None:
        cutoff = time.monotonic() - self._session_ttl
        stale = [k for k, v in self._store.items() if v.last_touched < cutoff]
        for k in stale:
            del self._store[k]
