"""Stable, user-facing DuckVault errors."""


class DuckVaultError(RuntimeError):
    """Base error carrying a stable code and retry hint."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }


class VaultIdentityError(DuckVaultError):
    """Raised before a database can be used for a different vault."""

    def __init__(self, message: str):
        super().__init__("VAULT_IDENTITY_MISMATCH", message)
