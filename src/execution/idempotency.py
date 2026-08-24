"""Deterministic idempotency keys. Per DECISIONS.md: derived from
(payment_id, attempt_number), not a random UUID per attempt — a random key
gives zero protection against a crash-and-replay, since the replay would
generate a fresh key and the duplicate-request check (ours, and Razorpay's
reference_id uniqueness on Payment Links) would never see it as a repeat.
"""


def make_idempotency_key(payment_id: str, attempt_number: int) -> str:
    return f"{payment_id}:attempt:{attempt_number}"
