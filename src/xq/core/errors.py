"""Exception hierarchy for xq.

Every error raised deliberately by the platform derives from `XQError`, so callers (and the CLI) can
separate expected failures from bugs.
"""


class XQError(Exception):
    """Base class for all deliberate xq errors."""


class ConfigError(XQError):
    """The configuration is missing, malformed or violates a configuration rule."""
