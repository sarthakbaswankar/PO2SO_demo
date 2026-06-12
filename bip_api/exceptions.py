"""bip_api/exceptions.py"""


class AuthError(Exception):
    """Raised when Oracle BIP rejects credentials."""


class ReportError(Exception):
    """Raised when BIP returns an error or unexpected response."""
