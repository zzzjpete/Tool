class ScraperError(Exception):
    """Base class for scraper-specific errors."""


class RateLimited(ScraperError):
    """Raised when the server signals rate limiting (429, 412, etc.)."""


class AuthRequired(ScraperError):
    """Raised when an endpoint requires a logged-in cookie that is missing or expired."""


class NotFound(ScraperError):
    """Raised when the requested resource does not exist or has been removed."""


class ParseError(ScraperError):
    """Raised when a response cannot be parsed into the expected shape."""


class CookieExpired(AuthRequired):
    """Raised when the session cookie is detectably dead (not just wrong-for-this-endpoint).

    A regular `AuthRequired` means "this endpoint needs auth"; `CookieExpired` means
    "the cookie you pasted has been invalidated, paste a fresh one." Keeping it a
    subclass of AuthRequired so existing `except AuthRequired:` blocks still catch it.
    """


class SoftBanned(RateLimited):
    """Raised when the server returns success-shaped responses with empty/abnormal data —
    a soft ban where you're not blocked per-request but the platform is feeding you
    garbage. Subclass of RateLimited so the retry/back-off path kicks in.
    """
