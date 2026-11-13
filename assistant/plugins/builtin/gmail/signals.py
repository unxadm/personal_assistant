"""Cheap deterministic signals for email: the first stage of the rules + LLM hybrid.

These run on every synced message at no cost. The proactive engine (Phase 5) sends only
messages with signals to the LLM for a real decision.
"""

from __future__ import annotations

import re
from typing import Any

_I = re.IGNORECASE

CATEGORIES: dict[str, re.Pattern[str]] = {
    "bill": re.compile(
        r"\b(invoice|bill(?:ing)? statement|payment (?:is )?due|amount due|balance due|"
        r"past due|overdue|pay by|your bill)\b", _I),
    "receipt": re.compile(
        r"\b(receipt|order confirmation|order #|payment received|payment confirmation|"
        r"thank you for your (?:purchase|order|payment))\b", _I),
    "travel": re.compile(
        r"\b(flight|itinerary|boarding pass|check-in|e-ticket|booking (?:confirmation|reference)|"
        r"reservation (?:confirmation|number)|departure|hotel reservation)\b", _I),
    "package": re.compile(
        r"\b(has shipped|shipped|out for delivery|tracking (?:number|no\.?|#)|"
        r"track your (?:package|order|shipment)|delivered|estimated delivery)\b", _I),
    "deadline": re.compile(
        r"\b(deadline|due (?:by|on|date)|no later than|expires? (?:on|in)|rsvp by|"
        r"by (?:end of day|eod|tomorrow|monday|tuesday|wednesday|thursday|friday)|"
        r"last day to)\b", _I),
    "action_request": re.compile(
        r"\b(please (?:reply|respond|confirm|review|sign|send|let me know|advise)|"
        r"can you|could you|action required|response required|needs? your|"
        r"waiting (?:for|on) your|get back to me)\b", _I),
}

_AMOUNT = re.compile(
    r"(?:[$€£]\s?\d[\d,]*(?:\.\d{2})?|\b\d[\d,]*(?:\.\d{2})?\s?(?:USD|EUR|GBP|NOK|SEK|DKK|CAD|AUD)\b)"
)
_TRACKING = [
    re.compile(r"\b1Z[0-9A-Z]{16}\b"),  # UPS
    re.compile(r"\b9[2-5]\d{20,24}\b"),  # USPS
    re.compile(r"tracking (?:number|no\.?|#|id)?\s*[:#]?\s*([A-Z0-9]{10,30})\b", _I),
]


def detect(subject: str, body: str) -> dict[str, Any]:
    """Return ``{"categories": [...], "amounts": [...], "tracking_numbers": [...]}``."""
    text = f"{subject}\n{body[:20000]}"
    categories = [name for name, pattern in CATEGORIES.items() if pattern.search(text)]
    amounts = list(dict.fromkeys(m.group(0).strip() for m in _AMOUNT.finditer(text)))[:5]
    tracking: list[str] = []
    for pattern in _TRACKING:
        for match in pattern.finditer(text):
            value = match.group(match.lastindex or 0)
            if any(ch.isdigit() for ch in value) and value not in tracking:
                tracking.append(value)
    if tracking and "package" not in categories:
        categories.append("package")
    return {"categories": categories, "amounts": amounts, "tracking_numbers": tracking[:5]}
