"""Razorpay client interface. One method this round: create_payment_link.

RealRazorpayClient wraps the official SDK against test-mode keys read from
.env. FakeRazorpayClient is what every test in this repo actually uses — no
network calls happen from this session, per the agreed scope. See
docs/manual_webhook_verification.md for the one manual step (a human paying
a real test-mode link in a browser) that this layer cannot self-verify.

reference_id is set to our own deterministic idempotency key on every call.
Razorpay's Payment Links API does not support true idempotency-key replay
(that's Payouts/Refunds only — see DECISIONS.md) — a duplicate reference_id
gets rejected with an error, not a replayed response. Passing it is still
worthwhile defense-in-depth (a genuine duplicate call is rejected server-side
too, not just prevented by our own outbox), but a resumed-after-crash retry
that hits this conflict is treated as execution_error this round, not
reconciled as "actually already succeeded" — see outbox.py.
"""

import os
from abc import ABC, abstractmethod
from typing import Optional, TypedDict


class PaymentLinkResult(TypedDict):
    id: str
    short_url: str
    status: str
    reference_id: str


class RazorpayClientInterface(ABC):
    @abstractmethod
    def create_payment_link(
        self,
        *,
        amount_paise: int,
        customer_name: str,
        customer_contact: str,
        reference_id: str,
        description: str = "",
    ) -> PaymentLinkResult: ...

    @abstractmethod
    def fetch_payment_link_status(self, reference_id: str) -> Optional[PaymentLinkResult]:
        """Current state of the link created with this reference_id, or None
        if none was ever created. Used by reconciliation.py to recover from
        a gap where the outcome was never locally recorded (a crash mid-call,
        or a webhook that never arrived)."""
        ...


class RealRazorpayClient(RazorpayClientInterface):
    def __init__(self, key_id: Optional[str] = None, key_secret: Optional[str] = None):
        import razorpay

        key_id = key_id or os.environ["RAZORPAY_KEY_ID"]
        key_secret = key_secret or os.environ["RAZORPAY_KEY_SECRET"]
        self._client = razorpay.Client(auth=(key_id, key_secret))

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        customer_name: str,
        customer_contact: str,
        reference_id: str,
        description: str = "",
    ) -> PaymentLinkResult:
        response = self._client.payment_link.create(
            {
                "amount": amount_paise,
                "currency": "INR",
                "reference_id": reference_id,
                "description": description,
                "customer": {"name": customer_name, "contact": customer_contact},
                # Disabled: customer_contact is a synthetic placeholder (see
                # orchestrator.py), not a real number -- notifying it would
                # either fail or hit a real phone that isn't the customer's.
                "notify": {"sms": False, "email": False},
                "reminder_enable": True,
            }
        )
        return {
            "id": response["id"],
            "short_url": response["short_url"],
            "status": response["status"],
            "reference_id": response.get("reference_id", reference_id),
        }

    def fetch_payment_link_status(self, reference_id: str) -> Optional[PaymentLinkResult]:
        # UNVERIFIED against the live API this round -- no network calls were
        # made from this session for this method (the manual verification
        # procedure only exercised create + webhook receipt). Razorpay's
        # Payment Links list endpoint is documented to support a reference_id
        # filter; assuming payment_link.all({"reference_id": ...}) and taking
        # the first match. If this doesn't behave as assumed, confirm/correct
        # it the same way the webhook envelope was: a real call, inspected,
        # not guessed twice.
        response = self._client.payment_link.all({"reference_id": reference_id})
        items = response.get("items", [])
        if not items:
            return None
        entity = items[0]
        return {
            "id": entity["id"],
            "short_url": entity["short_url"],
            "status": entity["status"],
            "reference_id": entity.get("reference_id", reference_id),
        }


class FakeRazorpayClient(RazorpayClientInterface):
    """In-memory fake. Records every call; raises on a reference_id it's
    already seen, mirroring Razorpay's real reference_id-uniqueness error
    (not a replayed response — see module docstring)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._seen_reference_ids: set[str] = set()
        self._next_id = 1
        self._links_by_reference_id: dict[str, dict] = {}

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        customer_name: str,
        customer_contact: str,
        reference_id: str,
        description: str = "",
    ) -> PaymentLinkResult:
        call = {
            "amount_paise": amount_paise,
            "customer_name": customer_name,
            "customer_contact": customer_contact,
            "reference_id": reference_id,
            "description": description,
        }
        self.calls.append(call)

        if reference_id in self._seen_reference_ids:
            raise RuntimeError(f"reference_id already exists: {reference_id}")
        self._seen_reference_ids.add(reference_id)

        link_id = f"plink_fake_{self._next_id:04d}"
        self._next_id += 1
        result: PaymentLinkResult = {
            "id": link_id,
            "short_url": f"https://fake.razorpay.link/{link_id}",
            "status": "created",
            "reference_id": reference_id,
        }
        self._links_by_reference_id[reference_id] = dict(result)
        return result

    def fetch_payment_link_status(self, reference_id: str) -> Optional[PaymentLinkResult]:
        record = self._links_by_reference_id.get(reference_id)
        return dict(record) if record else None

    def set_status(self, reference_id: str, status: str) -> None:
        """Test helper: simulate Razorpay-side status changing (e.g. the
        customer paid, or the link expired) independent of anything we know
        locally -- exactly the gap the reconciliation poller exists to close.
        """
        if reference_id not in self._links_by_reference_id:
            raise KeyError(reference_id)
        self._links_by_reference_id[reference_id]["status"] = status
