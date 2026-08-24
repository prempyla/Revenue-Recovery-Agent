"""Contact-cost / annoyance bookkeeping. Schema §5.

success_probability() stays pure — it takes contact_count as an explicit
argument and checks it against annoyance_threshold internally (see
ground_truth.py), rather than mutating a stored .opted_out flag. This module
is the one place that actually accumulates contact_count over a run: it
replays a customer's action history and increments only on customer-facing
actions, which is what a harness calls to derive the argument
success_probability needs before each action.

Two distinct opt-out-shaped concepts live here, kept structurally separate
per DECISIONS.md 2026-08-25 (same distinction already drawn between
annoyance_threshold and MAX_WEEKLY_CONTACTS):
  - is_annoyance_threshold_exceeded(): hidden ground-truth persona PATIENCE
    (contact_count > annoyance_threshold) — the customer would silently stop
    responding, whether or not they ever said anything. Never known to any
    policy at decision time; only usable in post-hoc audits.
  - mark_opted_out()/is_opted_out(): an EXPLICIT STATED REQUEST — set only
    when llm.classify_reply() returns OPT_OUT for an actual reply, timestamped
    so a decide()-time veto and a harness post-hoc audit can both check it.
    Conflating these would produce a correct-looking but falsely-labeled
    audit trail (the log would say "hit the weekly cap" when the truth is
    "the customer asked to stop") — same class of problem as ABANDONED
    collapsing three distinct reasons into one.
"""

from datetime import datetime
from typing import Dict, Optional

from . import config
from .types import Action, CUSTOMER_FACING_ACTION_TYPES


def is_customer_facing(action: Action) -> bool:
    if action.customer_facing is not None:
        return action.customer_facing
    return action.action_type in CUSTOMER_FACING_ACTION_TYPES


def contact_cost_paise(action: Action) -> int:
    """§5: only customer-facing actions cost money; silent actions cost 0."""
    return config.COST_PER_CONTACT_PAISE if is_customer_facing(action) else 0


class ContactTracker:
    """Per-customer contact_count, incremented only by customer-facing actions.

    contact_count(customer_id) is what gets passed into
    ground_truth.success_probability() as its contact_count argument.
    """

    def __init__(self) -> None:
        self._counts: Dict[str, int] = {}
        self._opted_out_at: Dict[str, datetime] = {}

    def contact_count(self, customer_id: str) -> int:
        return self._counts.get(customer_id, 0)

    def is_annoyance_threshold_exceeded(self, customer_id: str, annoyance_threshold: int) -> bool:
        """Hidden ground-truth persona patience exhausted — see module docstring."""
        return self.contact_count(customer_id) > annoyance_threshold

    def record(self, customer_id: str, action: Action) -> None:
        if is_customer_facing(action):
            self._counts[customer_id] = self.contact_count(customer_id) + 1

    def mark_opted_out(self, customer_id: str, at: datetime) -> None:
        """Explicit stated request — set only by llm.reply_handling.apply_reply_intent
        on an OPT_OUT classification. First opt-out timestamp wins."""
        self._opted_out_at.setdefault(customer_id, at)

    def is_opted_out(self, customer_id: str, as_of: Optional[datetime] = None) -> bool:
        """Explicit stated request — see module docstring. With as_of, checks
        whether the opt-out was already in effect at that moment (so a
        candidate action scheduled before the opt-out reply arrived isn't
        retroactively vetoed); without it, checks whether the customer has
        EVER opted out."""
        opted_out_at = self._opted_out_at.get(customer_id)
        if opted_out_at is None:
            return False
        if as_of is None:
            return True
        return as_of >= opted_out_at

    def opted_out_at(self, customer_id: str) -> Optional[datetime]:
        return self._opted_out_at.get(customer_id)

    def all_opted_out(self) -> Dict[str, datetime]:
        """Full customer_id -> opt-out-timestamp map, for a harness to audit
        post-hoc after a run completes (see harness.RunResult.opted_out_at)."""
        return dict(self._opted_out_at)
