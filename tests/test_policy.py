import pytest

from anvil.call_log import CallLog
from anvil.config import ConfigError, load_config_dict
from anvil.models import CallResult, PolicyConfig, RecordedCall, ViolationType
from anvil.policy import PolicyEngine

BASE_CONFIG = {
    "tools": {
        "verify_account": {"endpoint": "http://x/verify", "method": "POST", "trust": "trusted"},
        "get_balance": {"endpoint": "http://x/balance", "method": "GET", "trust": "trusted"},
        "send_payment": {"endpoint": "http://x/pay", "method": "POST", "trust": "untrusted"},
    },
    "policies": {
        "strict-payments": {
            "agents": {
                "payment_agent": {
                    "allowed_tools": ["verify_account", "get_balance", "send_payment"],
                    "sequences": [
                        {
                            "before": "send_payment",
                            "requires": ["verify_account", "get_balance"],
                            "match_fields": ["account_id"],
                        }
                    ],
                    "strictness": "strict",
                }
            }
        }
    },
}


def make_engine(config_dict=None):
    config = load_config_dict(config_dict or BASE_CONFIG)
    call_log = CallLog()
    engine = PolicyEngine(config.policies["strict-payments"], call_log)
    return engine, call_log


def test_unknown_agent_hard_blocked():
    engine, _ = make_engine()
    decision = engine.check_request("s1", "ghost_agent", "send_payment", {"account_id": "A"})
    assert not decision.allowed
    assert decision.correction.violation == ViolationType.UNKNOWN_AGENT
    assert decision.correction.required_actions == []


def test_disallowed_tool_hard_blocked_even_when_loose():
    config_dict = {
        **BASE_CONFIG,
        "policies": {
            "strict-payments": {
                "agents": {
                    "payment_agent": {
                        **BASE_CONFIG["policies"]["strict-payments"]["agents"]["payment_agent"],
                        "strictness": "loose",
                    }
                }
            }
        },
    }
    engine, _ = make_engine(config_dict)
    decision = engine.check_request("s1", "payment_agent", "send_payment", {"account_id": "A"})
    # loose strictness softens sequence violations: the call proceeds (allowed=True)
    # but the correction info is still populated so the caller can log a warning.
    assert decision.allowed
    assert decision.enforce is False
    assert decision.correction.violation == ViolationType.MISSING_PREREQUISITE


def test_missing_prerequisite_strict_blocks():
    engine, _ = make_engine()
    decision = engine.check_request("s1", "payment_agent", "send_payment", {"account_id": "A"})
    assert not decision.allowed
    assert decision.enforce is True
    assert set(decision.correction.required_actions) == {"verify_account", "get_balance"}


def test_prerequisites_satisfied_allows():
    engine, call_log = make_engine()
    call_log.record(
        "s1",
        RecordedCall(tool="verify_account", agent="payment_agent", params={"account_id": "A"}, result=CallResult.SUCCESS),
    )
    call_log.record(
        "s1",
        RecordedCall(tool="get_balance", agent="payment_agent", params={"account_id": "A"}, result=CallResult.SUCCESS),
    )
    decision = engine.check_request("s1", "payment_agent", "send_payment", {"account_id": "A"})
    assert decision.allowed


def test_match_fields_rejects_mismatched_account():
    engine, call_log = make_engine()
    call_log.record(
        "s1",
        RecordedCall(tool="verify_account", agent="payment_agent", params={"account_id": "A"}, result=CallResult.SUCCESS),
    )
    call_log.record(
        "s1",
        RecordedCall(tool="get_balance", agent="payment_agent", params={"account_id": "A"}, result=CallResult.SUCCESS),
    )
    # prereqs were for account A, but this call is for account B
    decision = engine.check_request("s1", "payment_agent", "send_payment", {"account_id": "B"})
    assert not decision.allowed
    assert set(decision.correction.required_actions) == {"verify_account", "get_balance"}


def test_response_withheld_call_still_counts_as_prerequisite():
    """A call whose response was withheld (injection/schema) still executed —
    the side effect happened — so it must still satisfy prerequisites."""
    engine, call_log = make_engine()
    call_log.record(
        "s1",
        RecordedCall(
            tool="verify_account",
            agent="payment_agent",
            params={"account_id": "A"},
            result=CallResult.RESPONSE_WITHHELD,
        ),
    )
    call_log.record(
        "s1",
        RecordedCall(tool="get_balance", agent="payment_agent", params={"account_id": "A"}, result=CallResult.SUCCESS),
    )
    decision = engine.check_request("s1", "payment_agent", "send_payment", {"account_id": "A"})
    assert decision.allowed


def test_blocked_call_does_not_satisfy_prerequisite():
    engine, call_log = make_engine()
    call_log.record(
        "s1",
        RecordedCall(tool="verify_account", agent="payment_agent", params={"account_id": "A"}, result=CallResult.BLOCKED),
    )
    decision = engine.check_request("s1", "payment_agent", "send_payment", {"account_id": "A"})
    assert not decision.allowed


# -- config validation ------------------------------------------------------


def test_config_rejects_sequence_on_unregistered_tool():
    bad = {
        "tools": BASE_CONFIG["tools"],
        "policies": {
            "p": {
                "agents": {
                    "a": {
                        "allowed_tools": ["send_payment"],
                        "sequences": [{"before": "send_payment", "requires": ["ghost_tool"]}],
                    }
                }
            }
        },
    }
    with pytest.raises(ConfigError):
        load_config_dict(bad)


def test_config_rejects_circular_prerequisites():
    bad = {
        "tools": {
            "tool_a": {"endpoint": "http://x/a", "trust": "trusted"},
            "tool_b": {"endpoint": "http://x/b", "trust": "trusted"},
        },
        "policies": {
            "p": {
                "agents": {
                    "a": {
                        "allowed_tools": ["tool_a", "tool_b"],
                        "sequences": [
                            {"before": "tool_a", "requires": ["tool_b"]},
                            {"before": "tool_b", "requires": ["tool_a"]},
                        ],
                    }
                }
            }
        },
    }
    with pytest.raises(ConfigError):
        load_config_dict(bad)


def test_config_rejects_trust_override_outside_allowed_tools():
    bad = {
        "tools": BASE_CONFIG["tools"],
        "policies": {
            "p": {
                "agents": {
                    "a": {
                        "allowed_tools": ["verify_account"],
                        "sequences": [],
                        "trust_overrides": {"send_payment": "untrusted"},
                    }
                }
            }
        },
    }
    with pytest.raises(ConfigError):
        load_config_dict(bad)


def test_config_rejects_policy_tool_not_in_tools_section():
    bad = {
        "tools": {"verify_account": BASE_CONFIG["tools"]["verify_account"]},
        "policies": {
            "p": {"agents": {"a": {"allowed_tools": ["nonexistent_tool"], "sequences": []}}}
        },
    }
    with pytest.raises(ConfigError):
        load_config_dict(bad)


def test_valid_config_loads_without_error():
    config = load_config_dict(BASE_CONFIG)
    assert "strict-payments" in config.policies
