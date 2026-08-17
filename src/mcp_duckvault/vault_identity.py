"""Vault identity and per-vault filesystem layout."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


def normalize_vault_path(vault_path: str | os.PathLike[str]) -> Path:
    """Return a stable absolute path, resolving symlinks and path notation."""
    resolved = Path(vault_path).expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(str(resolved))
    normalized = os.path.normcase(str(resolved)) if os.name == "nt" else str(resolved)
    return Path(normalized)


def vault_id_for_path(vault_path: str | os.PathLike[str]) -> str:
    normalized = normalize_vault_path(vault_path)
    return hashlib.sha256(os.fsencode(str(normalized))).hexdigest()[:24]


def duckvault_home() -> Path:
    override = os.environ.get("DUCKVAULT_HOME")
    return Path(override).expanduser() if override else Path.home() / ".duckvault"


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        # Windows and some mounted filesystems do not expose POSIX modes.
        pass


@dataclass(frozen=True)
class VaultIdentity:
    vault_id: str
    normalized_path: Path

    @classmethod
    def from_path(cls, vault_path: str | os.PathLike[str]) -> "VaultIdentity":
        normalized = normalize_vault_path(vault_path)
        return cls(vault_id_for_path(normalized), normalized)


@dataclass(frozen=True)
class VaultLayout:
    identity: VaultIdentity
    root: Path

    @classmethod
    def for_vault(
        cls, vault_path: str | os.PathLike[str], *, create: bool = False
    ) -> "VaultLayout":
        identity = VaultIdentity.from_path(vault_path)
        root = duckvault_home() / "vaults" / identity.vault_id
        layout = cls(identity, root)
        if create:
            layout.ensure()
        return layout

    def ensure(self) -> None:
        _ensure_private_directory(duckvault_home())
        _ensure_private_directory(duckvault_home() / "vaults")
        _ensure_private_directory(self.root)
        _ensure_private_directory(self.backups_dir)

    @property
    def db_path(self) -> Path:
        return self.root / "vault.db"

    @property
    def endpoint_path(self) -> Path:
        return self.root / "endpoint.json"

    @property
    def owner_lock_path(self) -> Path:
        return self.root / "owner.lock"

    @property
    def startup_lock_path(self) -> Path:
        return self.root / "startup.lock"

    @property
    def daemon_log_path(self) -> Path:
        return self.root / "daemon.log"

    @property
    def generated_mcp_config_path(self) -> Path:
        return self.root / "mcp-server.json"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"


def model_cache_path(*, create: bool = False) -> Path:
    path = duckvault_home() / "models"
    if create:
        _ensure_private_directory(path)
    return path
