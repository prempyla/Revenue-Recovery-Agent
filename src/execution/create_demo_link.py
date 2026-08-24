"""One-off script: create a single real test-mode payment link via
RealRazorpayClient, for the manual verification procedure in
docs/manual_webhook_verification.md. This is the ONLY place in this repo
that makes a real network call to Razorpay -- everything else (including
every test) uses FakeRazorpayClient.

Usage: .venv/bin/python -m execution.create_demo_link
"""

import dotenv

from .razorpay_client import RealRazorpayClient

dotenv.load_dotenv()


def main() -> None:
    client = RealRazorpayClient()
    result = client.create_payment_link(
        amount_paise=100,  # Rs 1 -- smallest sensible test amount
        customer_name="Manual Verification Test",
        customer_contact="9000000000",
        reference_id="manual-verification-pay:attempt:1",
        description="Manual webhook verification -- see docs/manual_webhook_verification.md",
    )
    print(result)


if __name__ == "__main__":
    main()
