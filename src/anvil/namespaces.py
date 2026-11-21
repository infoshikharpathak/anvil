"""App-namespaced IDs for tools and agents.

Every tool and agent belongs to an app. Its canonical ID is ``"app.name"``,
except for the ``default`` app, whose IDs stay bare (``"name"``) so configs
written before apps existed keep the same tool names, traces and messages.

References written inside an agent's policy (``allowed_tools``, sequences,
``allowed_delegations``...) are resolved relative to that agent's app: a
bare ``"get_accounts"`` under ``ironclad.banking_agent`` means
``"ironclad.get_accounts"``. To point at another app, write the qualified
form (``"notify.send_email"``); ``"default.x"`` names the default app.
"""

from __future__ import annotations

DEFAULT_APP = "default"


def qualify(app: str, name: str) -> str:
    """Canonical ID for ``name`` in ``app``."""
    return name if app == DEFAULT_APP else f"{app}.{name}"


def split_id(ref: str) -> tuple[str, str]:
    """``"app.name"`` -> ``(app, name)``; a bare name belongs to the default app."""
    if "." in ref:
        app, name = ref.split(".", 1)
        return app, name
    return DEFAULT_APP, ref


def canonical(ref: str) -> str:
    """Normalize an absolute reference (``"default.x"`` -> ``"x"``)."""
    return qualify(*split_id(ref))


def app_of(ref: str) -> str:
    return split_id(ref)[0]


def short_name(ref: str) -> str:
    return split_id(ref)[1]


def resolve(ref: str, ctx_app: str) -> str:
    """Resolve a reference written inside ``ctx_app``: bare names are local
    to that app, qualified names are absolute."""
    if "." in ref:
        return canonical(ref)
    return qualify(ctx_app, ref)


def relative(ref: str, ctx_app: str) -> str:
    """How an agent in ``ctx_app`` would name ``ref``: bare for its own
    app's tools, qualified otherwise. The inverse of ``resolve``."""
    app, name = split_id(ref)
    if app == ctx_app:
        return name
    # A default-app ID is bare, which would resolve locally; spell it out.
    return f"{DEFAULT_APP}.{name}" if app == DEFAULT_APP else ref
