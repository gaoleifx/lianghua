# -*- coding: utf-8 -*-
"""回测入口。凭据与回测区间全部由环境变量注入。"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config")))
from gm.api import *
from gm_runtime import initialize, on_bar, on_order_status, rebalance
from runtime_env import user_environment

def init(context):
    context.account_id = user_environment("GOLDMINER_BACKTEST_ACCOUNT")
    initialize(context, "backtest")

if __name__ == "__main__":
    token = user_environment("GM_TOKEN")
    if not token: raise RuntimeError("请设置GM_TOKEN环境变量")
    run(strategy_id=user_environment("GM_BACKTEST_STRATEGY_ID", "08196542-1ba7-11f1-8c86-107c6107318f"),
        filename="main.py", mode=MODE_BACKTEST, token=token,
        backtest_start_time=user_environment("GM_BACKTEST_START", "2025-01-01 09:00:00"),
        backtest_end_time=user_environment("GM_BACKTEST_END", "2025-12-31 15:00:00"),
        backtest_adjust=ADJUST_PREV, backtest_initial_cash=float(user_environment("GM_BACKTEST_CASH", "1000000")),
        backtest_commission_ratio=0.0003, backtest_slippage_ratio=0.001)
