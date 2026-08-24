"""Tunable constants for the execution layer, kept in one place per the
project's config-block convention (see simulator/config.py)."""

# ASSUMPTION: ungiven. Long enough that a normal in-flight API call (seconds)
# never gets falsely flagged as stuck; short enough to be a meaningful
# backstop within about half an hour of a real crash or a missed webhook.
RECONCILIATION_STALENESS_MINUTES = 30
