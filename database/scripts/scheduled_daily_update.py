# -*- coding: utf-8 -*-
"""Scheduled daily market-data update with lock, audit and machine-readable status."""
import json
import os
import sqlite3
import subprocess
import sys
import socket
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(ROOT, "progress", "scheduled_update.lock")
STATUS = os.path.join(ROOT, "progress", "data_freshness.json")
LOG_DIR = os.path.join(ROOT, "logs")
TERMINAL_EXE = os.environ.get("GOLDMINER_TERMINAL_EXE", r"C:\Programs\Minmetals Goldminer\minmetals.exe")
TERMINAL_HOST = "127.0.0.1"
TERMINAL_PORT = 7001


def atomic_json(path, payload):
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def terminal_ready():
    try:
        with socket.create_connection((TERMINAL_HOST, TERMINAL_PORT), timeout=1):
            return True
    except OSError:
        return False


def ensure_terminal(log, timeout=90):
    if terminal_ready():
        log.write("终端服务已启动，继续执行数据同步\n")
        return
    if not os.path.exists(TERMINAL_EXE):
        raise RuntimeError("GoldMiner终端不存在: %s" % TERMINAL_EXE)
    log.write("检测到终端服务未启动，自动启动: %s\n" % TERMINAL_EXE)
    try:
        subprocess.Popen([TERMINAL_EXE], cwd=os.path.dirname(TERMINAL_EXE),
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except OSError as exc:
        raise RuntimeError("GoldMiner终端启动失败: %s" % exc) from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if terminal_ready():
            log.write("GoldMiner终端服务已就绪\n")
            return
        time.sleep(1)
    raise RuntimeError("GoldMiner终端启动超时，%s秒内端口%d未就绪" % (timeout, TERMINAL_PORT))


def latest_data():
    with sqlite3.connect(os.path.join(ROOT, "stocks.db")) as con:
        latest = con.execute("SELECT MAX(date) FROM history WHERE date GLOB '????-??-??'").fetchone()[0]
        coverage = con.execute("SELECT COUNT(DISTINCT code) FROM history WHERE date=?", (latest,)).fetchone()[0]
        complete_row = con.execute("""SELECT date, COUNT(DISTINCT code) FROM history
            WHERE date GLOB '????-??-??' GROUP BY date
            HAVING COUNT(DISTINCT code) >= 760 ORDER BY date DESC LIMIT 1""").fetchone()
        complete_latest, complete_coverage = complete_row if complete_row else (None, 0)
        index_latest = dict(con.execute(
            "SELECT symbol,MAX(date) FROM index_history GROUP BY symbol"))
        bad_index_returns = con.execute("""SELECT COUNT(*) FROM (
            SELECT symbol,date,ABS(close/LAG(close) OVER(PARTITION BY symbol ORDER BY date)-1) r
            FROM index_history) WHERE r>0.15""").fetchone()[0]
    return latest, coverage, complete_latest, complete_coverage, index_latest, bad_index_returns


def build_health_payload(base, latest, coverage, complete_latest, complete_coverage, index_latest, bad_index_returns,
                         expected, sync_exit_code, audit_exit_code, log_path):
    required_indices = {"SHSE.000906", "SHSE.000300", "SHSE.000905"}
    indices_fresh = (set(index_latest) == required_indices and
                     all(value >= expected for value in index_latest.values()))
    healthy = (sync_exit_code == 0 and audit_exit_code == 0 and latest is not None and
               complete_latest is not None and complete_latest >= expected and
               complete_coverage >= 760 and indices_fresh and
               bad_index_returns == 0)
    base.update(status="healthy" if healthy else "degraded",
                finished_at=datetime.now().isoformat(),
                latest_trade_date=latest, expected_trade_date=expected,
                latest_complete_trade_date=complete_latest,
                latest_coverage=coverage, complete_coverage=complete_coverage,
                index_latest_dates=index_latest,
                index_data_fresh=indices_fresh,
                abnormal_index_return_rows=bad_index_returns,
                sync_exit_code=sync_exit_code, audit_exit_code=audit_exit_code,
                log=log_path)
    return healthy


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
            ensure_terminal(log)
            sync = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "auto_sync_data.py")],
                                  cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            audit = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "audit_database.py")],
                                   cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        latest, coverage, complete_latest, complete_coverage, index_latest, bad_index_returns = latest_data()
        expected = acceptable_date(now) if now.time() >= datetime.strptime("15:45:00", "%H:%M:%S").time() else complete_latest
        healthy = build_health_payload(
            payload, latest, coverage, complete_latest, complete_coverage, index_latest, bad_index_returns, expected,
            sync.returncode, audit.returncode, log_path)
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
