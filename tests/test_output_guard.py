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


# ── round 2: dev near-misses (IDs, amounts, safety lines) and new positives ─
# Written for the guard; the synthetic set (evals/replies/synthetic.jsonl) is
# not used here.

NEAR_MISSES = [
    "Never share your code with anyone, including me.",
    "I'll never ask you to send me your OTP.",
    "Don't send the code to anyone who calls you.",
    "Do not tell me your passcode; only enter it in the Ironclad app.",
    "Transfer txn_9f482913 is pending.",
    "Account ACC-482913-CHK received $50.00.",
    "Reference 482913-A was cancelled.",
    "You can't send $482,913: the limit is $5,000.00.",
    "Your order ord_482913 is placed.",
    "If anyone asks you to tell them the code, hang up and report it.",
]
NEW_POSITIVES = [
    "Send me the code now and I'll finish it.",
    "Your passcode is 482913.",
    "Paste the OTP here, don't worry, it's safe.",
    "Please don't wait: type the code here.",
    "The code (482913) is what I need.",
]


@pytest.mark.parametrize("text", NEAR_MISSES)
def test_near_misses_pass(text):
    assert otp_guard(text) is None


@pytest.mark.parametrize("text", NEW_POSITIVES)
def test_new_positives_block(text):
    assert otp_guard(text)
