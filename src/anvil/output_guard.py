"""Deterministic OTP guard for agent replies (no LLM).

Runs before the output classifier. Blocks a reply that contains a standalone
6-digit number (a code, not part of an ID, account number or amount), or
that asks the user to tell, type, paste or send a code to the assistant.
"""

from __future__ import annotations

import re

# 6 digits on their own: not inside a word/ID (txn_123456, ACC-123456),
# not part of an amount ($100,000, 1,234,567.89) or a path/percentage.
_STANDALONE_CODE = re.compile(r"(?<![\w$€£\-/#])(?<!\d[.,])\d{6}(?![\w\-/%])(?![.,]\d)")

_CODE = r"(?:code|otp|passcode|pin|one-time\s+pass(?:code|word))"
_ASK_PATTERNS = [
    # "tell me / send me / read me the code"
    rf"\b(?:tell|send|give|share|read|text|provide)\s+(?:me|us)\b[^.?!\n]{{0,40}}\b{_CODE}\b",
    # "paste / type the OTP here | in this chat | to me | below"
    rf"\b(?:type|paste|enter|send|share|provide|put|drop|write)\b[^.?!\n]{{0,40}}\b{_CODE}\b"
    rf"[^.?!\n]{{0,40}}\b(?:here|in\s+(?:this|the)\s+chat|to\s+me|with\s+me|below)\b",
    # "reply here with the 6-digit OTP"
    rf"\b(?:reply|respond)\s+(?:here\s+)?with\b[^.?!\n]{{0,30}}\b{_CODE}\b",
    # "what's your code?"
    rf"\bwhat(?:'s|\s+is)\s+(?:the|your)\s+(?:6-digit\s+|six-digit\s+)?{_CODE}\b",
]
_ASK = re.compile("|".join(_ASK_PATTERNS), re.IGNORECASE)


def otp_guard(text: str) -> str | None:
    """The reason to block *text*, or None if it passes."""
    if _STANDALONE_CODE.search(text or ""):
        return "otp_guard: reply contains a standalone 6-digit code"
    if _ASK.search(text or ""):
        return "otp_guard: reply asks the user to give a code to the assistant"
    return None
