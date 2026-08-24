"""Data types for the simulator, matching docs/simulator_schema.md field-for-field.

Customer (§1) and Payment (§2) carry only the fields listed in the schema tables.
Runtime state needed by the ground-truth function (contact_count, for the §5
annoyance/opt-out check) is deliberately NOT stored on Customer — it's passed
into success_probability() as an explicit argument instead, so both the
persona object stays exactly schema-shaped and the ground-truth function stays
pure (state comes in as an argument, nothing is mutated or read from outside).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, Optional, Tuple


class Language(str, Enum):
    EN = "en"
    HI = "hi"
    HINGLISH = "hinglish"


class InstrumentType(str, Enum):
    CARD = "card"
    UPI = "upi"
    MANDATE = "mandate"


class DeclineReason(str, Enum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    MANDATE_INSUFFICIENT_FUNDS = "MANDATE_INSUFFICIENT_FUNDS"
    ISSUER_DOWN = "ISSUER_DOWN"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    RISK_DECLINE = "RISK_DECLINE"
    CARD_EXPIRED = "CARD_EXPIRED"
    CARD_OR_ACCOUNT_BLOCKED = "CARD_OR_ACCOUNT_BLOCKED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    AFA_3DS_DROPOFF = "AFA_3DS_DROPOFF"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"


class ActionType(str, Enum):
    RETRY_NOW = "retry_now"
    RETRY_SCHEDULED = "retry_scheduled"
    SEND_PAYMENT_LINK = "send_payment_link"
    SEND_NUDGE = "send_nudge"
    SEND_INSTRUMENT_UPDATE_LINK = "send_instrument_update_link"
    ESCALATE_ALTERNATE_INSTRUMENT = "escalate_alternate_instrument"
    STOP = "stop"


class Channel(str, Enum):
    UPI_LINK = "upi_link"
    WHATSAPP = "whatsapp"
    SMS = "sms"


# §5: which action types are customer-facing (cost money, count toward annoyance)
# vs. silent (a backend retry the customer never sees). retry_now / retry_scheduled
# are the schema's own example of a silent action; stop takes no contact at all.
CUSTOMER_FACING_ACTION_TYPES = frozenset(
    {
        ActionType.SEND_PAYMENT_LINK,
        ActionType.SEND_NUDGE,
        ActionType.SEND_INSTRUMENT_UPDATE_LINK,
        ActionType.ESCALATE_ALTERNATE_INSTRUMENT,
    }
)


@dataclass(frozen=True)
class Customer:
    """§1 Customer persona. Hidden from the policy under test."""

    customer_id: str
    recovery_propensity: float
    funds_arrival_day: int
    contact_hours: Tuple[int, int]
    annoyance_threshold: int
    channel_response_rate: Dict[str, float]
    preferred_language: Language


@dataclass(frozen=True)
class Payment:
    """§2 Payment record."""

    payment_id: str
    customer_id: str
    amount_paise: int
    instrument_type: InstrumentType
    decline_reason: DeclineReason
    issuer_code: str
    failed_at: datetime


@dataclass(frozen=True)
class OutageEvent:
    """§3 Outage event. Two of these exist in a run: one true_outage, one decoy_cluster."""

    issuer_code: str
    start_time: datetime
    duration_minutes: int
    kind: str  # "true_outage" | "decoy_cluster"


@dataclass(frozen=True)
class Action:
    """The action a policy is taking, as passed into success_probability().

    `channel` is required for the categories whose formula multiplies by
    channel_response_rate[channel] (terminal categories via
    send_instrument_update_link, and AFA_3DS_DROPOFF via send_nudge); leave it
    None for silent/retry/stop actions. Per §6, this is the same value the
    §6 log entry persists as `channel` (`none` there for the None case here) —
    the ground-truth input and the audit record are one value, not two.

    `customer_facing` overrides the default CUSTOMER_FACING_ACTION_TYPES
    classification for this specific action instance. Schema §5's own phrasing
    ("a backend retry the customer never sees, e.g. retry_now with no message
    sent") is an example, not an absolute rule — whether a retry counts as a
    contact depends on whether a message went with it. Leave None to use the
    action_type default; set explicitly (e.g. naive_fixed_retry's retries,
    which do send a notification) to override it. See is_customer_facing() in
    contact_tracking.py.
    """

    action_type: ActionType
    channel: Optional[Channel] = None
    customer_facing: Optional[bool] = None
