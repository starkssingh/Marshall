"""Exception hierarchy for xq.

Every error raised deliberately by the platform derives from `XQError`, so callers (and the CLI) can
separate expected failures from bugs.
"""


class XQError(Exception):
    """Base class for all deliberate xq errors."""


class ConfigError(XQError):
    """The configuration is missing, malformed or violates a configuration rule."""


class NaiveTimestampError(XQError, ValueError):
    """A timestamp without a timezone reached code that requires tz-aware values."""


class ClockConventionError(XQError, ValueError):
    """A clock convention is malformed, or timestamps are inconsistent with the declared one."""
