# -*- coding: utf-8 -*-
import json, os, sys, tempfile, unittest
from datetime import datetime
from unittest.mock import patch

ROOT=os.path.abspath(os.path.join(os.path.dirname(__file__),".."));sys.path.insert(0,os.path.join(ROOT,"config"))

try:
    import gm_runtime
except ImportError:
    gm_runtime=None


class DummyStore:
    def __init__(self,last=None,risk=1.0,forced=None):
        self.state={"last_rebalance":last,"risk_multiplier":risk,"last_forced_exit_date":forced}


class DummyContext:
    def __init__(self,now,last=None,risk=1.0,forced=None):
        self.now=now;self.strategy_state=DummyStore(last,risk,forced)


@unittest.skipIf(gm_runtime is None,"gm SDK unavailable")
class RebalanceGateTests(unittest.TestCase):
    def test_backtest_does_not_require_live_freshness_file(self):
        context=DummyContext(datetime(2026,8,12,10,0)); context.strategy_role="backtest"
        self.assertTrue(gm_runtime._daily_data_healthy(context)[0])

    def test_retry_schedule_is_configured(self):
        self.assertGreaterEqual(len(gm_runtime.CONFIG["portfolio"]["rebalance_retry_times"]), 1)

    def test_same_day_retry_runs_after_failed_attempt(self):
        context=DummyContext(datetime(2026,8,12,10,30),None)
        with patch.object(gm_runtime,"rebalance") as call:
            gm_runtime.weekly_rebalance_gate(context);call.assert_called_once_with(context)

    def test_same_day_retry_stops_after_success(self):
        context=DummyContext(datetime(2026,8,12,10,30),"2026-08-12T10:00:00+08:00")
        with patch.object(gm_runtime,"rebalance") as call:
            gm_runtime.weekly_rebalance_gate(context);call.assert_not_called()

    def test_any_weekday_can_trigger(self):
        context=DummyContext(datetime(2026,8,12,10,0),None)  # Wednesday
        with patch.object(gm_runtime,"rebalance") as call:
            gm_runtime.weekly_rebalance_gate(context);call.assert_called_once_with(context)

    def test_same_iso_week_is_blocked(self):
        context=DummyContext(datetime(2026,8,14,10,0),"2026-08-12")
        with patch.object(gm_runtime,"rebalance") as call:
            gm_runtime.weekly_rebalance_gate(context);call.assert_not_called()

    def test_sixth_day_is_blocked(self):
        context=DummyContext(datetime(2026,8,18,10,0),"2026-08-12")
        with patch.object(gm_runtime,"rebalance") as call:
            gm_runtime.weekly_rebalance_gate(context);call.assert_not_called()

    def test_seventh_day_can_trigger(self):
        context=DummyContext(datetime(2026,8,19,10,0),"2026-08-12")
        with patch.object(gm_runtime,"rebalance") as call:
            gm_runtime.weekly_rebalance_gate(context);call.assert_called_once_with(context)

    def test_persistent_risk_stop_blocks_next_week(self):
        context=DummyContext(datetime(2026,8,17,10,0),"2026-08-12",risk=0.0)
        with patch.object(gm_runtime,"rebalance") as call:
            gm_runtime.weekly_rebalance_gate(context);call.assert_not_called()

    def test_reduced_market_risk_allows_half_weight_rebalance(self):
        context=DummyContext(datetime(2026,8,12,10,0),risk=.5)
        with patch.object(gm_runtime,"rebalance") as call:
            gm_runtime.weekly_rebalance_gate(context);call.assert_called_once_with(context)

    def test_same_day_forced_exit_blocks_reentry(self):
        context=DummyContext(datetime(2026,8,12,10,0),None,risk=1.0,forced="2026-08-12")
        with patch.object(gm_runtime,"rebalance") as call:
            gm_runtime.weekly_rebalance_gate(context);call.assert_not_called()

    def test_rebalance_exception_is_logged_and_does_not_escape_scheduler(self):
        context=DummyContext(datetime(2026,8,12,10,0),None)
        with patch.object(gm_runtime,"rebalance", side_effect=RuntimeError("account_unavailable")), \
             patch.object(gm_runtime,"_log") as log:
            gm_runtime.weekly_rebalance_gate(context)
        log.assert_any_call("rebalance_failed", error="account_unavailable",
                            strategy_time="2026-08-12T10:00:00",)

    def test_empty_account_fails_closed_with_explicit_reason(self):
        context=DummyContext(datetime(2026,8,12,10,0),None)
        context.account_id="missing-account"
        context.account=lambda account_id: None
        with self.assertRaisesRegex(RuntimeError, "account_unavailable:missing-account"):
            gm_runtime._account(context)


if __name__=="__main__":unittest.main()
