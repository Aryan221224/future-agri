"""Exceptions the HTTP layer maps to status codes."""


class Busy(RuntimeError):
    """A session or farm is busy with another action (HTTP 409)."""


class NotFound(LookupError):
    """HTTP 404."""


class TooLarge(ValueError):
    """Upload over the size limit (HTTP 413)."""
