import json

import pytest

from anvil.call_log import CallLog
from anvil.models import CallResult, RecordedCall, ViolationType
from anvil.trace import TraceBuffer


def make_buffer(tmp_path, session_id="s1"):
    call_log = CallLog()
    call_log.record(
        session_id,
        RecordedCall(tool="verify_account", agent="payment_agent", params={"account_id": "A"}, result=CallResult.SUCCESS),
    )
    call_log.record(
        session_id,
        RecordedCall(
            tool="send_payment",
            agent="payment_agent",
            params={"account_id": "B"},
            result=CallResult.BLOCKED,
            violation=ViolationType.MISSING_PREREQUISITE,
            correction_message="verify_account is required before send_payment",
        ),
    )
    buffer = TraceBuffer(session_id, "strict-payments", call_log, fallback_dir=str(tmp_path))
    return buffer, call_log


def test_build_payload_summary_counts(tmp_path):
    buffer, _ = make_buffer(tmp_path)
    payload = buffer.build_payload()
    assert payload["session_id"] == "s1"
    assert payload["summary"]["total_calls"] == 2
    assert payload["summary"]["successful"] == 1
    assert payload["summary"]["blocked"] == 1
    assert payload["summary"]["violations_by_type"]["missing_prerequisite"] == 1


def test_build_payload_clears_call_log(tmp_path):
    buffer, call_log = make_buffer(tmp_path)
    buffer.build_payload()
    assert call_log.calls_for("s1", "payment_agent") == []


@pytest.mark.asyncio
async def test_flush_falls_back_to_local_file_when_registry_unreachable(tmp_path):
    buffer, _ = make_buffer(tmp_path)
    sent = await buffer.flush(registry_url="http://localhost:1")  # nothing listening
    assert sent is False
    fallback_file = tmp_path / "s1.json"
    assert fallback_file.exists()
    data = json.loads(fallback_file.read_text())
    assert data["session_id"] == "s1"


@pytest.mark.asyncio
async def test_flush_with_no_registry_writes_fallback(tmp_path):
    buffer, _ = make_buffer(tmp_path)
    sent = await buffer.flush(registry_url=None)
    assert sent is False
    assert (tmp_path / "s1.json").exists()
