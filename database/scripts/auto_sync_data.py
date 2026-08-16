# -*- coding: utf-8 -*-
"""中证800自动同步：行业、近期日线、审计。支持重复运行与断点续跑。"""
import io
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime

import pandas as pd
from gm.api import (ADJUST_NONE, history_n, set_token, stk_get_index_constituents,
                    stk_get_symbol_industry)

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCKS_DB = os.path.join(ROOT, "stocks.db")
FINANCIAL_DB = os.path.join(ROOT, "financial.db")
PROGRESS = os.path.join(ROOT, "progress", "auto_sync_index800.json")
LOG_DIR = os.path.join(ROOT, "logs")
INDEX_SYMBOLS = ["SHSE.000906", "SHSE.000300", "SHSE.000905"]


def user_environment(name):
    if os.environ.get(name): return os.environ[name]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except (OSError, ImportError): return ""


def setup_log():
    os.makedirs(LOG_DIR, exist_ok=True); os.makedirs(os.path.dirname(PROGRESS), exist_ok=True)
    path = os.path.join(LOG_DIR, "auto_sync_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()])
    return path


def save_progress(data):
    temp = PROGRESS + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle: json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temp, PROGRESS)


def load_progress():
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(PROGRESS, encoding="utf-8") as handle: data = json.load(handle)
        if data.get("run_date") == today:
            return data
    except (OSError, ValueError):
        pass
    return {"run_date": today, "completed_history": []}


def symbols():
    frame = stk_get_index_constituents(index="SHSE.000906")
    return frame["symbol"].dropna().astype(str).drop_duplicates().tolist()


def sync_industries(items):
    frame = stk_get_symbol_industry(symbols=",".join(items), source="zjh2012", level=1, date="")
    if frame is None or len(frame) < 700: raise RuntimeError("行业数据返回不足")
    with sqlite3.connect(STOCKS_DB) as con:
        con.executemany("UPDATE stocks SET sector=? WHERE code=?",
                        [(str(r.industry_name), str(r.symbol).split(".")[-1]) for r in frame.itertuples()])
        con.commit()
    logging.info("行业同步完成: %d", len(frame))


def sync_history(items, progress):
    done = set(progress.get("completed_history", [])); success = fail = 0
    for index, symbol in enumerate(items, 1):
        if symbol in done: continue
        try:
            frame = history_n(symbol=symbol, frequency="1d", count=180,
                              fields="symbol,eob,open,close,high,low,volume",
                              adjust=ADJUST_NONE, df=True)
            records = []
            if frame is not None:
                for row in frame.itertuples():
                    records.append((symbol.split(".")[-1], pd.Timestamp(row.eob).strftime("%Y-%m-%d"),
                                    float(row.open), float(row.close), float(row.high), float(row.low), int(row.volume)))
            if records:
                with sqlite3.connect(STOCKS_DB) as con:
                    con.executemany("INSERT OR IGNORE INTO history(code,date,open,close,high,low,volume) VALUES(?,?,?,?,?,?,?)", records)
                    con.execute("UPDATE stocks SET price=?,last_update=? WHERE code=?",
                                (records[-1][3], datetime.now().isoformat(timespec="seconds"), records[-1][0]))
                    con.commit()
                success += 1
            else: fail += 1
        except Exception as exc:
            fail += 1; logging.warning("行情失败 %s: %s", symbol, exc)
        if records:
            done.add(symbol)
        progress["run_date"] = datetime.now().strftime("%Y-%m-%d")
        progress["completed_history"] = sorted(done); progress["updated"] = datetime.now().isoformat()
        if index % 20 == 0: save_progress(progress); logging.info("行情进度 %d/%d 成功=%d 失败=%d", index, len(items), success, fail)
        time.sleep(0.03)
    save_progress(progress); logging.info("行情同步完成: 成功=%d 失败=%d", success, fail)


def _validate_index_frame(symbol, frame):
    required = {"eob", "open", "close", "high", "low", "volume"}
    if frame is None or len(frame) < 60 or not required.issubset(frame.columns):
        raise RuntimeError("%s指数行情不足或字段缺失" % symbol)
    clean = frame.copy().sort_values("eob")
    for column in ("open", "close", "high", "low"):
        clean[column] = pd.to_numeric(clean[column], errors="coerce")
    if clean[["open", "close", "high", "low"]].isna().any().any():
        raise RuntimeError("%s指数行情含非数值价格" % symbol)
    if (clean[["open", "close", "high", "low"]] <= 0).any().any():
        raise RuntimeError("%s指数行情含非正价格" % symbol)
    if ((clean["high"] < clean[["open", "close"]].max(axis=1)) |
            (clean["low"] > clean[["open", "close"]].min(axis=1)) |
            (clean["high"] < clean["low"])).any():
        raise RuntimeError("%s指数OHLC关系异常" % symbol)
    maximum_return = float(clean["close"].pct_change().abs().max())
    if maximum_return > 0.15:
        raise RuntimeError("%s指数单日涨跌异常 %.2f%%" % (symbol, maximum_return * 100))
    dates = pd.to_datetime(clean["eob"], errors="coerce")
    if dates.isna().any() or dates.dt.strftime("%Y-%m-%d").duplicated().any():
        raise RuntimeError("%s指数日期异常" % symbol)
    return clean


def sync_index_history(count=180):
    with sqlite3.connect(STOCKS_DB) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS index_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL NOT NULL, close REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
            volume INTEGER NOT NULL,
            UNIQUE(symbol,date))""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_index_history_date ON index_history(date)")
        for symbol in INDEX_SYMBOLS:
            frame = history_n(symbol=symbol, frequency="1d", count=count,
                              fields="symbol,eob,open,close,high,low,volume",
                              adjust=ADJUST_NONE, df=True)
            clean = _validate_index_frame(symbol, frame)
            records = [(symbol, pd.Timestamp(row.eob).strftime("%Y-%m-%d"),
                        float(row.open), float(row.close), float(row.high), float(row.low), int(row.volume))
                       for row in clean.itertuples()]
            con.executemany("""INSERT INTO index_history(symbol,date,open,close,high,low,volume)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET
                open=excluded.open,close=excluded.close,high=excluded.high,
                low=excluded.low,volume=excluded.volume""", records)
            logging.info("指数同步完成 %s: %d", symbol, len(records))
        con.commit()


def audit(items):
    codes={x.split('.')[-1] for x in items}
    with sqlite3.connect(STOCKS_DB) as con:
        master={x[0] for x in con.execute('select code from stocks')}; hist={x[0] for x in con.execute('select distinct code from history')}
        unknown=con.execute("select count(*) from stocks where upper(coalesce(sector,'')) in ('','UNKNOWN')").fetchone()[0]
        latest=con.execute("select max(date) from history where date glob '????-??-??'").fetchone()[0]
    with sqlite3.connect(FINANCIAL_DB) as con: fin={x[0] for x in con.execute('select distinct code from financial')}
    result={"master":len(codes&master),"history":len(codes&hist),"financial":len(codes&fin),"industry_unknown":unknown,"latest":latest}
    logging.info("审计: %s", result); return result


def main():
    log=setup_log(); set_token(user_environment("GM_TOKEN")); items=symbols(); progress=load_progress()
    logging.info("自动同步启动，成分=%d",len(items)); sync_industries(items); sync_history(items,progress)
    sync_index_history()
    result=audit(items); progress["audit"]=result; progress["status"]="complete"; save_progress(progress)
    print(json.dumps(result,ensure_ascii=False)); print("日志:",log)


if __name__ == "__main__": main()
