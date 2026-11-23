"""Run the registry: ``anvil-registry`` or ``python -m anvil.registry``.

Needs the [registry] extra (``pip install "garrison-anvil[registry]"``). The
policy file comes from ANVIL_CONFIG (default ./anvil.yaml).
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="anvil-registry", description="Run the Anvil registry service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args(argv)
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(f'The registry needs its extra: pip install "garrison-anvil[registry]" ({exc})')
    uvicorn.run("anvil.registry.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
