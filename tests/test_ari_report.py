from __future__ import annotations

import ast
import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ari_report
import pytest
from ari import report
from ari.evidence import ingest
from ari_fixtures import PROMPT, SECRET, asst, build, mcp_config, usage, write_jsonl

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
NOW = "2026-11-01T00:00:00Z"


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Path]:
    build(tmp_path / "root", tmp_path / "skills", tmp_path / "outside")
    return {"root": tmp_path / "root", "skills": tmp_path / "skills",
            "cfg": mcp_config(tmp_path / "claude.json"), "tmp": tmp_path}


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a: Any, **k: Any) -> Any:
        raise AssertionError("network use attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _main(env: dict[str, Path], *extra: str) -> int:
    return ari_report.main(["--root", str(env["root"]), "--now", NOW, "--days", "12",
                            "--skills-root", str(env["skills"]), "--mcp-config",
                            str(env["cfg"]), "--out", str(env["tmp"] / "r.html"), *extra])


def test_html_is_offline_escaped_and_complete(env: dict[str, Path],
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    _no_network(monkeypatch)
    assert _main(env) == 0
    page = (env["tmp"] / "r.html").read_text()
    low = page.lower()
    assert "<script" not in low and "http://" not in low and "https://" not in low
    assert "@import" not in low and "url(" not in low
    assert "Content-Security-Policy" in page and "default-src 'none'" in page
    assert "<img src=x" not in page and "&lt;img src=x onerror=alert(1)&gt;" in page
    assert SECRET not in page and PROMPT not in page and "run-srv1" not in page
    for heading in ("Agent types: starting context", "Starting context distribution",
                    "Weekly measured usage and forecast", "Cache breakdown per agent type",
                    "Skills and MCP audit", "Configuration drift", "Evidence quality",
                    "Hypotheses and proposed experiments", "Provider quota ETA: unavailable"):
        assert heading in page
    assert 'role="img"' in page and "<title>Agent Runtime Report</title>" in page


def test_share_is_screened_local_and_sparse_groups_suppressed(
        env: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _no_network(monkeypatch)
    sub = env["root"] / "proj-a" / "sess9" / "subagents"
    for i in range(5):
        for agent in ("lean-worker", "nebula-internal-auditor"):
            name = f"agent-{agent}{i}"
            write_jsonl(sub / f"{name}.jsonl",
                        [asst(f"{agent}{i}", f"2026-10-2{i}T00:00:00Z", usage(1, 7000 + i))])
            (sub / f"{name}.meta.json").write_text(json.dumps({"agentType": agent}))
    share = env["tmp"] / "share.json"
    assert _main(env, "--share", str(share)) == 0
    data = json.loads(share.read_text())
    assert report.screen_share(data) == []
    types = {r["agent_type"] for r in data["agent_types"]}
    assert "custom" in types and "lean-worker" in types  # odd custom name folded
    text = share.read_text()
    for bad in (str(env["tmp"]), "sess1", "proj-a", "2026-10", SECRET, "onerror"):
        assert bad not in text
    assert "nebula" not in text
    assert data["suppressed_groups"] == 0 or all(
        r["runs_bucket"] != "<5" for r in data["agent_types"])
    assert "nothing was uploaded" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        _main(env, "--share", str(share), "--share-min-runs", "1")


def test_screen_catches_leaks() -> None:
    base = {"schema": report.SHARE_SCHEMA, "agent_types": [], "claude_code_minor_versions": [],
            "ari_version": "2.0.0", "min_group_runs": 5, "suppressed_groups": 0,
            "note": report.SHARE_NOTE}
    assert report.screen_share(base) == []
    leaks = [
        {**base, "path": "/home/x"},
        {**base, "agent_types": [{"agent_type": "my-secret-project", "runs_bucket": "5-9"}]},
        {**base, "agent_types": [{"agent_type": "fork", "runs_bucket": "<5"}]},
        {**base, "claude_code_minor_versions": ["2.1.316"]},
        {**base, "schema": "a@b.com"},
        {**base, "ari_version": "a@b.com"},
        {**base, "ari_version": "2026-11-01"},
        {**base, "ari_version": "deadbeefdeadbeef"},
        {**base, "ari_version": "sk-ant-x"},
    ]
    for obj in leaks:
        assert report.screen_share(obj), obj


def test_share_refuses_to_write_when_screen_fails(env: dict[str, Path],
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report, "build_share",
                        lambda ev, k=5: {"schema": "x", "leak": "/home/user"})
    share = env["tmp"] / "share.json"
    assert _main(env, "--share", str(share)) == 1
    assert not share.exists()


def test_no_ari_module_can_reach_the_network() -> None:
    banned = {"socket", "urllib", "http", "requests", "ssl", "ftplib", "smtplib", "asyncio"}
    files = list((SCRIPTS / "ari").glob("*.py")) + [
        SCRIPTS / n for n in ("weekly_burn.py", "skill_mcp_audit.py", "ari_report.py",
                              "ari_nightly.py")]
    for f in files:
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            assert not banned & set(names), f"{f.name} imports {names}"


def test_gap_hypothesis_is_open_not_concluded(tmp_path: Path) -> None:
    root = tmp_path / "r"
    sub = root / "p" / "s" / "subagents"
    for i in range(3):
        for agent, n in (("general-purpose", 120000), ("lean-worker", 7000)):
            write_jsonl(sub / f"agent-{agent}{i}.jsonl",
                        [asst(f"{agent}{i}", f"2026-10-2{i}T00:00:00Z", usage(1, n))])
            (sub / f"agent-{agent}{i}.meta.json").write_text(json.dumps({"agentType": agent}))
    hs = report.gap_hypotheses(ingest([root], since=datetime(2026, 10, 1, tzinfo=timezone.utc)))
    assert len(hs) == 1
    h = hs[0]
    assert h.primary is None and h.confidence == "unknown" and h.state == "OPEN"
    assert len(h.explanations) >= 2
