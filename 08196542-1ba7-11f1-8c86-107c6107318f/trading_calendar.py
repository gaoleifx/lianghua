"""本地可审计的A股交易日判断。节假日由 trading_calendar.json 维护。"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def is_trading_day(day: date, calendar_path: Path) -> bool:
    data = json.loads(calendar_path.read_text(encoding="utf-8"))
    closed = set(data.get("closed_dates", []))
    opened = set(data.get("open_dates", []))
    value = day.isoformat()
    if value in opened:
        return True
    if value in closed:
        return False
    trade_dates = set(data.get("trade_dates", []))
    if trade_dates:
        return value in trade_dates
    return day.weekday() < 5


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--calendar", default="trading_calendar.json")
    args = parser.parse_args()
    print("trading" if is_trading_day(date.fromisoformat(args.date), Path(args.calendar)) else "closed")
