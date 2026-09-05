# -*- coding: utf-8 -*-
"""Accumulate point-in-time daily main-fund-flow observations for later backtests.

The source currently exposes a rolling window, so this collector must run
periodically.  It records both the source trading date and collection time;
the strategy must never use a row collected after the simulated signal date.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)

from market_intelligence import _eastmoney_daily_flow, _sina_daily_flow


def _fetch_daily_flow(symbol):
    """Fetch one rolling window, falling back to Sina if Eastmoney is unavailable."""
    code = str(symbol).split(".")[-1]
    try:
        return _eastmoney_daily_flow(code), "eastmoney_20d"
    except Exception as eastmoney_error:
        try:
            return _sina_daily_flow(code), "sina_backup_20d"
        except Exception as sina_error:
            raise RuntimeError("eastmoney=%s; sina=%s" % (eastmoney_error, sina_error))


def _codes_from_database(database):
    with sqlite3.connect(database) as con:
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        table = "stocks" if "stocks" in tables else "history" if "history" in tables else None
        if table is None:
            raise RuntimeError("database must contain stocks(code) or history(code)")
        rows = con.execute("SELECT DISTINCT code FROM %s ORDER BY code" % table).fetchall()
    return [str(row[0]) for row in rows if row and row[0] is not None]


def collect(codes, output_path):
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    existing = set()
    if os.path.exists(output_path):
        with open(output_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    existing.add((row.get("symbol"), row.get("date")))
                except (json.JSONDecodeError, TypeError):
                    continue
    collected_at = datetime.now(timezone.utc).isoformat()
    added = 0
    with open(output_path, "a", encoding="utf-8") as handle:
        for code in codes:
            symbol = str(code).strip()
            if not symbol:
                continue
            try:
                rows, source = _fetch_daily_flow(symbol)
            except Exception as exc:
                print(json.dumps({"symbol": symbol, "error": str(exc)}, ensure_ascii=False))
                continue
            for row in rows:
                key = (symbol, row.get("date"))
                if not row.get("date") or key in existing:
                    continue
                record = {"symbol": symbol, "date": row["date"],
                          "main_net": float(row.get("main_net", 0.0)),
                          "collected_at": collected_at, "source": source}
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                existing.add(key)
                added += 1
    return added


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("codes", nargs="*", help="six-digit codes or strategy symbols")
    parser.add_argument("--codes-file", help="text file containing one code per line")
    parser.add_argument("--database", help="SQLite database containing a stocks(code) table")
    parser.add_argument("--max-codes", type=int, default=0,
                        help="optional cap after combining positional and file codes")
    parser.add_argument("--output", default=os.environ.get(
        "GOLDMINER_FUND_FLOW_CACHE", "D:\\股票数据库\\fund_flow_daily.jsonl"))
    args = parser.parse_args()
    codes = list(args.codes)
    if args.codes_file:
        with open(args.codes_file, encoding="utf-8") as handle:
            codes.extend(line.strip() for line in handle if line.strip())
    if args.database:
        codes.extend(_codes_from_database(args.database))
    codes = list(dict.fromkeys(codes))
    if args.max_codes > 0:
        codes = codes[:args.max_codes]
    if not codes:
        parser.error("provide codes or --codes-file")
    print(json.dumps({"requested": len(codes), "added": collect(codes, args.output),
                      "output": os.path.abspath(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
