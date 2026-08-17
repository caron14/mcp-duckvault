"""CLI contract and machine-readable output tests."""

import json

from click.testing import CliRunner

from mcp_duckvault import cli


class FakeClient:
    def __init__(self, result):
        self.result = result

    def call(self, method, params=None, **_kwargs):
        assert method == "sync"
        return self.result


def test_cli_requires_explicit_subcommand(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()

    result = CliRunner().invoke(cli.main, [str(vault)])

    assert result.exit_code == 2
    assert "No such command" in result.output


def test_sync_json_uses_exit_two_for_partial_result(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    payload = {
        "status": "partial",
        "scanned": 2,
        "indexed": 1,
        "skipped": 0,
        "deleted": 0,
        "failed": 1,
        "excluded": 0,
    }
    monkeypatch.setattr(cli, "ensure_daemon", lambda *_args, **_kwargs: FakeClient(payload))

    result = CliRunner().invoke(cli.main, ["sync", str(vault), "--json"])

    assert result.exit_code == 2
    assert json.loads(result.output)["status"] == "partial"


def test_doctor_json_is_single_document_and_fails_when_a_check_fails(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(
        cli,
        "_doctor_checks",
        lambda *_args: [
            {"code": "MODEL_OFFLINE", "ok": False, "detail": "missing", "repair": "init"}
        ],
    )

    result = CliRunner().invoke(cli.main, ["doctor", str(vault), "--json"])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "status": "failed",
        "checks": [{"code": "MODEL_OFFLINE", "ok": False, "detail": "missing", "repair": "init"}],
    }
