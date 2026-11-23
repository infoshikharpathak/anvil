"""anvil-registry reports a missing or invalid config in one line and exits 2."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from anvil.registry import __main__ as cli

ROOT_CONFIG = Path(__file__).resolve().parents[1] / "anvil.yaml"


def test_missing_config_is_a_clear_message_not_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANVIL_CONFIG", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("anvil-registry: no config file at 'anvil.yaml'")
    assert "--config" in err and "ANVIL_CONFIG" in err


def test_invalid_config_is_reported(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("tools: [not, a, mapping\n")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(bad)])
    assert exc.value.code == 2
    assert capsys.readouterr().err.startswith(f"anvil-registry: can't read {bad}")


def test_valid_config_starts_uvicorn_with_it(monkeypatch):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.delenv("ANVIL_CONFIG", raising=False)
    cli.main(["--config", str(ROOT_CONFIG), "--port", "8123"])
    assert calls == [("anvil.registry.app:app", {"host": "127.0.0.1", "port": 8123})]
    import os
    assert os.environ["ANVIL_CONFIG"] == str(ROOT_CONFIG)
    monkeypatch.delenv("ANVIL_CONFIG")


def test_command_line_run_prints_no_traceback(tmp_path):
    env = {k: v for k, v in __import__("os").environ.items() if k != "ANVIL_CONFIG"}
    proc = subprocess.run([sys.executable, "-m", "anvil.registry"], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert proc.stderr.strip().startswith("anvil-registry: no config file at 'anvil.yaml'")
