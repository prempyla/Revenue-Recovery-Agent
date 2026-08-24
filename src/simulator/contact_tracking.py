"""Contact-cost / annoyance bookkeeping. Schema §5.

success_probability() stays pure — it takes contact_count as an explicit
argument and checks it against annoyance_threshold internally (see
ground_truth.py), rather than mutating a stored .opted_out flag. This module
is the one place that actually accumulates contact_count over a run: it
replays a customer's action history and increments only on customer-facing
actions, which is what a harness calls to derive the argument
success_probability needs before each action.
"""

from typing import Dict

from . import config
from .types import Action, CUSTOMER_FACING_ACTION_TYPES


def is_customer_facing(action: Action) -> bool:
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

    def contact_count(self, customer_id: str) -> int:
        return self._counts.get(customer_id, 0)

    def is_opted_out(self, customer_id: str, annoyance_threshold: int) -> bool:
        return self.contact_count(customer_id) > annoyance_threshold

    def record(self, customer_id: str, action: Action) -> None:
        if is_customer_facing(action):
            self._counts[customer_id] = self.contact_count(customer_id) + 1
