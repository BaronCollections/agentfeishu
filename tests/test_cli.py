from __future__ import annotations

from agentfeishu.cli import main


def test_cli_capabilities_outputs_json(tmp_path, capsys):
    rc = main(["--project-root", str(tmp_path), "capabilities"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "url_ingest" in out
