# -*- coding: utf-8 -*-
"""创建并回填独立指数行情表，阻止指数代码与深市股票代码串号。"""
import os
import shutil
import sqlite3
from datetime import datetime

import pandas as pd
from gm.api import ADJUST_NONE, history_n, set_token

from auto_sync_data import INDEX_SYMBOLS, STOCKS_DB, _validate_index_frame, user_environment


def main():
    set_token(user_environment("GM_TOKEN"))
    root = os.path.dirname(STOCKS_DB)
    backup_dir = os.path.join(root, "archive", "index_history_repair_%s" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(backup_dir, exist_ok=False)
    backup_path = os.path.join(backup_dir, "stocks.db")
    shutil.copy2(STOCKS_DB, backup_path)
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
            frame = history_n(symbol=symbol, frequency="1d", count=2000,
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
            print(symbol, len(records), records[0][1], records[-1][1])
        con.commit()
    print("backup:", backup_path)


if __name__ == "__main__":
    main()
