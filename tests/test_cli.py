"""CLI contract and machine-readable output tests."""

import json

from click.testing import CliRunner

from mcp_duckvault import cli
from mcp_duckvault.vault_identity import VaultIdentity


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


def test_init_preflight_runs_before_initialization_and_partial_exits_two(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    calls = []

    def preflight(*_args, **_kwargs):
        calls.append("preflight")
        return {"status": "planned", "scanned": 1}

    def initialize(*_args, **_kwargs):
        calls.append("initialize")
        return {
            "status": "partial",
            "sync": {"failures": [{"path": "bad.md"}]},
            "retry_command": f"duckvault sync {vault} --json",
        }

    monkeypatch.setattr(cli, "_preflight_plan", preflight)
    monkeypatch.setattr(cli, "_initialize_vault", initialize)

    result = CliRunner().invoke(cli.main, ["init", str(vault), "--json"])

    assert result.exit_code == 2
    assert calls == ["preflight", "initialize"]
    payload = json.loads(result.output)
    assert payload["preflight"]["status"] == "planned"
    assert payload["sync"]["failures"][0]["path"] == "bad.md"
    assert payload["retry_command"].endswith("--json")


def test_explain_ignore_reports_matching_rule(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".vaultignore").write_text("private/\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli.main, ["explain-ignore", str(vault), "private/note.md", "--json"]
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "path": "private/note.md",
        "excluded": True,
        "reason": "pattern:private/",
    }


def test_sync_dry_run_does_not_start_daemon_or_write_database(
    tmp_path, monkeypatch, database_factory
):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "new.md").write_text("# New", encoding="utf-8")
    db_path = tmp_path / "vault.db"
    db = database_factory(str(db_path), identity=VaultIdentity.from_path(vault))
    db.close()
    monkeypatch.setattr(
        cli,
        "ensure_daemon",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("daemon started")),
    )

    result = CliRunner().invoke(
        cli.main,
        ["sync", str(vault), "--db-path", str(db_path), "--dry-run", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["paths"]["indexed"] == ["new.md"]
    check = cli.DatabaseManager(str(db_path), read_only=True)
    check.connect(load_vss=False)
    assert check.conn.execute("SELECT count(*) FROM documents").fetchone() == (0,)
    check.close()
