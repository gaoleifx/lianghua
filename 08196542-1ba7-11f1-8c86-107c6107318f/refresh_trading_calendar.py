"""Refresh the cached A-share trading calendar using AkShare, preserving overrides."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


def refresh(path: Path) -> None:
    import akshare as ak

    frame = ak.tool_trade_date_hist_sina()
    dates = sorted(str(value)[:10] for value in frame["trade_date"].tolist())
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    current["trade_dates"] = dates
    current["last_refreshed_at"] = datetime.now().astimezone().isoformat()
    fd, tmp = tempfile.mkstemp(prefix="calendar_", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(current, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--calendar", default="trading_calendar.json")
    args = parser.parse_args()
    refresh(Path(args.calendar))
    print("calendar refreshed")
