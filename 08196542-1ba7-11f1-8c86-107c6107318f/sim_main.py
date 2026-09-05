# -*- coding: utf-8 -*-
"""当前策略的掘金量化模拟盘入口。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config")))
from gm.api import *
from gm_runtime import initialize, on_bar, on_order_status, rebalance
from runtime_env import acquire_single_instance, user_environment


def init(context):
    context.account_id = user_environment("GOLDMINER_SIM_ACCOUNT")
    if not context.account_id:
        raise RuntimeError("请设置GOLDMINER_SIM_ACCOUNT模拟账户")
    # 模拟盘必须使用独立的 simulation 状态，不能污染实盘 live_conservative.json。
    initialize(context, "simulation")


if __name__ == "__main__":
    acquire_single_instance("simulation")
    token = user_environment("GM_TOKEN")
    if not token:
        raise RuntimeError("请设置GM_TOKEN环境变量")
    run(
        strategy_id=user_environment(
            "GM_SIM_STRATEGY_ID", "08196542-1ba7-11f1-8c86-107c6107318f"
        ),
        filename="sim_main.py",
        mode=MODE_LIVE,
        token=token,
    )
