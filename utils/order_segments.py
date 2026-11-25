"""
Turns a bot's order events and open-order snapshots into one segment per order: placed -> filled, cancelled or
still open. The orders chart draws each segment as a line at the order's price. Events give the exact start and
end; snapshots fill the gaps (an order placed before the window, or an event that never arrived).
"""
from typing import Dict, List, Optional

# Bots write an open-order snapshot every 5 s: an order seen this recently is still open.
SNAPSHOT_GRACE_S = 12
# Events that end an order. A partial fill ("partially_filled") is a fill marker; the order stays open.
TERMINAL_EVENTS = {"filled", "cancelled", "failed", "expired"}
FILL_EVENTS = {"partially_filled", "filled"}


def build_segments(events: List[Dict], snapshots: List[Dict], window_start: float, window_end: float,
                   latest_snapshot: Optional[float]) -> List[Dict]:
    """
    events:    {"time", "event" (created | partially_filled | filled | cancelled | failed | expired), "order_id",
                "side", "price", "amount", "level", "realized_pnl" (optional)}
    snapshots: {"order_id", "side", "price", "amount", "level", "first_seen", "last_seen"}, one row per order
    Times are epoch seconds. Returns segments sorted by start, each with an "outcome":
    open, filled, cancelled, failed, expired, or gone (it left the snapshots without an event).
    """
    orders: Dict[str, Dict] = {}
    for snapshot in snapshots:
        orders[snapshot["order_id"]] = {
            "order_id": snapshot["order_id"], "side": snapshot["side"], "price": snapshot["price"],
            "amount": snapshot["amount"], "level": snapshot.get("level"), "start": snapshot["first_seen"],
            "end": None, "outcome": None, "last_seen": snapshot["last_seen"], "realized_pnl": None,
        }
    for event in sorted(events, key=lambda e: e["time"]):
        order = orders.setdefault(event["order_id"], {
            "order_id": event["order_id"], "side": event["side"], "price": event["price"],
            "amount": event["amount"], "level": event.get("level"), "start": None, "end": None,
            "outcome": None, "last_seen": None, "realized_pnl": None,
        })
        if event["event"] == "created":
            order["start"] = event["time"]
        elif event["event"] in TERMINAL_EVENTS:
            order["end"] = event["time"]
            order["outcome"] = event["event"]
        if event.get("realized_pnl") is not None:
            order["realized_pnl"] = event["realized_pnl"]

    segments = []
    for order in orders.values():
        start = order["start"] if order["start"] is not None else window_start
        if order["end"] is None:
            seen_recently = (order["last_seen"] is not None and latest_snapshot is not None
                             and latest_snapshot - order["last_seen"] <= SNAPSHOT_GRACE_S)
            if seen_recently or (order["last_seen"] is None and order["start"] is not None):
                order["outcome"], end = "open", window_end
            else:
                order["outcome"], end = "gone", order["last_seen"] or start
        else:
            end = order["end"]
        start, end = max(start, window_start), min(end, window_end)
        if end < start:
            continue
        segments.append({**{key: value for key, value in order.items() if key not in ("start", "end", "last_seen")},
                         "start": start, "end": end})
    segments.sort(key=lambda segment: segment["start"])
    return segments
