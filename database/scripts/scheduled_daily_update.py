# -*- coding: utf-8 -*-
"""Scheduled daily market-data update with lock, audit and machine-readable status."""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(ROOT, "progress", "scheduled_update.lock")
STATUS = os.path.join(ROOT, "progress", "data_freshness.json")
LOG_DIR = os.path.join(ROOT, "logs")


def atomic_json(path, payload):
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def latest_data():
    with sqlite3.connect(os.path.join(ROOT, "stocks.db")) as con:
        latest = con.execute("SELECT MAX(date) FROM history WHERE date GLOB '????-??-??'").fetchone()[0]
        coverage = con.execute("SELECT COUNT(DISTINCT code) FROM history WHERE date=?", (latest,)).fetchone()[0]
        index_latest = dict(con.execute(
            "SELECT symbol,MAX(date) FROM index_history GROUP BY symbol"))
        bad_index_returns = con.execute("""SELECT COUNT(*) FROM (
            SELECT symbol,date,ABS(close/LAG(close) OVER(PARTITION BY symbol ORDER BY date)-1) r
            FROM index_history) WHERE r>0.15""").fetchone()[0]
    return latest, coverage, index_latest, bad_index_returns


def acceptable_date(now):
    day = now.date()
    # At 15:45 on a weekday, today's bar is expected. On weekends expect Friday.
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def main():
    os.makedirs(os.path.dirname(LOCK), exist_ok=True); os.makedirs(LOG_DIR, exist_ok=True)
    now = datetime.now(); payload = {"started_at": now.isoformat(), "status": "running"}
    if os.path.exists(LOCK):
        age = now.timestamp() - os.path.getmtime(LOCK)
        if age < 4 * 3600:
            payload.update(status="skipped_locked", lock_age_seconds=int(age)); atomic_json(STATUS, payload); return 2
        os.remove(LOCK)
    open(LOCK, "w", encoding="utf-8").write(str(os.getpid()))
    log_path = os.path.join(LOG_DIR, "scheduled_daily_%s.log" % now.strftime("%Y%m%d_%H%M%S"))
    try:
        with open(log_path, "w", encoding="utf-8") as log:
            sync = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "auto_sync_data.py")],
                                  cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            audit = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "audit_database.py")],
                                   cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        latest, coverage, index_latest, bad_index_returns = latest_data(); expected = acceptable_date(now)
        required_indices = {"SHSE.000906", "SHSE.000300", "SHSE.000905"}
        indices_fresh = (set(index_latest) == required_indices and
                         all(value >= expected for value in index_latest.values()))
        healthy = (sync.returncode == 0 and audit.returncode == 0 and latest >= expected and
                   coverage >= 760 and indices_fresh and bad_index_returns == 0)
        payload.update(status="healthy" if healthy else "degraded", finished_at=datetime.now().isoformat(),
                       latest_trade_date=latest, expected_trade_date=expected, latest_coverage=coverage,
                       index_latest_dates=index_latest, index_data_fresh=indices_fresh,
                       abnormal_index_return_rows=bad_index_returns,
                       sync_exit_code=sync.returncode, audit_exit_code=audit.returncode, log=log_path)
        atomic_json(STATUS, payload)
        return 0 if healthy else 1
    except Exception as exc:
        payload.update(status="failed", finished_at=datetime.now().isoformat(), error=str(exc), log=log_path)
        atomic_json(STATUS, payload); return 1
    finally:
        try: os.remove(LOCK)
        except OSError: pass


if __name__ == "__main__":
    raise SystemExit(main())
