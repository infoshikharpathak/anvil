"""Structured audit logging for the Anvil library.

Named audit.py (not logging.py) to avoid shadowing the stdlib module inside
the package.
"""

from __future__ import annotations

import logging
import sys

_LOGGER_NAME = "anvil.audit"


def get_audit_logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [anvil] %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_call(
    *,
    session_id: str,
    agent_id: str,
    tool_name: str,
    result: str,
    violation: str | None = None,
    enforced: bool = True,
) -> None:
    logger = get_audit_logger()
    msg = f"session={session_id} agent={agent_id} tool={tool_name} result={result}"
    if violation:
        msg += f" violation={violation} enforced={enforced}"
    if result == "blocked" or (violation and not enforced):
        logger.warning(msg)
    else:
        logger.info(msg)
