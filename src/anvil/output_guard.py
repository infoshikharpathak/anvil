"""Deterministic OTP guard for agent replies (no LLM).

Runs before the output classifier. Blocks a reply that contains a standalone
6-digit number (a code, not part of an ID, account number or amount), or
that asks the user to tell, type, paste or send a code to the assistant.
"""

from __future__ import annotations

import re

# 6 digits on their own: not part of an amount ($100,000, 1,234,567.89) or a
# path/percentage, and not inside a token that has letters, underscores or
# hyphens (txn_9f482913, ACC-482913-CHK, ref 482913-A): see _in_id_token.
_SIX = re.compile(r"(?<![\d$€£/#])(?<!\d[.,])\d{6}(?![\d%/])(?![.,]\d)")
_TOKEN_EDGE = re.compile(r"[\s.,;:!?()\[\]{}\"'<>]")

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


# A request phrase is ignored when its clause negates it ("never share it",
# "don't send it to anyone", "I'll never ask for", "..., including me").
_CLAUSE_EDGE = re.compile(r"[.;:!?,\n]")
_NEGATION = re.compile(r"\b(?:never|don'?t|do\s+not|won'?t|will\s+not|shouldn'?t|should\s+not|not|no\s+one|nobody|"
                       r"neither)\b", re.IGNORECASE)
_INCLUDING_ME = re.compile(r"^\W{0,3}(?:including|not\s+even|even)\s+(?:with\s+|to\s+)?me\b", re.IGNORECASE)


def _in_id_token(text: str, start: int, end: int) -> bool:
    """True if the digits at text[start:end] are part of a token that also has
    letters, underscores or hyphens (an ID, not a code)."""
    left = start
    while left > 0 and not _TOKEN_EDGE.match(text[left - 1]):
        left -= 1
    right = end
    while right < len(text) and not _TOKEN_EDGE.match(text[right]):
        right += 1
    token = text[left:right]
    return token != text[start:end] and bool(re.search(r"[A-Za-z_\-]", token))


def _standalone_code(text: str) -> bool:
    return any(not _in_id_token(text, m.start(), m.end()) for m in _SIX.finditer(text))


def _negated(text: str, start: int, end: int) -> bool:
    clause_start = max((m.end() for m in _CLAUSE_EDGE.finditer(text, 0, start)), default=0)
    if _NEGATION.search(text[clause_start:start]):
        return True
    return bool(_INCLUDING_ME.match(text[end:end + 30]))


def _asks_for_code(text: str) -> bool:
    return any(not _negated(text, m.start(), m.end()) for m in _ASK.finditer(text))


def otp_guard(text: str) -> str | None:
    """The reason to block *text*, or None if it passes."""
    text = text or ""
    if _standalone_code(text):
        return "otp_guard: reply contains a standalone 6-digit code"
    if _asks_for_code(text):
        return "otp_guard: reply asks the user to give a code to the assistant"
    return None
