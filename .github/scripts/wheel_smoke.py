"""Validate the installed DuckVault wheel without initializing a Vault."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run(command: list[str], *, expected_exit: int, env: dict[str, str]) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=60,
    )
    if result.returncode != expected_exit:
        raise AssertionError(
            f"Command {command!r} exited {result.returncode}, expected {expected_exit}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: wheel_smoke.py DUCKVAULT_EXECUTABLE EXPECTED_VERSION")

    executable, expected_version = sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="duckvault-wheel-smoke-") as temporary:
        root = Path(temporary)
        vault = root / "vault"
        vault.mkdir()
        env = os.environ.copy()
        env.update(
            {
                "DUCKVAULT_HOME": str(root / "home"),
                "HF_HUB_OFFLINE": "1",
                "SENTENCE_TRANSFORMERS_HOME": str(root / "models"),
            }
        )

        help_output = run([executable, "--help"], expected_exit=0, env=env)
        assert "Usage:" in help_output

        version_output = run([executable, "--version"], expected_exit=0, env=env)
        assert expected_version in version_output

        doctor_output = run([executable, "doctor", str(vault), "--json"], expected_exit=1, env=env)
        doctor = json.loads(doctor_output)
        assert doctor["status"] == "failed"
        assert isinstance(doctor["checks"], list)

        status_output = run([executable, "status", str(vault), "--json"], expected_exit=1, env=env)
        status = json.loads(status_output)
        assert status["status"] == "failed"
        assert status["error"]["code"] == "NOT_INITIALIZED"


if __name__ == "__main__":
    main()
