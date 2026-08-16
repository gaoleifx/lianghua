# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "config"))

from config_loader import load_config, validate_config
import gm_runtime
from gm_runtime import _state_filename
from strategy_core import (FactorEngine, FactorRow, LocalDatabase, PortfolioBuilder,
                           RiskEngine, StateStore, TradeIntent, UniverseSnapshot,
                           evaluate_tradability, position_available, position_cost,
                           can_increase_existing_position, positive_pyramid_target,
                           calculate_market_breadth, market_target_exposure,
                           trend_leader_signal,
                           protected_limit_price, round_lot)


class StrategyCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.cfg = load_config()

    def test_config_validation(self):
        cfg = json.loads(json.dumps(self.cfg)); cfg["factors"]["weights"]["relative_strength"] += .1
        with self.assertRaises(ValueError): validate_config(cfg)

    def test_return_first_bold_defaults(self):
        self.assertEqual(self.cfg["version"], "v6.8-candidate-quality-leader-breadth-confirmed-2026")
        self.assertEqual(self.cfg["portfolio"]["minimum_rebalance_interval_days"], 7)
        self.assertTrue(self.cfg["portfolio"]["dynamic_exposure_enabled"])
        self.assertEqual(self.cfg["portfolio"]["max_stocks"], 3)
        self.assertEqual(self.cfg["risk"]["all_time_peak_drawdown_lock"], 0.15)
        self.assertFalse(self.cfg["portfolio"]["enforce_maximum_holding_days"])
        self.assertFalse(self.cfg["deployment"]["live_new_entries_enabled"])

    def test_index_history_is_isolated_from_same_code_stock(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "stocks.db")
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(path)) as con:
                con.execute("""CREATE TABLE history (
                    id INTEGER PRIMARY KEY, code TEXT, date TEXT, open REAL, close REAL,
                    high REAL, low REAL, volume INTEGER, UNIQUE(code,date))""")
                con.execute("""CREATE TABLE index_history (
                    id INTEGER PRIMARY KEY, symbol TEXT, date TEXT, open REAL, close REAL,
                    high REAL, low REAL, volume INTEGER, UNIQUE(symbol,date))""")
                con.execute("INSERT INTO history(code,date,open,close,high,low,volume) VALUES(?,?,?,?,?,?,?)",
                            ("000905", "2026-08-14", 8.5, 8.65, 8.7, 8.4, 1000))
                con.execute("INSERT INTO index_history(symbol,date,open,close,high,low,volume) VALUES(?,?,?,?,?,?,?)",
                            ("SHSE.000905", "2026-08-14", 7900, 7990, 8020, 7880, 100000))
                con.commit()
            db = LocalDatabase(root=root, config=self.cfg)
            stock = db.bars(["SZSE.000905"], "2026-08-15", 1)
            index = db.bars(["SHSE.000905"], "2026-08-15", 1)
            self.assertEqual(float(stock.iloc[0]["close"]), 8.65)
            self.assertEqual(float(index.iloc[0]["close"]), 7990)
            self.assertEqual(index.iloc[0]["code"], "SHSE.000905")

    def test_conservative_profile_and_state_files(self):
        conservative = load_config()
        self.assertEqual(conservative["profile"], "conservative")
        self.assertFalse(conservative["portfolio"]["enforce_maximum_holding_days"])
        self.assertFalse(conservative["execution"]["positive_pyramid"]["enabled"])
        self.assertEqual(conservative["factors"]["minimum_market_breadth"], 0.30)
        self.assertTrue(conservative["portfolio"]["dynamic_exposure_enabled"])
        self.assertEqual(_state_filename("live", "conservative"), "live_conservative.json")
        self.assertEqual(_state_filename("backtest", "conservative", "run:01"),
                         "backtest_conservative_run01.json")

    def test_database_has_no_future_bars_or_financials(self):
        db = LocalDatabase(config=self.cfg)
        bars = db.bars(["SHSE.600000"], "2024-06-28", 20)
        if not bars.empty: self.assertLess(bars.date.max(), "2024-06-28")
        fin = db.financials(["SHSE.600000"], "2024-06-30")
        if not fin.empty:
            cutoff = (pd.Timestamp("2024-06-30") - pd.Timedelta(days=self.cfg["data"]["financial_lag_days"])).strftime("%Y-%m-%d")
            self.assertLessEqual(fin.report_date.max(), cutoff)

    def test_factor_missing_values_reweight_and_rank(self):
        engine = FactorEngine(self.cfg)
        raw = pd.DataFrame([{ "symbol": "SHSE.%06d" % i, "industry": "A",
          "relative_strength": i if i != 3 else np.nan, "trend_acceleration": i,
          "breakout": i, "volume_confirmation": i, "trend_efficiency": i,
          "downside_risk": i, "liquidity": i, "volatility": .2, "atr": 1,
          "confirmation_count": 5}
          for i in range(1, 11)])
        rows = engine.score(raw)
        self.assertEqual(len(rows), 10); self.assertEqual(rows[0].symbol, "SHSE.000010")

    def test_selection_requires_positive_momentum_and_trend(self):
        engine = FactorEngine(self.cfg)
        dates = pd.date_range("2025-01-01", periods=130)
        frames = []
        for code, prices in (("600001", np.linspace(10, 15, 130)),
                             ("600002", np.linspace(15, 10, 130))):
            volume = np.full(130, 10000000.0)
            if code == "600001":
                volume[-5:] = 15000000.0
            frames.append(pd.DataFrame({"code": code, "date": dates.astype(str), "open": prices,
                                        "close": prices, "high": prices * 1.01, "low": prices * .99,
                                        "volume": volume}))
        universe = UniverseSnapshot("2025-06-01", ["SHSE.600001", "SHSE.600002"])
        master = pd.DataFrame([{"code":"600001","sector":"A"},{"code":"600002","sector":"B"}])
        benchmark_prices = np.linspace(10, 11, 130)
        benchmark = pd.DataFrame({"date": dates.astype(str), "close": benchmark_prices})
        raw = engine.build_raw(universe, pd.concat(frames), pd.DataFrame(), master, benchmark)
        self.assertEqual(raw.symbol.tolist(), ["SHSE.600001"])
        self.assertEqual(universe.excluded["SHSE.600002"], "negative_momentum")
        self.assertGreater(raw.iloc[0].relative_strength, 0)
        self.assertGreaterEqual(int(raw.iloc[0].confirmation_count), 1)
        self.assertIn("confirm_rs20", raw.columns)
        self.assertIn("confirm_rs60", raw.columns)

    def test_market_breadth_counts_only_confirmed_uptrends(self):
        dates = pd.date_range("2025-01-01", periods=65).astype(str)
        up = np.linspace(10, 15, 65)
        down = np.linspace(15, 10, 65)
        bars = pd.concat([
            pd.DataFrame({"code": "600001", "date": dates, "close": up}),
            pd.DataFrame({"code": "600002", "date": dates, "close": down}),
            pd.DataFrame({"code": "600003", "date": dates[-30:], "close": up[-30:]}),
        ])
        breadth = calculate_market_breadth(bars)
        self.assertEqual(breadth["total"], 2)
        self.assertEqual(breadth["bullish"], 1)
        self.assertAlmostEqual(breadth["ratio"], .5)

    def test_dynamic_exposure_uses_breadth_tiers(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["portfolio"]["dynamic_exposure_enabled"] = True
        self.assertEqual(market_target_exposure(.19, cfg), 0.0)
        self.assertEqual(market_target_exposure(.20, cfg), .70)
        self.assertEqual(market_target_exposure(.35, cfg), .90)
        self.assertEqual(market_target_exposure(.50, cfg), 1.00)

    def test_potential_filters_weak_overheated_and_unconfirmed_volume(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["factors"]["maximum_5d_return"] = .18
        cfg["factors"]["overheat_20d_return"] = .35
        engine = FactorEngine(cfg)
        dates = pd.date_range("2025-01-01", periods=130)
        benchmark_prices = np.linspace(10, 12, 130)
        benchmark = pd.DataFrame({"date": dates.astype(str), "close": benchmark_prices})
        specs = {
            "600101": np.linspace(10, 15, 130),
            "600102": np.linspace(10, 11.5, 130),
            "600103": np.r_[np.linspace(10, 12, 125), np.linspace(12.5, 15, 5)],
            "600104": np.linspace(10, 14.5, 130),
        }
        frames = []
        for code, prices in specs.items():
            volume = np.full(130, 10000000.0)
            if code == "600101": volume[-5:] = 15000000.0
            frames.append(pd.DataFrame({"code": code, "date": dates.astype(str), "open": prices,
                                        "close": prices, "high": prices * 1.01, "low": prices * .99,
                                        "volume": volume}))
        universe = UniverseSnapshot("2025-07-01", ["SHSE." + code for code in specs])
        master = pd.DataFrame([{"code": code, "sector": "I" + code[-1]} for code in specs])
        raw = engine.build_raw(universe, pd.concat(frames), pd.DataFrame(), master, benchmark)
        weak = raw[raw.symbol == "SHSE.600102"].iloc[0]
        self.assertFalse(bool(weak.entry_relative_strength_ok))
        self.assertIn(universe.excluded["SHSE.600103"], ("short_term_overheated", "overheated"))
        no_volume = raw[raw.symbol == "SHSE.600104"].iloc[0]
        self.assertFalse(bool(no_volume.confirm_volume))

    def test_trend_leader_holds_consecutive_limit_ups(self):
        dates = pd.date_range("2025-01-01", periods=30)
        closes = np.r_[np.linspace(10, 12, 27), 13.2, 14.52, 14.70]
        frame = pd.DataFrame({"date": dates, "close": closes,
                              "volume": np.full(30, 1000000.0)})
        signal = trend_leader_signal(frame, 10, self.cfg)
        self.assertTrue(signal["leader"])
        self.assertTrue(signal["trend_intact"])
        self.assertFalse(signal["distribution"])
        self.assertEqual(signal["recent_limit_ups"], 2)

    def test_trend_leader_detects_high_volume_distribution(self):
        dates = pd.date_range("2025-01-01", periods=30)
        closes = np.r_[np.linspace(10, 14, 27), 15.4, 16.94, 12.5]
        volumes = np.r_[np.full(29, 1000000.0), 4000000.0]
        frame = pd.DataFrame({"date": dates, "close": closes, "volume": volumes})
        signal = trend_leader_signal(frame, 10, self.cfg)
        self.assertTrue(signal["leader"])
        self.assertTrue(signal["distribution"])

    def test_invalid_financial_values_never_change_potential_factors(self):
        engine = FactorEngine(self.cfg)
        dates = pd.date_range("2025-01-01", periods=130)
        prices = np.linspace(10, 15, 130)
        volume = np.full(130, 10000000.0); volume[-5:] = 15000000.0
        bars = pd.DataFrame({"code": "600201", "date": dates.astype(str), "open": prices,
                             "close": prices, "high": prices * 1.01, "low": prices * .99,
                             "volume": volume})
        benchmark = pd.DataFrame({"date": dates.astype(str), "close": np.linspace(10, 11, 130)})
        master = pd.DataFrame([{"code": "600201", "sector": "A"}])
        empty = engine.build_raw(UniverseSnapshot("2025-07-01", ["SHSE.600201"]),
                                 bars, pd.DataFrame(), master, benchmark)
        corrupt = pd.DataFrame([{"code": "600201", "pe": -999, "pb": 1e99,
                                 "roe": 5e10, "net_profit": 0}])
        supplied = engine.build_raw(UniverseSnapshot("2025-07-01", ["SHSE.600201"]),
                                    bars, corrupt, master, benchmark)
        for factor in FactorEngine.NAMES:
            if factor == "quality_growth":
                continue
            self.assertAlmostEqual(float(empty.iloc[0][factor]), float(supplied.iloc[0][factor]))
        self.assertTrue(pd.isna(empty.iloc[0]["quality_growth"]))
        self.assertTrue(pd.isna(supplied.iloc[0]["quality_growth"]))

    def test_valid_point_in_time_financials_create_quality_growth(self):
        engine = FactorEngine(self.cfg)
        dates = pd.date_range("2025-01-01", periods=130)
        frames = []
        financials = []
        master = []
        for index, code in enumerate(("600211", "600212", "600213")):
            prices = np.linspace(10, 15 + index * .2, 130)
            volume = np.full(130, 10000000.0); volume[-5:] = 15000000.0
            frames.append(pd.DataFrame({"code": code, "date": dates.astype(str), "open": prices,
                                        "close": prices, "high": prices * 1.01, "low": prices * .99,
                                        "volume": volume}))
            financials.append({"symbol": "SHSE." + code, "pub_date": "2025-05-01",
                               "rpt_date": "2025-03-31", "roe_weight_avg": 8 + index * 6,
                               "inc_oper_yoy": 5 + index * 10, "net_prof_pcom_yoy": 3 + index * 12,
                               "net_cf_oper": 100 + index * 100, "net_prof_pcom": 100})
            master.append({"code": code, "sector": "I" + str(index)})
        benchmark = pd.DataFrame({"date": dates.astype(str), "close": np.linspace(10, 11, 130)})
        universe = UniverseSnapshot("2025-07-01", ["SHSE." + x for x in ("600211", "600212", "600213")])
        raw = engine.build_raw(universe, pd.concat(frames), pd.DataFrame(financials),
                               pd.DataFrame(master), benchmark)
        self.assertTrue(raw["quality_growth"].notna().all())
        self.assertGreater(raw.loc[raw.symbol == "SHSE.600213", "quality_growth"].iloc[0],
                           raw.loc[raw.symbol == "SHSE.600211", "quality_growth"].iloc[0])

    def test_financial_snapshot_excludes_same_day_and_future_publications(self):
        frame = pd.DataFrame([
            {"symbol": "SHSE.600221", "pub_date": "2025-06-29", "rpt_date": "2025-03-31",
             "roe_weight_avg": 12, "inc_oper_yoy": 10, "net_prof_pcom_yoy": 9,
             "net_cf_oper": 110, "net_prof_pcom": 100},
            {"symbol": "SHSE.600222", "pub_date": "2025-07-01", "rpt_date": "2025-03-31",
             "roe_weight_avg": 20, "inc_oper_yoy": 20, "net_prof_pcom_yoy": 20,
             "net_cf_oper": 200, "net_prof_pcom": 100},
        ])
        gm_runtime._FINANCIAL_CACHE.clear()
        with patch.dict(gm_runtime.CONFIG["factors"], {"financial_factor_enabled": True}), \
             patch.object(gm_runtime, "stk_get_finance_prime_pt", return_value=frame):
            result, coverage = gm_runtime._gm_financial_snapshot(
                ["SHSE.600221", "SHSE.600222"], "2025-07-01")
        self.assertEqual(result.symbol.tolist(), ["SHSE.600221"])
        self.assertAlmostEqual(coverage, .5)

    def test_potential_confirmation_required_only_for_new_positions(self):
        rows = [FactorRow("SHSE.600301", "A", {}, {}, 1.0, .01, .2, 1, 0),
                FactorRow("SHSE.600302", "B", {}, {}, .9, .02, .2, 1, 5)]
        cfg = json.loads(json.dumps(self.cfg))
        cfg["factors"]["minimum_potential_confirmations"] = 1
        builder = PortfolioBuilder(cfg)
        self.assertEqual([x.symbol for x in builder.build(rows, holdings={"SHSE.600301"}, target_count=2)],
                         ["SHSE.600301", "SHSE.600302"])
        self.assertEqual([x.symbol for x in builder.build(rows, holdings=set(), target_count=2)],
                         ["SHSE.600302"])

    def test_negative_ic_relative_strength_is_not_a_mandatory_entry_gate(self):
        rows = [FactorRow("SHSE.600311", "A", {}, {}, 1.0, .01, .2, 1, 1,
                          {"entry_relative_strength_ok": False}),
                FactorRow("SHSE.600312", "B", {}, {}, .9, .02, .2, 1, 1,
                          {"entry_relative_strength_ok": False})]
        selected = PortfolioBuilder(self.cfg).build(rows, target_count=2)
        self.assertEqual([x.symbol for x in selected], ["SHSE.600311", "SHSE.600312"])

    def test_weak_benchmark_blocks_only_new_positions(self):
        rows = [FactorRow("SHSE.600301", "A", {}, {}, 1.0, .01, .2, 1, 5,
                          {"entry_relative_strength_ok": True}),
                FactorRow("SHSE.600302", "B", {}, {}, .9, .02, .2, 1, 5,
                          {"entry_relative_strength_ok": True})]
        builder = PortfolioBuilder(self.cfg)
        self.assertEqual(builder.build(rows, target_count=2, allow_new_positions=False), [])
        retained = builder.build(rows, holdings={"SHSE.600301"}, target_count=2,
                                 allow_new_positions=False)
        self.assertEqual([x.symbol for x in retained], ["SHSE.600301"])

    def test_portfolio_limits(self):
        rows = [FactorRow("SHSE.%06d" % i, "I%d" % (i % 3), {}, {}, 10-i, i/100, .1+i/100, 1, 5) for i in range(1, 11)]
        targets = PortfolioBuilder(self.cfg).build(rows)
        self.assertLessEqual(len(targets), self.cfg["portfolio"]["max_stocks"])
        self.assertTrue(all(x.weight <= self.cfg["portfolio"]["max_stock_weight"] + 1e-12 for x in targets))
        self.assertLessEqual(sum(x.weight for x in targets), 1-self.cfg["portfolio"]["cash_reserve"] + 1e-12)
        builder = PortfolioBuilder(self.cfg)
        failures = builder.validate_affordability(targets, {x.symbol: 30 for x in targets}, 4500)
        self.assertGreater(len(failures), 0)
        self.assertEqual(builder.affordable_count(rows, {x.symbol: 10 for x in rows}, 4500), 3)
        self.assertEqual(builder.affordable_count(rows, {x.symbol: 10 for x in rows}, 20000), 3)
        self.assertEqual(builder.affordable_count(rows, {x.symbol: 10 for x in rows}, 100000), 3)
        small = builder.build_affordable_small(rows, {x.symbol: 10 for x in rows}, 4500)
        self.assertEqual(len(small), 3)
        self.assertLessEqual(sum(x.weight for x in small), .90)
        self.assertGreater(sum(x.weight for x in small), .80)
        dynamic_cfg = json.loads(json.dumps(self.cfg))
        dynamic_cfg["portfolio"]["dynamic_exposure_enabled"] = True
        dynamic_cfg["portfolio"]["max_stocks"] = 3
        dynamic_builder = PortfolioBuilder(dynamic_cfg)
        three = dynamic_builder.build_affordable_small(
            rows, {x.symbol: 10 for x in rows}, 20000, target_exposure=.70)
        self.assertEqual(len(three), 3)
        self.assertEqual(builder.build_affordable_small(rows, {x.symbol: 10 for x in rows}, 4500, 0.0), [])
        reduced = builder.build_affordable_small(rows, {x.symbol: 10 for x in rows}, 4500, 0.5)
        self.assertTrue(all(abs(x.weight - expected_weight * .5) < 1e-12 for x in reduced))
        buffered_rows = [FactorRow("SHSE.600001", "A", {}, {}, 1, .20, .2, 1, 5),
                         FactorRow("SHSE.600002", "B", {}, {}, .9, .05, .2, 1, 5),
                         FactorRow("SHSE.600003", "C", {}, {}, .8, .06, .2, 1, 5)]
        buffered = builder.build_affordable_small(
            buffered_rows, {x.symbol: 10 for x in buffered_rows}, 20000, holdings={"SHSE.600001"})
        self.assertEqual(buffered[0].symbol, "SHSE.600001")
        fallback = dynamic_builder.build_affordable_small(
            buffered_rows, {"SHSE.600001": 10, "SHSE.600002": 10, "SHSE.600003": 100},
            20000, target_exposure=.70)
        self.assertEqual(len(fallback), 2)

    def test_risk_stops(self):
        risk = RiskEngine(self.cfg)
        self.assertEqual(risk.exit_reason(100, 91, 105, 1, 2), "hard_or_atr_stop")
        activation = self.cfg["risk"]["trailing_activation"]
        trail = self.cfg["risk"]["trailing_drawdown"]
        peak_price = 100 * (1 + activation + .05)
        trailing_price = peak_price * (1 - trail - .01)
        self.assertEqual(risk.exit_reason(100, trailing_price, peak_price, 1, 10), "trailing_profit")
        self.assertIsNone(risk.exit_reason(100, 99, 100, 0, 2))
        self.assertIsNone(risk.exit_reason(
            100, 101, 102, 0, self.cfg["portfolio"]["maximum_holding_days"] + 100))
        closes = [100.0] * (self.cfg["risk"]["market_ma_days"] + 1)
        force_drawdown = self.cfg["risk"]["portfolio_drawdown_force_exit"]
        state = risk.market_state(closes, 10000 * (1 - force_drawdown - .001), 10000)
        self.assertEqual(state.multiplier, 0.0)
        self.assertEqual(state.reason, "portfolio_drawdown_force_exit")

    def test_tiered_trailing_profit_uses_highest_activated_level(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["risk"]["trailing_levels"] = [
            {"activation": .08, "drawdown": .07},
            {"activation": .20, "drawdown": .10},
        ]
        risk = RiskEngine(cfg)
        self.assertIsNone(risk.exit_reason(100, 111, 121, 1, 30))
        self.assertEqual(risk.exit_reason(100, 108, 121, 1, 30), "trailing_profit")

    def test_reduced_risk_recovery_requires_cooldown_and_strong_market(self):
        risk = RiskEngine(self.cfg)
        ma_days = self.cfg["risk"]["market_ma_days"]
        strong = [100.0] * ma_days + [101.0]
        self.assertFalse(risk.can_recover(strong, "2026-08-10", "2026-08-13"))
        self.assertTrue(risk.can_recover(strong, "2026-08-01", "2026-08-13"))
        self.assertFalse(risk.can_recover([100.0] * ma_days + [99.0], "2026-08-01", "2026-08-13"))
        self.assertTrue(risk.can_recover_degraded("2026-07-01", "2026-08-01", 19800, 20000, True))
        self.assertFalse(risk.can_recover_degraded("2026-07-01", "2026-08-01", 19000, 20000, True))
        self.assertFalse(risk.can_recover_degraded("2026-07-01", "2026-08-01", 20000, 20000, False))

    def test_execution_rounding(self):
        self.assertEqual(round_lot(999), 900)
        self.assertEqual(protected_limit_price(10, "BUY", 20), 10.02)
        self.assertEqual(protected_limit_price(10, "SELL", 20), 9.98)

    def test_tradability_and_t_plus_one_fields(self):
        instrument = {"is_suspended": 0, "upper_limit": 11, "lower_limit": 9}
        quote = {"price": 10, "quotes": [{"bid_p": 9.99, "ask_p": 10.01}]}
        result = evaluate_tradability(instrument, quote)
        self.assertTrue(result.can_buy); self.assertTrue(result.can_sell)
        self.assertFalse(evaluate_tradability({**instrument, "is_suspended": 1}, quote).can_buy)
        self.assertFalse(evaluate_tradability(instrument, {"price": 11, "quotes": [{"bid_p": 11, "ask_p": 0}]}).can_buy)
        self.assertFalse(evaluate_tradability(instrument, {"price": 9, "quotes": [{"bid_p": 0, "ask_p": 9}]}).can_sell)
        pos = {"vwap": 8.95, "available": 1700, "volume_today": 0}
        self.assertEqual(position_cost(pos), 8.95); self.assertEqual(position_available(pos), 1700)

    def test_existing_position_never_averages_down(self):
        position = {"vwap": 10.0, "volume": 1000}
        self.assertFalse(can_increase_existing_position(position, 9.9))
        self.assertFalse(can_increase_existing_position(position, 10.04, .005))
        self.assertTrue(can_increase_existing_position(position, 10.05, .005))
        self.assertFalse(can_increase_existing_position({"volume": 1000}, 11.0))
        self.assertTrue(can_increase_existing_position(None, 9.0))

    def test_positive_pyramid_only_unlocks_higher_weight_after_profit(self):
        pyramid = {"enabled": True, "initial_weight": .45, "levels": [
            {"profit_threshold": .01, "target_weight": .4625},
            {"profit_threshold": .025, "target_weight": .475},
        ]}
        position = {"vwap": 10.0, "volume": 700, "market_value": 7000}
        self.assertEqual(positive_pyramid_target(.475, None, 10, pyramid), (.45, "pyramid_initial"))
        self.assertEqual(positive_pyramid_target(.475, position, 10.09, pyramid, 10, .45),
                         (.45, "pyramid_hold_initial"))
        self.assertEqual(positive_pyramid_target(.475, position, 10.10, pyramid, 10, .45),
                         (.4625, "pyramid_add_1"))
        self.assertEqual(positive_pyramid_target(.475, position, 10.25, pyramid, 10, .4625),
                         (.475, "pyramid_add_2"))
        # Profit retracement must not reverse a completed pyramid stage.
        self.assertEqual(positive_pyramid_target(.475, position, 10.20, pyramid, 10, .475)[0], .475)

    def test_state_deduplicates_and_recovers(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "state.json"); store = StateStore(path)
            intent = TradeIntent("SHSE.600000", .2, "BUY", "test", "id-1")
            self.assertTrue(store.intent_once(intent)); self.assertFalse(store.intent_once(intent))
            self.assertIn("id-1", StateStore(path).state["active_orders"])
            store.complete_intent("id-1")
            recovered = StateStore(path)
            self.assertNotIn("id-1", recovered.state["active_orders"])
            self.assertIn("id-1", recovered.state["completed_intents"])
            self.assertFalse(recovered.intent_once(intent))

    def test_rejected_intent_is_terminal_for_the_day(self):
        with tempfile.TemporaryDirectory() as root:
            store = StateStore(os.path.join(root, "state.json"))
            intent = TradeIntent("SHSE.600000", 0, "SELL", "risk", "20260812:SHSE.600000:0")
            self.assertTrue(store.intent_once(intent))
            store.complete_intent(intent.intent_id, successful=False, terminal=True, outcome="rejected")
            self.assertFalse(store.intent_once(intent))
            self.assertEqual(store.state["completed_intents"][intent.intent_id]["outcome"], "rejected")


if __name__ == "__main__": unittest.main()

