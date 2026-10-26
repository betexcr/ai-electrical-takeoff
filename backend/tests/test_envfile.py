"""takeoff.envfile: the repo-root .env loader the CLI entry points call. No test here reads the real .env."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from takeoff import envfile

KEY = "sk-ant-api03-FAKE-envfile-secret-0123456789"
TOOLS = Path(__file__).resolve().parents[1] / "tools"


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_plain_quoted_and_exported_lines(tmp_path):
    p = write(tmp_path, "# comment\n\nANTHROPIC_API_KEY=" + KEY + "\n"
                        "export TAKEOFF_AI_MODEL='claude-test'\nTAKEOFF_AI_MAX_REQUESTS = \"7\"\r\n")
    env: dict[str, str] = {}
    assert envfile.load_env_file(p, env) == ["ANTHROPIC_API_KEY", "TAKEOFF_AI_MODEL", "TAKEOFF_AI_MAX_REQUESTS"]
    assert env == {"ANTHROPIC_API_KEY": KEY, "TAKEOFF_AI_MODEL": "claude-test", "TAKEOFF_AI_MAX_REQUESTS": "7"}


def test_never_overrides_the_environment(tmp_path):
    p = write(tmp_path, f"ANTHROPIC_API_KEY={KEY}\n")
    env = {"ANTHROPIC_API_KEY": "from-shell"}
    assert envfile.load_env_file(p, env) == []
    assert env == {"ANTHROPIC_API_KEY": "from-shell"}


def test_empty_values_and_malformed_lines_are_skipped(tmp_path):
    p = write(tmp_path, "ANTHROPIC_API_KEY=\nANTHROPIC_AUTH_TOKEN=''\nnot a pair\n=orphan\n")
    env: dict[str, str] = {}
    assert envfile.load_env_file(p, env) == []
    assert env == {}


def test_missing_file_is_a_no_op(tmp_path):
    env: dict[str, str] = {}
    assert envfile.load_env_file(tmp_path / ".env", env) == []
    assert env == {}


def test_default_path_is_the_repo_root():
    assert envfile.DEFAULT_PATH == Path(__file__).resolve().parents[2] / ".env"


def test_importing_the_entry_points_loads_nothing(monkeypatch):
    """Only running a tool as a script loads .env; importing it (as the tests do) must not pick up a real key."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(envfile, "load_env_file", lambda *a, **k: calls.append(a) or [])
    for name in ("takeoff_app", "ai_preflight", "eval_ai_verifier", "eval_ai_discovery"):
        spec = importlib.util.spec_from_file_location(f"envcheck_{name}", TOOLS / f"{name}.py")
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
    assert calls == []


def test_running_a_tool_loads_the_file_and_prints_names_only(tmp_path):
    """takeoff_app.py --help runs the __main__ block (load, then argparse exits 0) without starting a server."""
    p = write(tmp_path, f"ANTHROPIC_API_KEY={KEY}\n")
    code = (f"import os, runpy, sys; os.environ.pop('ANTHROPIC_API_KEY', None); "
            f"from takeoff import envfile; envfile.DEFAULT_PATH = __import__('pathlib').Path({str(p)!r}); "
            f"sys.argv = ['takeoff_app.py', '--help']; "
            f"runpy.run_path({str(TOOLS / 'takeoff_app.py')!r}, run_name='__main__')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=TOOLS.parent)
    assert r.returncode == 0, r.stderr
    assert "loaded ANTHROPIC_API_KEY from" in r.stderr
    assert KEY not in r.stdout + r.stderr
