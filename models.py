"""
Pure-logic functions for pricing, queue scoring, and reserve pricing.
No I/O or database access — these are stateless calculations.
"""

import math
from datetime import datetime, timezone


def block_price(chunks: int, full_block_cost: int) -> int:
    """
    Tiered pricing based on the land's current chunk count.

    0-2 chunks:  Free
    3-5 chunks:  7 ems
    6-10 chunks: 16 ems
    11-16 chunks: 25 ems
    17-25 chunks: 32 ems
    26+:         full_block_cost
    """
    if chunks <= 2:
        return 0
    elif chunks <= 5:
        return 7
    elif chunks <= 10:
        return 16
    elif chunks <= 16:
        return 25
    elif chunks <= 25:
        return 32
    else:
        return full_block_cost


def reserve_price(chunks: int, full_block_cost: int) -> int:
    """1.5× the normal block price, rounded up."""
    return math.ceil(block_price(chunks, full_block_cost) * 1.5)


def queue_score(
    chunks: int,
    members: int,
    days_waiting: int,
    vault: bool = False,
    builds: bool = False,
) -> int:
    """
    Calculate a land's queue priority score. Higher = higher priority.

    +20 if claiming vault
    +10 if claiming builds
    +5  per day spent in queue
    +2  per land member
    -1  per chunk already claimed
    """
    score = 0
    if vault:
        score += 20
    if builds:
        score += 10
    score += days_waiting * 5
    score += members * 2
    score -= chunks
    return score


def days_in_queue(requested_at_iso: str) -> int:
    """
    Compute whole days elapsed since a request was submitted.
    Uses UTC to avoid timezone drift.
    """
    requested_at = datetime.fromisoformat(requested_at_iso).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - requested_at
    return max(0, delta.days)


def price_tier_label(chunks: int, full_block_cost: int) -> str:
    """Human-readable label for the current price tier."""
    price = block_price(chunks, full_block_cost)
    if price == 0:
        return "Free (0-2 chunks)"
    elif price == 7:
        return "7 ems (3-5 chunks)"
    elif price == 16:
        return "16 ems (6-10 chunks)"
    elif price == 25:
        return "25 ems (11-16 chunks)"
    elif price == 32:
        return "32 ems (17-25 chunks)"
    else:
        return f"{full_block_cost} ems (26+ chunks)"
