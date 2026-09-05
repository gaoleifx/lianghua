# -*- coding: utf-8 -*-
"""五矿证券实盘入口；账户凭据由Windows用户环境变量注入。"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config")))
from gm.api import *
from gm_runtime import initialize, on_bar, on_order_status, rebalance
from runtime_env import acquire_single_instance, user_environment

def init(context):
    context.account_id = user_environment("GOLDMINER_LIVE_ACCOUNT")
    if not context.account_id:
        raise RuntimeError("请设置GOLDMINER_LIVE_ACCOUNT实盘账户")
    initialize(context, "live")

if __name__ == "__main__":
    acquire_single_instance("live")
    token = user_environment("GM_TOKEN")
    if not token: raise RuntimeError("请设置GM_TOKEN环境变量")
    run(strategy_id=user_environment("GM_LIVE_STRATEGY_ID", "a9c2a169-41ee-11f1-a1ab-107c6107318f"),
        filename="main.py", mode=MODE_LIVE, token=token)
