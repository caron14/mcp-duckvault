"""Verify an initialized wheel can start and search with no network interfaces."""

import os
import sys
import time
from pathlib import Path

import torch

from mcp_duckvault.daemon import ensure_daemon
from mcp_duckvault.vault_identity import VaultLayout


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: offline_search_smoke.py VAULT_PATH")
    vault = Path(sys.argv[1]).resolve(strict=True)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    assert torch.version.cuda is None
    assert not torch.cuda.is_available()

    layout = VaultLayout.for_vault(vault)
    client = ensure_daemon(layout, timeout=30)
    try:
        result = client.call("tool:search_notes", {"query": "offline release smoke"})
        assert result["schema_version"] == "1.0"
        assert result["count"] >= 1
        assert result["items"][0]["path"] == "offline.md"
    finally:
        client.call("shutdown")
        deadline = time.monotonic() + 20
        while layout.endpoint_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not layout.endpoint_path.exists()


if __name__ == "__main__":
    main()
