"""Read-only health summary for a strategy simulation log."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def summarize(path: str) -> dict:
    counts = {
        "initialized": 0,
        "account_unavailable": 0,
        "portfolio_risk_degraded": 0,
        "bar": 0,
        "order_submitted": 0,
        "order_status": 0,
        "trade": 0,
    }
    last_event = None
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = event.get("event")
        if name == "initialized":
            counts["initialized"] += 1
        if "account_unavailable" in str(event.get("error", "")):
            counts["account_unavailable"] += 1
        if name == "portfolio_risk_degraded":
            counts["portfolio_risk_degraded"] += 1
        if name in counts:
            counts[name] += 1
        if name in {"bar", "order_submitted", "order_status", "trade"}:
            last_event = event
    healthy = (counts["initialized"] >= 1 and
               counts["account_unavailable"] == 0 and
               counts["portfolio_risk_degraded"] == 0)
    if not healthy:
        observation_status = "degraded"
    elif counts["bar"] == 0:
        observation_status = "waiting_for_market_events"
    else:
        observation_status = "market_events_received"
    return {"log": str(Path(path)), "healthy_for_observation": healthy,
            "observation_status": observation_status, "counts": counts,
            "last_relevant_event": last_event}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime_log")
    args = parser.parse_args()
    print(json.dumps(summarize(args.runtime_log), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
