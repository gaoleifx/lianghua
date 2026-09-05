"""Read-only summary for a persisted strategy backtest state."""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
from datetime import date
from pathlib import Path


def _forward_signal_metrics(ledger: list[dict], history_db: str | None) -> dict:
    if not history_db:
        return {}
    connection = sqlite3.connect(history_db)
    try:
        cache: dict[str, list[tuple[str, float, float]]] = {}

        def bars(symbol: str) -> list[tuple[str, float, float]]:
            code = symbol.split(".")[-1]
            if code not in cache:
                cache[code] = connection.execute(
                    "select date, close, low from history where code=? order by date",
                    (code,),
                ).fetchall()
            return cache[code]

        result = {}
        for horizon in (5, 10):
            returns = []
            minimum_paths = []
            for fill in ledger:
                if fill.get("side") != "1":
                    continue
                rows = bars(fill["symbol"])
                dates = [row[0] for row in rows]
                if fill["date"] not in dates:
                    continue
                index = dates.index(fill["date"])
                if index + horizon >= len(rows):
                    continue
                entry = float(fill["price"])
                returns.append(float(rows[index + horizon][1]) / entry - 1.0)
                minimum_paths.append(min(float(row[2]) / entry - 1.0
                                          for row in rows[index + 1:index + horizon + 1]))
            result[f"forward_{horizon}d"] = {
                "count": len(returns),
                "mean_return": sum(returns) / len(returns) if returns else None,
                "win_rate": sum(value > 0 for value in returns) / len(returns)
                if returns else None,
                "mean_minimum_path": sum(minimum_paths) / len(minimum_paths)
                if minimum_paths else None,
                "worst_minimum_path": min(minimum_paths) if minimum_paths else None,
            }
        return result
    finally:
        connection.close()


def _gate_blocked_opportunity_metrics(event_log: str | None,
                                      history_db: str | None) -> dict:
    """Measure top scored symbols observed while the historical gate blocked entry."""
    if not event_log or not history_db:
        return {}
    events = []
    for line in Path(event_log).read_text(encoding="utf-8", errors="ignore").splitlines():
        if "gate_blocked_opportunity_audit" not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "gate_blocked_opportunity_audit":
            events.append(event)
    connection = sqlite3.connect(history_db)
    try:
        cache: dict[str, list[tuple[str, float]]] = {}

        def bars(symbol: str) -> list[tuple[str, float]]:
            code = symbol.split(".")[-1]
            if code not in cache:
                cache[code] = connection.execute(
                    "select date, close from history where code=? order by date", (code,)
                ).fetchall()
            return cache[code]

        returns = {5: [], 10: []}
        event_returns = {5: [], 10: []}
        for event in events:
            per_event = {5: [], 10: []}
            signal_date = str(event.get("as_of", ""))[:10]
            for symbol in event.get("qualified_symbols", []):
                rows = bars(symbol)
                index = next((i for i, row in enumerate(rows) if row[0][:10] >= signal_date), None)
                if index is None or index + 10 >= len(rows):
                    continue
                entry = float(rows[index][1])
                for horizon in (5, 10):
                    value = float(rows[index + horizon][1]) / entry - 1.0
                    returns[horizon].append(value)
                    per_event[horizon].append(value)
            for horizon in (5, 10):
                if per_event[horizon]:
                    event_returns[horizon].append(sum(per_event[horizon]) /
                                                  len(per_event[horizon]))

        result = {"events": len(events)}
        for horizon in (5, 10):
            values = returns[horizon]
            grouped = event_returns[horizon]
            result[f"forward_{horizon}d"] = {
                "count": len(values),
                "mean_return": sum(values) / len(values) if values else None,
                "win_rate": sum(value > 0 for value in values) / len(values)
                if values else None,
                "positive_event_rate": sum(value > 0 for value in grouped) / len(grouped)
                if grouped else None,
            }
        return result
    finally:
        connection.close()


def _execution_constraint_metrics(event_log: str | None) -> dict:
    """Summarize selection/order constraints from structured replay events."""
    if not event_log:
        return {}
    events = []
    for line in Path(event_log).read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") in {"order_blocked", "order_skipped", "rebalance_aborted"}:
            events.append(event)
    return {
        "event_count": len(events),
        "order_blocked": dict(collections.Counter(
            event.get("reason") for event in events if event.get("event") == "order_blocked")),
        "order_skipped": dict(collections.Counter(
            event.get("reason") for event in events if event.get("event") == "order_skipped")),
        "rebalance_aborted": dict(collections.Counter(
            event.get("reason") for event in events if event.get("event") == "rebalance_aborted")),
    }


def summarize(path: str, history_db: str | None = None,
              event_log: str | None = None) -> dict:
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    curve = sorted(state.get("equity_curve", []), key=lambda row: row["date"])
    assets = [float(row["asset"]) for row in curve]
    initial = assets[0] if assets else float(state.get("initial_asset", 0))
    final = assets[-1] if assets else initial
    peak = 0.0
    max_drawdown = 0.0
    for asset in assets:
        peak = max(peak, asset)
        if peak:
            max_drawdown = max(max_drawdown, 1.0 - asset / peak)
    ledger = state.get("trade_ledger", [])
    costs = {
        "commission": sum(float(row.get("commission_estimate", 0)) for row in ledger),
        "stamp_tax": sum(float(row.get("stamp_tax_estimate", 0)) for row in ledger),
        "slippage": sum(float(row.get("slippage_estimate", 0)) for row in ledger),
    }
    calendar_days = None
    annualized_return = None
    if curve:
        calendar_days = (date.fromisoformat(curve[-1]["date"]) -
                         date.fromisoformat(curve[0]["date"])).days
        if initial and final and calendar_days > 0:
            annualized_return = (final / initial) ** (365.25 / calendar_days) - 1.0
    gross_turnover = sum(float(row.get("price", 0)) * int(row.get("volume", 0))
                         for row in ledger)
    return {
        "start": curve[0]["date"] if curve else None,
        "end": curve[-1]["date"] if curve else None,
        "observations": len(curve),
        "initial_asset": initial,
        "final_asset": final,
        "return": final / initial - 1.0 if initial else None,
        "annualized_return": annualized_return,
        "calendar_days": calendar_days,
        "max_drawdown": max_drawdown,
        "zero_position_days": sum(row.get("positions_count", 0) == 0 for row in curve),
        "average_positions": (sum(row.get("positions_count", 0) for row in curve) / len(curve)
                              if curve else None),
        "average_gross_exposure": (sum(float(row.get("gross_exposure", 0)) for row in curve) / len(curve)
                                    if curve else None),
        "buys": sum(row.get("side") == "1" for row in ledger),
        "sells": sum(row.get("side") == "2" for row in ledger),
        "costs": costs,
        "total_cost_estimate": sum(costs.values()),
        "gross_turnover": gross_turnover,
        "turnover_multiple_of_initial": gross_turnover / initial if initial else None,
        "exit_reasons": dict(collections.Counter(row.get("reason") for row in ledger
                                                   if row.get("side") == "2")),
        "forward_signal_metrics": _forward_signal_metrics(ledger, history_db),
        "gate_blocked_opportunity_metrics": _gate_blocked_opportunity_metrics(
            event_log, history_db),
        "execution_constraint_metrics": _execution_constraint_metrics(event_log),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("state_json")
    parser.add_argument("--history-db", help="Optional SQLite history database for forward 5/10 day metrics")
    parser.add_argument("--event-log", help="Optional replay log with gate-blocked opportunity audit events")
    args = parser.parse_args()
    print(json.dumps(summarize(args.state_json, args.history_db, args.event_log),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
