# -*- coding: utf-8 -*-
"""当前策略的掘金量化实盘入口；账户凭据由Windows用户环境变量注入。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config")))
from gm.api import *
from gm_runtime import initialize, on_bar, on_order_status, rebalance
from runtime_env import user_environment


def init(context):
    context.account_id = user_environment("GOLDMINER_LIVE_ACCOUNT")
    if not context.account_id:
        raise RuntimeError("请设置GOLDMINER_LIVE_ACCOUNT实盘账户")
    initialize(context, "live")


if __name__ == "__main__":
    token = user_environment("GM_TOKEN")
    if not token:
        raise RuntimeError("请设置GM_TOKEN环境变量")
    run(
        strategy_id=user_environment(
            "GM_LIVE_STRATEGY_ID", "08196542-1ba7-11f1-8c86-107c6107318f"
        ),
        filename="live_main.py",
        mode=MODE_LIVE,
        token=token,
    )
