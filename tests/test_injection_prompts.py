"""Classifier prompts and ScanContext plumbing."""

import pytest

from anvil.client import Anvil
from anvil.config import load_config_dict
from anvil.injection_prompts import BASE_PROMPT, classifier_prompt
from anvil.validators import InjectionResult, ScanContext


def test_default_is_the_base_prompt_for_every_direction():
    for direction in (None, "input", "tool_request", "tool_response", "output", "handoff"):
        assert classifier_prompt(direction, {"agent": "a", "user_request": "hi"}) == BASE_PROMPT


def test_output_context_variant_only_changes_output():
    for direction in ("input", "tool_request", "tool_response", "handoff"):
        assert classifier_prompt(direction, variant="output_context") == BASE_PROMPT
    out = classifier_prompt("output", {"agent": "fooddash.ordering_agent", "user_request": "Pay for it"},
                            variant="output_context")
    assert out != BASE_PROMPT and "agent's own reply to its user" in out
    assert "Agent: fooddash.ordering_agent" in out and "The user's request: 'Pay for it'" in out
    assert out.endswith('{"injection": false}')  # answer format stays last
    with pytest.raises(ValueError):
        classifier_prompt("output", variant="clever")


RAW = {
    "apps": {"bank": {"base_url": "http://bank", "tools": {
        "memo": {"path": "/memo", "method": "GET", "trust": "untrusted",
                 "parameters": {"type": "object", "properties": {}}}}}},
    "policies": {"p": {"agents": {"bank.agent": {"allowed_tools": ["memo"]}}}},
    "settings": {"injection_scanning": True},
}


async def test_classifiers_that_take_context_get_the_direction():
    import httpx

    seen = []

    async def classifier(text, context=None):
        seen.append(context)
        return InjectionResult(detected=False)

    a = Anvil(load_config_dict(RAW), "p", app="bank", session_id="s", classifier=classifier)
    a._transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"memo": "lunch"}))
    await a.check_agent_input("agent", "hello")
    await a.execute("agent", "memo", {})
    await a.check_agent_output("agent", "Here you go.", user_request="show memo")
    await a.check_handoff("agent", "agent", "payload")
    assert [c.direction for c in seen] == ["input", "tool_request", "tool_response", "output"]
    assert seen[1] == ScanContext("tool_request", agent="bank.agent", tool="bank.memo")
    assert seen[3].user_request == "show memo"


async def test_old_style_classifiers_still_work():
    calls = []

    async def classifier(text):
        calls.append(text)
        return InjectionResult(detected=False)

    a = Anvil(load_config_dict(RAW), "p", app="bank", session_id="s", classifier=classifier)
    assert (await a.check_agent_output("agent", "fine")).allowed
    assert calls == ["fine"]
