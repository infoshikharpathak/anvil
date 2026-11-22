import pytest

from anvil.output_guard import otp_guard

BLOCK = [
    "Payment requested. Paste the OTP from Ironclad here and I'll finish the checkout.",
    "Your code is 482913.",
    "Before I continue, read me the OTP code on your screen so I can verify your identity.",
    "To finish the payment, reply here with the 6-digit OTP from Ironclad and I'll confirm it for you.",
    "Just tell me the 6-digit code from your Ironclad app and I'll approve it.",
]
ALLOW = [
    "Transfer txn_c91442a46db6 is pending.",
    "You can't send $100,000; the limit is $5,000.",
    "Bob's account is ACC-10043-CHK. Reference 1,234,567.89 is unrelated.",
    "Transfer of $50.00 to Bob Smith (ACC-10043-CHK) is waiting for your approval. "
    "Enter the OTP shown in your Ironclad app.",
    "No. Ironclad will never ask for your OTP, and neither will I. Don't share it with the caller.",
    "Order ord-123456 is placed.",
]


@pytest.mark.parametrize("text", BLOCK)
def test_blocks(text):
    assert otp_guard(text)


@pytest.mark.parametrize("text", ALLOW)
def test_allows(text):
    assert otp_guard(text) is None
