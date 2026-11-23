"""Run the registry: ``anvil-registry`` or ``python -m anvil.registry``.

Needs the [registry] extra (``pip install "garrison-anvil[registry]"``). The
policy file is ``--config``, else ANVIL_CONFIG, else ./anvil.yaml. A missing
or invalid file is reported in one line (exit status 2), before the server
starts.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

CONFIG_DOCS = "https://github.com/infoshikharpathak/anvil#config-reference-anvilyaml"


def _fail(message: str) -> None:
    print(f"anvil-registry: {message}", file=sys.stderr)
    raise SystemExit(2)


def check_config(path: str) -> None:
    """Exit with a one-line message if *path* is missing or not a valid
    Anvil config."""
    from anvil.config import ConfigError, load_config

    if not Path(path).is_file():
        _fail(f"no config file at {path!r}. Pass --config PATH or set ANVIL_CONFIG "
              f"(see {CONFIG_DOCS}).")
    try:
        load_config(path)
    except ConfigError as exc:
        _fail(f"{path} is not a valid Anvil config: {exc}")
    except Exception as exc:  # unreadable file, YAML syntax error, ...
        _fail(f"can't read {path}: {type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="anvil-registry", description="Run the Anvil registry service.")
    parser.add_argument("--config", default=os.environ.get("ANVIL_CONFIG", "anvil.yaml"),
                        help="the Anvil policy file (default: $ANVIL_CONFIG, else ./anvil.yaml)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args(argv)
    check_config(args.config)
    try:
        import uvicorn
    except ImportError as exc:
        _fail(f'the registry needs its extra: pip install "garrison-anvil[registry]" ({exc})')
    os.environ["ANVIL_CONFIG"] = args.config  # what anvil.registry.app loads
    uvicorn.run("anvil.registry.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
