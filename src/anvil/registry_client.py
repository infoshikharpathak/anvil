"""HTTP client for fetching policies from the Anvil Registry."""

from __future__ import annotations

import httpx

from .models import AnvilConfig


class RegistryError(Exception):
    pass


def registry_headers(
    session_id: str | None, client_id: str | None, client_key: str | None
) -> dict[str, str]:
    """Headers identifying this client to the registry. `client_key` is the
    per-app API key issued for `client_id`; the registry stores only its
    hash and uses it to decide which app's policies and tools to serve."""
    headers: dict[str, str] = {}
    if session_id:
        headers["X-Session-Id"] = session_id
    if client_id:
        headers["X-Client-Id"] = client_id
    if client_key:
        headers["Authorization"] = f"Bearer {client_key}"
    return headers


async def fetch_policy_bundle(
    registry_url: str,
    policy_name: str,
    session_id: str,
    client_id: str | None = None,
    timeout: float = 5.0,
    client_key: str | None = None,
) -> AnvilConfig:
    """Fetch a full config bundle (tools + the named policy + settings) from
    the registry. Records a policy checkout on the registry side (used later
    for orphan-session detection)."""
    headers = registry_headers(session_id, client_id, client_key)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{registry_url}/policies/{policy_name}", headers=headers
            )
            resp.raise_for_status()
            return AnvilConfig.model_validate(resp.json())
    except httpx.HTTPError as e:
        raise RegistryError(f"Could not fetch policy '{policy_name}' from registry: {e}") from e
