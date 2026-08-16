# -*- coding: utf-8 -*-
"""统一审计股票数据库与中证800覆盖率，只读。"""
import io
import os
import sqlite3
import sys

from gm.api import set_token, stk_get_index_constituents

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def user_environment(name):
    value = os.environ.get(name)
    if value: return value
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except (OSError, ImportError): return ""


def scalar(con, sql, params=()): return con.execute(sql, params).fetchone()[0]


def main():
    set_token(user_environment("GM_TOKEN"))
    constituents = stk_get_index_constituents(index="SHSE.000906")
    codes = {str(x).split(".")[-1] for x in constituents["symbol"]}
    with sqlite3.connect(os.path.join(ROOT, "stocks.db")) as con:
        master = {x[0] for x in con.execute("SELECT code FROM stocks")}
        history = {x[0] for x in con.execute("SELECT DISTINCT code FROM history")}
        latest = scalar(con, "SELECT MAX(date) FROM history WHERE date GLOB '????-??-??'")
        bad_dates = scalar(con, "SELECT COUNT(*) FROM history WHERE date NOT GLOB '????-??-??'")
        rows = scalar(con, "SELECT COUNT(*) FROM history")
        unknown = scalar(con, "SELECT COUNT(*) FROM stocks WHERE sector IS NULL OR sector='' OR UPPER(sector)='UNKNOWN'")
        index_table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_history'").fetchone()
        index_counts = dict(con.execute(
            "SELECT symbol,COUNT(*) FROM index_history GROUP BY symbol")) if index_table else {}
        bad_index_returns = scalar(con, """SELECT COUNT(*) FROM (
            SELECT symbol,date,ABS(close/LAG(close) OVER(PARTITION BY symbol ORDER BY date)-1) r
            FROM index_history) WHERE r>0.15""") if index_table else -1
    with sqlite3.connect(os.path.join(ROOT, "financial.db")) as con:
        financial = {x[0] for x in con.execute("SELECT DISTINCT code FROM financial")}
        financial_rows = scalar(con, "SELECT COUNT(*) FROM financial")
        latest_report = scalar(con, "SELECT MAX(report_date) FROM financial")
    print("stocks主表:", len(master), "history行数:", rows, "最新行情:", latest)
    print("异常日期记录:", bad_dates)
    print("financial行数:", financial_rows, "最新报告期:", latest_report)
    print("中证800主表覆盖: %d/800 (%.1f%%)" % (len(codes & master), len(codes & master)/8))
    print("中证800行情覆盖: %d/800 (%.1f%%)" % (len(codes & history), len(codes & history)/8))
    print("中证800财务覆盖: %d/800 (%.1f%%)" % (len(codes & financial), len(codes & financial)/8))
    print("行业未知记录:", unknown)
    print("指数行情覆盖:", index_counts)
    print("指数异常单日涨跌记录:", bad_index_returns)
    if codes - master: print("缺主表:", ",".join(sorted(codes-master)))
    if codes - history: print("缺行情:", ",".join(sorted(codes-history)))


if __name__ == "__main__":
    main()
