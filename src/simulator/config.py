"""Tunable constants for the simulator, kept in one block per schema §4.

Values marked ASSUMPTION are not given numerically by the spec (it deliberately
leaves them for tuning — e.g. "k1 ~ 0.15/day is a reasonable starting constant").
Adjust here; nothing downstream hardcodes these.
"""

# --- Ground-truth decay constants (§4) ---
K1_FUNDS_ARRIVAL_DECAY = 0.15  # per day; spec's own suggested starting value
K2_ISSUER_RECOVERY_DECAY = 0.02  # per hour; ASSUMPTION — "small", barely decays
K3_NETWORK_TIMEOUT_DECAY = 0.5  # per minute; ASSUMPTION — "large", decays fast
K4_AFA_DROPOFF_DECAY = 0.3  # per hour; ASSUMPTION — "large", forgets within hours
K5_LIMIT_EXCEEDED_DECAY = 0.05  # per hour; ASSUMPTION — "moderate" decay

RISK_DECLINE_ESCALATE_MULTIPLIER = 0.4  # spec-given, not tunable
LIMIT_EXCEEDED_COOLDOWN_HOURS = 4  # ASSUMPTION — spec says "few hours"

# --- Contact cost (§5) ---
COST_PER_CONTACT_PAISE = 200  # spec-given example value (Rs 2)

# --- Compliance (eval spec §2 hard invariants) ---
# 2026-08-25 (DECISIONS.md): a policy-level compliance rule, separate from
# Customer.annoyance_threshold (a hidden, per-persona lifetime-patience cap
# used by ground truth). MAX_WEEKLY_CONTACTS is a rate limit any policy must
# respect, checked against the action log — not part of the persona schema.
MAX_WEEKLY_CONTACTS = 3

# --- Payment generation ---
# ASSUMPTION: decline_reason category weights. Spec only specifies ordering
# (INSUFFICIENT_FUNDS and ISSUER_DOWN heaviest, terminal codes lightest).
DECLINE_REASON_WEIGHTS = {
    "INSUFFICIENT_FUNDS": 0.26,
    "ISSUER_DOWN": 0.20,
    "MANDATE_INSUFFICIENT_FUNDS": 0.12,
    "NETWORK_TIMEOUT": 0.12,
    "AFA_3DS_DROPOFF": 0.10,
    "RISK_DECLINE": 0.08,
    "LIMIT_EXCEEDED": 0.06,
    "CARD_EXPIRED": 0.03,
    "CARD_OR_ACCOUNT_BLOCKED": 0.02,
    "MANDATE_REVOKED": 0.01,
}

# Confirmed 2026-08-25 (DECISIONS.md): which decline reasons are valid per
# instrument_type, so e.g. MANDATE_REVOKED never appears on a card payment.
# Weights within each list are drawn from DECLINE_REASON_WEIGHTS above and
# renormalized. See docs/simulator_schema.md §2.
INSTRUMENT_VALID_DECLINE_REASONS = {
    "card": [
        "INSUFFICIENT_FUNDS",
        "ISSUER_DOWN",
        "NETWORK_TIMEOUT",
        "RISK_DECLINE",
        "AFA_3DS_DROPOFF",
        "LIMIT_EXCEEDED",
        "CARD_EXPIRED",
        "CARD_OR_ACCOUNT_BLOCKED",
    ],
    "upi": [
        "INSUFFICIENT_FUNDS",
        "ISSUER_DOWN",
        "NETWORK_TIMEOUT",
        "RISK_DECLINE",
        "LIMIT_EXCEEDED",
    ],
    "mandate": [
        "MANDATE_INSUFFICIENT_FUNDS",
        "ISSUER_DOWN",
        "NETWORK_TIMEOUT",
        "MANDATE_REVOKED",
    ],
}

# ASSUMPTION: instrument type mix. Spec doesn't give one.
INSTRUMENT_TYPE_WEIGHTS = {"card": 0.50, "upi": 0.35, "mandate": 0.15}

# ASSUMPTION: issuer pool and payment amount range. Spec doesn't give either;
# amounts are illustrative merchant transaction sizes in paise.
ISSUER_CODES = ["HDFC", "ICICI", "SBI", "AXIS", "KOTAK"]
AMOUNT_PAISE_MIN = 50_000  # Rs 500
AMOUNT_PAISE_MAX = 2_000_000  # Rs 20,000

# --- Persona generation ---
ANNOYANCE_THRESHOLD_POISSON_LAMBDA = 4
ANNOYANCE_THRESHOLD_FLOOR = 2
CONTACT_HOURS_DEFAULT = (9, 21)
CONTACT_HOURS_NARROW = (10, 18)
CONTACT_HOURS_NARROW_PROBABILITY = 0.10
CHANNEL_RESPONSE_RATE_MIN = 0.3
CHANNEL_RESPONSE_RATE_MAX = 0.9
LANGUAGE_WEIGHTS = {"hinglish": 0.5, "hi": 0.3, "en": 0.2}

# funds_arrival_day mixture (§1): 60% clustered day 1-3, 40% clustered day 25-30
FUNDS_ARRIVAL_EARLY_PROBABILITY = 0.6
FUNDS_ARRIVAL_EARLY_RANGE = (1, 3)
FUNDS_ARRIVAL_LATE_RANGE = (25, 30)

# --- Outage + decoy injection (§3) ---
# 2026-08-25 (DECISIONS.md): bumped from the schema's literal 20 to 90. At 20
# minutes, naive_fixed_retry's first attempt (+1hr) always lands after the
# outage has already cleared, so naive never gets caught by it and the
# outage-ablation comparison has nothing to demonstrate. 90 min overlaps
# naive's first retry while still clearing before its second.
TRUE_OUTAGE_DURATION_MINUTES = 90
# ASSUMPTION: decoy duration. Spec gives 20 min for the true outage but leaves
# the decoy's duration unspecified beyond "smaller ... burst".
DECOY_CLUSTER_DURATION_MINUTES = 15
# start_time as a fraction of window_days, so both events land mid-window and
# never at day 1 or day 30, per spec.
OUTAGE_START_FRACTION_RANGE = (0.25, 0.75)
# ASSUMPTION: how many extra same-issuer ISSUER_DOWN payments get injected
# into each window to actually create a detectable cluster (background
# density alone over a 20-minute slice of a 30-day window would place ~0
# payments there). Decoy is explicitly "smaller" than the true outage per §3.
TRUE_OUTAGE_CLUSTER_SIZE = 15
DECOY_CLUSTER_SIZE = 6
