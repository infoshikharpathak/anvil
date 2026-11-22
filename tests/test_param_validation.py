"""Tool params are checked against the tool's declared schema before it runs."""

import json
import logging

import httpx
import pytest

from anvil import client as anvil_client
from anvil.client import Anvil
from anvil.config import load_config_dict
from anvil.models import CallResult, ViolationType
from anvil.validators import validate_params

ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "restaurant_id": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"menu_item_id": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1}},
            "required": ["menu_item_id"],
        }},
        "speed": {"type": "string", "enum": ["standard", "express"]},
    },
    "required": ["restaurant_id", "items"],
}

RAW = {
    "apps": {"food": {"base_url": "http://food", "tools": {
        "get_menu": {"path": "/restaurants/{restaurant_id}/menu", "method": "GET", "trust": "trusted",
                     "parameters": {"type": "object", "properties": {"restaurant_id": {"type": "string"}},
                                    "required": ["restaurant_id"]}},
        "place_order": {"path": "/orders", "method": "POST", "trust": "trusted", "parameters": ORDER_SCHEMA},
        "pay": {"path": "/orders/{order_id}/pay", "method": "POST", "trust": "trusted",
                "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                               "required": ["order_id"]}},
        "legacy": {"path": "/legacy", "method": "POST", "trust": "trusted"},  # no schema
    }}},
    "policies": {"p": {"agents": {"food.agent": {
        "allowed_tools": ["get_menu", "place_order", "pay", "legacy"],
        "sequences": [{"before": "place_order", "requires": ["get_menu"], "match_fields": []}],
        "strictness": "strict",
    }}}},
    "settings": {"injection_scanning": False},
}


@pytest.fixture
def anvil():
    a = Anvil(load_config_dict(RAW), "p", app="food", session_id="s")
    a.sent = []

    def upstream(request: httpx.Request) -> httpx.Response:
        a.sent.append(request)
        return httpx.Response(200, json={"ok": True})

    a._transport = httpx.MockTransport(upstream)
    return a


ITEMS = [{"menu_item_id": "item_1", "quantity": 2}]


async def test_valid_call_passes(anvil):
    await anvil.execute("agent", "get_menu", {"restaurant_id": "rest_1"})
    resp = await anvil.execute("agent", "place_order", {"restaurant_id": "rest_1", "items": ITEMS, "speed": "express"})
    assert resp.success is True
    assert json.loads(anvil.sent[-1].content) == {"restaurant_id": "rest_1", "items": ITEMS, "speed": "express"}


@pytest.mark.parametrize("params,problem", [
    ({"items": ITEMS}, "restaurant_id: required field is missing"),
    ({"restaurant_id": 7, "items": ITEMS}, "restaurant_id: expected string, got integer"),
    ({"restaurant_id": "r", "items": [{"menu_item_id": "i", "quantity": "two"}]},
     "items[0].quantity: expected integer, got string"),
    ({"restaurant_id": "r", "items": [{"menu_item_id": "i", "quantity": True}]},
     "items[0].quantity: expected integer, got boolean"),
    ({"restaurant_id": "r", "items": [{"quantity": 1}]}, "items[0].menu_item_id: required field is missing"),
    ({"restaurant_id": "r", "items": ITEMS, "speed": "teleport"}, "speed: 'teleport' is not one of 'standard', 'express'"),
    ({"restaurant_id": "r", "items": ITEMS, "tip": 5}, "tip: unknown field (not in the tool's schema)"),
    ({"restaurant_id": "r", "items": [{"menu_item_id": "i", "price": 0}]}, "items[0].price: unknown field"),
    ({"restaurant_id": "r", "items": [{"menu_item_id": "i", "quantity": 0}]}, "items[0].quantity: must be at least 1"),
])
async def test_bad_params_are_blocked_with_a_clear_message(anvil, params, problem):
    await anvil.execute("agent", "get_menu", {"restaurant_id": "rest_1"})
    sent_before = len(anvil.sent)
    resp = await anvil.execute("agent", "place_order", params)
    assert resp.success is False and resp.tool_executed is False and resp.data is None
    assert resp.correction.violation == ViolationType.INVALID_PARAMS
    assert problem in resp.correction.message and "'place_order'" in resp.correction.message
    assert len(anvil.sent) == sent_before  # the upstream was never called
    call = anvil.call_log.calls_for("s", "food.agent")[-1]
    assert (call.result, call.violation) == (CallResult.BLOCKED, ViolationType.INVALID_PARAMS)


async def test_path_params_are_checked(anvil):
    resp = await anvil.execute("agent", "get_menu", {})
    assert "restaurant_id: required field is missing" in resp.correction.message
    resp = await anvil.execute("agent", "get_menu", {"restaurant_id": ["a"]})
    assert "restaurant_id: expected string, got array" in resp.correction.message
    assert anvil.sent == []


async def test_path_params_are_url_encoded(anvil):
    await anvil.execute("agent", "get_menu", {"restaurant_id": "../admin?x=1"})
    assert anvil.sent[-1].url.raw_path == b"/restaurants/..%2Fadmin%3Fx%3D1/menu"


async def test_blocked_call_does_not_satisfy_a_sequence_rule(anvil):
    bad = await anvil.execute("agent", "get_menu", {"restaurant_id": "rest_1", "extra": 1})
    assert bad.correction.violation == ViolationType.INVALID_PARAMS
    assert anvil.call_log.history_names("s", "food.agent") == []
    resp = await anvil.execute("agent", "place_order", {"restaurant_id": "rest_1", "items": ITEMS})
    assert resp.correction.violation == ViolationType.MISSING_PREREQUISITE


async def test_tool_without_schema_warns_once_and_passes(anvil, caplog):
    anvil_client._UNCHECKED_TOOLS_WARNED.discard("food.legacy")
    with caplog.at_level(logging.WARNING, logger="anvil"):
        for _ in range(2):
            resp = await anvil.execute("agent", "legacy", {"anything": {"goes": 1}})
            assert resp.success is True
    warnings = [r.getMessage() for r in caplog.records if "food.legacy" in r.getMessage()]
    assert len(warnings) == 1 and "declares no parameter schema" in warnings[0]


# ── host-filled params ─────────────────────────────────────────────────────

async def test_host_param_may_be_undeclared_and_is_sent(anvil):
    resp = await anvil.execute("agent", "pay", {"order_id": "o1"}, host_params={"channel": "agent"})
    assert resp.success is True
    assert anvil.sent[-1].url.path == "/orders/o1/pay"
    assert json.loads(anvil.sent[-1].content) == {"channel": "agent"}
    assert anvil.call_log.calls_for("s", "food.agent")[-1].params == {"order_id": "o1", "channel": "agent"}


async def test_model_cannot_send_a_param_only_the_host_may_set(anvil):
    resp = await anvil.execute("agent", "pay", {"order_id": "o1", "channel": "checkout"},
                               host_params={"channel": "agent"})
    assert resp.correction.violation == ViolationType.INVALID_PARAMS
    assert "channel: unknown field" in resp.correction.message
    assert anvil.sent == []


async def test_host_can_fill_a_required_declared_param(anvil):
    await anvil.execute("agent", "get_menu", {}, host_params={"restaurant_id": "rest_9"})
    assert anvil.sent[-1].url.path == "/restaurants/rest_9/menu"


def test_validator_unit_cases():
    schema = {"type": "object", "properties": {"n": {"type": "number"}, "free": {"type": "object",
              "additionalProperties": True}}}
    assert validate_params({"n": 1.5, "free": {"x": 1}}, schema) == []
    assert validate_params({"n": 2}, schema) == []
    assert validate_params({"n": "2"}, schema) == ["n: expected number, got string"]
    assert validate_params({}, {"type": "object", "properties": {}}) == []
    assert validate_params({"a": 1}, {"type": "object", "properties": {}}) == ["a: unknown field (not in the tool's schema)"]
