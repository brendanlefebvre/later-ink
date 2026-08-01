import httpx

STRIPE_API = "https://api.stripe.com/v1"


async def verify_checkout_session(session_id: str, api_key: str) -> bool:
    """True if the Stripe Checkout Session exists and was paid."""
    if not session_id.startswith("cs_"):
        return False
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{STRIPE_API}/checkout/sessions/{session_id}",
            auth=(api_key, ""),
        )
    if resp.status_code != 200:
        return False
    return resp.json().get("payment_status") == "paid"
