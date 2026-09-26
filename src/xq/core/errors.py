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


class SourceFormatError(XQError, ValueError):
    """A source file does not match the format its adapter expects."""


class ProvenanceError(XQError):
    """A source or instrument declaration conflicts with what was already recorded."""


class RawStoreIntegrityError(XQError):
    """A file in the immutable raw store differs from its manifest entry."""


class MirrorVersionError(XQError):
    """A raw-store mirror part predates the schema a later stage needs; rebuild the mirror."""


class VaultAccessError(XQError):
    """Data at or after ``vault.start`` was requested without a gate token."""
