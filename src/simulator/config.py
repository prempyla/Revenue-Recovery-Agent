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
