# -*- coding: utf-8 -*-
"""掘金运行适配层。共享核心不依赖gm，便于离线测试。"""
from __future__ import print_function

import json
import logging
import math
import os
import shutil
from datetime import datetime

import pandas as pd

from config_loader import data_root, load_config, strategy_profile
from strategy_core import (FactorEngine, LocalDatabase, PortfolioBuilder, RiskEngine,
                           StateStore, TradeIntent, UniverseSnapshot, protected_limit_price,
                           evaluate_tradability, position_available, position_cost,
                           can_increase_existing_position, positive_pyramid_target,
                           calculate_market_breadth, market_target_exposure, trend_leader_signal,
                           round_lot, to_code, to_gm_symbol)
from market_intelligence import live_main_fund_signal

try:
    from gm.api import *
except ImportError:  # 离线单元测试环境
    pass

LOG = logging.getLogger("goldminer_strategy")
CONFIG = load_config()
DB = LocalDatabase(config=CONFIG)
FACTORS = FactorEngine(CONFIG)
PORTFOLIO = PortfolioBuilder(CONFIG)
RISK = RiskEngine(CONFIG)
_FINANCIAL_CACHE = {}
_TREND_SIGNAL_CACHE = {}


def _cached_trend_signal(symbol, as_of, entry_price):
    key = (symbol, str(as_of), round(float(entry_price or 0), 4))
    if key not in _TREND_SIGNAL_CACHE:
        history_frame = DB.bars([symbol], str(as_of), 40)
        _TREND_SIGNAL_CACHE[key] = trend_leader_signal(history_frame, entry_price, CONFIG)
        if len(_TREND_SIGNAL_CACHE) > 1000:
            for old_key in list(_TREND_SIGNAL_CACHE)[:-500]:
                _TREND_SIGNAL_CACHE.pop(old_key, None)
    return _TREND_SIGNAL_CACHE[key]


def _state_filename(role, profile, suffix=""):
    safe_suffix = "".join(ch for ch in suffix if ch.isalnum() or ch in ("-", "_"))
    parts = [role, profile] + ([safe_suffix] if safe_suffix else [])
    return "_".join(parts) + ".json"


def _state_store(role):
    directory = os.environ.get(CONFIG["state"].get("directory_env", ""), os.path.join(data_root(CONFIG), "strategy_state"))
    profile = CONFIG.get("profile", strategy_profile())
    suffix = os.environ.get("GOLDMINER_BACKTEST_STATE_SUFFIX", "") if role == "backtest" else ""
    path = os.path.join(directory, _state_filename(role, profile, suffix))
    legacy = os.path.join(directory, role + ".json")
    if profile == "conservative" and role != "backtest" and not os.path.exists(path) and os.path.exists(legacy):
        os.makedirs(directory, exist_ok=True); shutil.copy2(legacy, path)
    return StateStore(path)


def _log(event, **payload):
    payload.update(event=event, time=datetime.now().isoformat(timespec="seconds"))
    LOG.info(json.dumps(payload, ensure_ascii=False, default=str))


def _daily_data_healthy(context):
    if context.strategy_role == "backtest":
        return True, "backtest_point_in_time"
    path = os.path.join(data_root(CONFIG), "progress", "data_freshness.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            status = json.load(handle)
        progress_path = os.path.join(data_root(CONFIG), "progress", "auto_sync_index800.json")
        if status.get("status") != "healthy":
            try:
                with open(progress_path, "r", encoding="utf-8") as progress_handle:
                    progress = json.load(progress_handle)
                audit = progress.get("audit", {})
                if (progress.get("status") == "complete" and
                        int(audit.get("history", 0)) >= 760 and
                        int(audit.get("master", 0)) >= 760 and
                        status.get("abnormal_index_return_rows", 0) == 0 and
                        status.get("index_data_fresh") is not False):
                    return True, "healthy:sync_completion_fallback:%s" % audit.get("latest")
            except (OSError, TypeError, ValueError):
                pass
            return False, "scheduled_update_" + str(status.get("status", "unknown"))
        coverage = status.get("complete_coverage", status.get("latest_coverage", 0))
        if int(coverage) < 760:
            return False, "latest_coverage_insufficient"
        return True, "healthy:%s" % status.get("latest_trade_date")
    except (OSError, ValueError) as exc:
        return False, "freshness_status_unavailable:%s" % exc


def _constituents(as_of):
    """兼容不同版本gm SDK；中证800失败时合并沪深300与中证500。"""
    symbols = []
    for index in [CONFIG["universe"]["index_symbol"]] + CONFIG["universe"].get("fallback_index_symbols", []):
        try:
            result = stk_get_index_constituents(index=index, trade_date=as_of)
            if isinstance(result, pd.DataFrame):
                column = "symbol" if "symbol" in result else "constituent_symbol"
                batch = result[column].dropna().astype(str).tolist()
            else:
                batch = [x.get("symbol") or x.get("constituent_symbol") for x in (result or [])]
            symbols.extend(x for x in batch if x)
            if len(set(symbols)) >= CONFIG["universe"]["minimum_constituents"]:
                break
        except Exception as exc:
            _log("index_constituents_failed", index=index, error=str(exc))
    return list(dict.fromkeys(symbols))


def _gm_financial_snapshot(symbols, as_of):
    """Validated GM point-in-time financial snapshot; never reads the corrupted local fields."""
    if not CONFIG["factors"].get("financial_factor_enabled", False):
        return pd.DataFrame(), 1.0
    cache_key = (as_of, tuple(sorted(symbols)))
    if cache_key in _FINANCIAL_CACHE:
        return _FINANCIAL_CACHE[cache_key]
    fields = "roe_weight_avg,inc_oper_yoy,net_prof_pcom_yoy,net_cf_oper,net_prof_pcom"
    frames = []
    try:
        for start in range(0, len(symbols), 200):
            batch = stk_get_finance_prime_pt(
                symbols=symbols[start:start + 200], fields=fields, data_type=101,
                date=as_of, df=True)
            if batch is not None and len(batch):
                frames.append(batch)
    except Exception as exc:
        raise RuntimeError("gm_point_in_time_financial_failed: %s" % exc)
    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    required = ["symbol", "pub_date", "rpt_date", "roe_weight_avg", "inc_oper_yoy",
                "net_prof_pcom_yoy", "net_cf_oper", "net_prof_pcom"]
    if frame.empty or any(column not in frame.columns for column in required):
        raise RuntimeError("gm_point_in_time_financial_schema_invalid")
    frame["pub_date"] = pd.to_datetime(frame["pub_date"], errors="coerce")
    frame["rpt_date"] = pd.to_datetime(frame["rpt_date"], errors="coerce")
    cutoff = pd.Timestamp(as_of)
    frame = frame[(frame["pub_date"] < cutoff) & (frame["rpt_date"] < cutoff)].copy()
    numeric = ["roe_weight_avg", "inc_oper_yoy", "net_prof_pcom_yoy", "net_cf_oper", "net_prof_pcom"]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame.loc[~frame["roe_weight_avg"].between(-100, 100), "roe_weight_avg"] = float("nan")
    for column in ("inc_oper_yoy", "net_prof_pcom_yoy"):
        frame.loc[~frame[column].between(-100, 1000), column] = float("nan")
    frame = frame.sort_values(["symbol", "pub_date", "rpt_date"]).drop_duplicates("symbol", keep="last")
    usable = frame[numeric].notna().sum(axis=1) >= 3
    coverage = float(frame.loc[usable, "symbol"].nunique()) / max(len(set(symbols)), 1)
    result = (frame, coverage)
    _FINANCIAL_CACHE[cache_key] = result
    if len(_FINANCIAL_CACHE) > 8:
        _FINANCIAL_CACHE.pop(next(iter(_FINANCIAL_CACHE)))
    return result


def _universe(as_of):
    symbols = _constituents(as_of)
    snapshot = UniverseSnapshot(as_of, symbols, degraded=False)
    if len(symbols) < CONFIG["universe"]["minimum_constituents"]:
        snapshot.degraded = True
        snapshot.excluded["*"] = "index_constituents_unavailable"
        return snapshot
    master = DB.stock_master(symbols)
    try:
        instruments = get_instruments(symbols=",".join(symbols), skip_suspended=False, skip_st=False, df=True)
        instrument_map = instruments.set_index("symbol") if instruments is not None and len(instruments) else pd.DataFrame()
    except Exception as exc:
        snapshot.degraded = True; snapshot.excluded["*"] = "instrument_status_unavailable:" + str(exc)
        return snapshot
    known = set(master["code"].astype(str)) if not master.empty else set()
    usable = []
    for symbol in symbols:
        code = to_code(symbol)
        if code not in known:
            snapshot.excluded[symbol] = "not_in_local_database"; continue
        row = master[master["code"] == code].iloc[0]
        name = str(row.get("name", "")).upper()
        if "ST" in name or "退" in name:
            snapshot.excluded[symbol] = "special_treatment"; continue
        if instrument_map.empty or symbol not in instrument_map.index:
            snapshot.excluded[symbol] = "instrument_missing"; continue
        inst = instrument_map.loc[symbol]
        if int(inst.get("is_suspended", 0)):
            snapshot.excluded[symbol] = "suspended"; continue
        listed = pd.Timestamp(inst.get("listed_date")).tz_localize(None)
        if (pd.Timestamp(as_of) - listed).days < CONFIG["universe"]["minimum_listing_days"]:
            snapshot.excluded[symbol] = "recently_listed"; continue
        usable.append(symbol)
    snapshot.symbols = usable
    coverage = len(usable) / max(len(symbols), 1)
    if coverage < CONFIG["universe"]["minimum_coverage_ratio"]:
        snapshot.degraded = True
        snapshot.excluded["*"] = "local_database_coverage:%.3f" % coverage
    return snapshot


def initialize(context, role):
    context.strategy_role = role
    context.strategy_state = _state_store(role)
    if role == "backtest":
        # 每次回测必须从干净状态开始，禁止继承其他回测区间的峰值、持仓和订单。
        context.strategy_state.state = {"positions": {}, "active_orders": {}, "completed_intents": {},
                                        "last_rebalance": None, "peak_asset": 0.0,
                                        "risk_multiplier": 1.0, "risk_reason": "normal",
                                        "risk_updated": None, "last_forced_exit_date": None}
        context.strategy_state.save()
    context.strategy_config = CONFIG
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # 五矿掘金3 SDK不接受date_rule="1w"，改为日调度+周门控。
    schedule(schedule_func=weekly_rebalance_gate, date_rule="1d", time_rule=CONFIG["portfolio"]["rebalance_time"])
    # 首次调仓未完成（last_rebalance未写入）时，在当日后续时点自动重试。
    # weekly_rebalance_gate自身负责周频、风控及强制退出门控，成功后不会重复下单。
    for time_rule in CONFIG["portfolio"].get("rebalance_retry_times", []):
        schedule(schedule_func=weekly_rebalance_gate, date_rule="1d", time_rule=time_rule)
    for time_rule in ("09:35:00", "10:30:00", "11:25:00", "13:30:00", "14:30:00", "14:55:00"):
        schedule(schedule_func=daily_risk_check, date_rule="1d", time_rule=time_rule)
    _log("initialized", role=role, profile=CONFIG.get("profile"),
         database_root=data_root(CONFIG), config_version=CONFIG["version"])


def weekly_rebalance_gate(context):
    now = getattr(context, "now", datetime.now())
    iso_week = "%04d-W%02d" % (now.isocalendar()[0], now.isocalendar()[1])
    last = context.strategy_state.state.get("last_rebalance")
    if last:
        last_ts = pd.Timestamp(last)
        elapsed_days = (pd.Timestamp(now).tz_localize(None) - last_ts.tz_localize(None)).days
        if elapsed_days < int(CONFIG["portfolio"].get("minimum_rebalance_interval_days", 7)):
            return
        last_week = "%04d-W%02d" % (last_ts.isocalendar()[0], last_ts.isocalendar()[1])
        if last_week == iso_week:
            return
    risk_multiplier = float(context.strategy_state.state.get("risk_multiplier", 1.0))
    if risk_multiplier <= 0.0:
        _log("rebalance_aborted", reason="market_risk_not_normal", multiplier=risk_multiplier)
        return
    if context.strategy_state.state.get("last_forced_exit_date") == now.strftime("%Y-%m-%d"):
        _log("rebalance_aborted", reason="same_day_after_forced_exit")
        return
    # 任意交易日均可尝试；只有实际完成调仓才会写last_rebalance并消耗本周额度。
    rebalance(context)


def _positions(account_id):
    return get_position(account_id=account_id) or []


def _account(context):
    account = context.account(account_id=context.account_id)
    cash = account.cash
    return account, float(cash.get("nav", cash.get("asset", 0))), float(cash.get("available", 0))


def _submit_target(context, symbol, target, reason, last_price):
    now = getattr(context, "now", datetime.now())
    day = now.strftime("%Y%m%d")
    existing = next((p for p in _positions(context.account_id) if p.get("symbol") == symbol), None)
    try:
        _, nav, available_cash = _account(context)
    except Exception:
        nav, available_cash = 0, 0
    current_weight = float(existing.get("market_value", 0)) / nav if existing and nav > 0 else 0
    tolerance = float(CONFIG["execution"].get("target_weight_tolerance", 0.01))
    if abs(float(target) - current_weight) <= tolerance:
        _log("order_skipped", symbol=symbol, target=target, current_weight=current_weight,
             reason="target_unchanged")
        return
    side_name = "BUY" if target > current_weight + 0.001 else "SELL"
    intent = TradeIntent(symbol, target, side_name, reason,
                         "%s:%s:%s" % (day, symbol, round(target, 4)))
    if not context.strategy_state.intent_once(intent):
        _log("duplicate_intent_suppressed", symbol=symbol, intent_id=intent.intent_id); return
    side = intent.side
    try:
        if context.strategy_role == "backtest":
            # 禁止历史回测读取当前盘口；由掘金撮合引擎处理历史停牌、涨跌停和成交。
            price = 0
        else:
            instrument = get_instruments(symbols=symbol, skip_suspended=False, skip_st=False, df=True)
            quotes = current(symbols=symbol, fields="")
            inst = instrument.iloc[-1].to_dict() if instrument is not None and len(instrument) else {}
            quote = quotes[-1] if isinstance(quotes, list) and quotes else (quotes.iloc[-1].to_dict() if isinstance(quotes, pd.DataFrame) and len(quotes) else {})
            trade = evaluate_tradability(inst, quote, CONFIG["execution"]["price_protection_bps"])
            if (side == "BUY" and not trade.can_buy) or (side == "SELL" and not trade.can_sell):
                context.strategy_state.complete_intent(intent.intent_id, successful=False)
                _log("order_blocked", symbol=symbol, side=side, reason=trade.reason); return
            price = trade.buy_price if side == "BUY" else trade.sell_price
        if side == "SELL":
            if existing is None or position_available(existing) <= 0:
                context.strategy_state.complete_intent(intent.intent_id, successful=False)
                _log("order_blocked", symbol=symbol, side=side, reason="t_plus_one_no_available"); return
        sizing_price = float(last_price) if context.strategy_role == "backtest" else float(price)
        current_volume = int(float(existing.get("volume", 0))) if existing else 0
        lot = int(CONFIG["execution"]["lot_size"])
        desired_volume = round_lot(nav * float(target) / sizing_price, lot) if target > 0 and sizing_price > 0 else 0
        delta = desired_volume - current_volume
        if delta > 0:
            minimum_profit = float(CONFIG["execution"].get("existing_position_add_min_profit", 0.005))
            if existing and not can_increase_existing_position(existing, sizing_price, minimum_profit):
                context.strategy_state.complete_intent(intent.intent_id, successful=False, terminal=True,
                                                       outcome="existing_position_not_profitable")
                _log("order_blocked", symbol=symbol, side="BUY", reason="no_averaging_down",
                     current_price=sizing_price, position_cost=position_cost(existing),
                     minimum_profit=minimum_profit, desired_volume=desired_volume,
                     current_volume=current_volume)
                return
            affordable = round_lot(available_cash / sizing_price, lot)
            volume = min(delta, affordable)
            if volume < lot:
                context.strategy_state.complete_intent(intent.intent_id, successful=False, terminal=True,
                                                       outcome="insufficient_cash_or_lot")
                _log("order_blocked", symbol=symbol, side="BUY", reason="insufficient_cash_or_lot",
                     desired_volume=desired_volume, current_volume=current_volume, available_cash=available_cash)
                return
            order_volume(symbol=symbol, volume=volume, side=OrderSide_Buy,
                         order_type=OrderType_Market if context.strategy_role == "backtest" else OrderType_Limit,
                         position_effect=PositionEffect_Open,
                         price=price, account=context.account_id)
        elif delta < 0:
            available_volume = position_available(existing) if existing else 0
            # A full exit may include an odd-lot remainder; partial reductions must be whole lots.
            volume = min(current_volume, available_volume) if desired_volume == 0 else min(round_lot(-delta, lot), available_volume)
            if volume <= 0:
                context.strategy_state.complete_intent(intent.intent_id, successful=False, terminal=True,
                                                       outcome="t_plus_one_no_available")
                _log("order_blocked", symbol=symbol, side="SELL", reason="t_plus_one_no_available")
                return
            order_volume(symbol=symbol, volume=volume, side=OrderSide_Sell,
                         order_type=OrderType_Market if context.strategy_role == "backtest" else OrderType_Limit,
                         position_effect=PositionEffect_Close,
                         price=price, account=context.account_id)
        else:
            context.strategy_state.complete_intent(intent.intent_id, successful=True, outcome="lot_unchanged")
            _log("order_skipped", symbol=symbol, target=target, reason="lot_unchanged")
            return
        _log("order_submitted", symbol=symbol, target=target, price=price, volume=volume,
             reason=reason, intent_id=intent.intent_id)
    except Exception as exc:
        context.strategy_state.complete_intent(intent.intent_id, successful=False)
        _log("order_submit_failed", symbol=symbol, error=str(exc), reason=reason)


def rebalance(context):
    as_of = getattr(context, "now", datetime.now()).strftime("%Y-%m-%d")
    healthy, freshness_reason = _daily_data_healthy(context)
    if not healthy:
        _log("rebalance_aborted", reason="daily_data_not_healthy", detail=freshness_reason)
        return
    snapshot = _universe(as_of)
    if snapshot.degraded:
        _log("rebalance_aborted", reason="universe_degraded", excluded=snapshot.excluded); return
    bars = DB.bars(snapshot.symbols, as_of, 130)
    master = DB.stock_master(snapshot.symbols)
    benchmark_bars = pd.DataFrame()
    benchmark_source = None
    for candidate in [CONFIG["universe"]["index_symbol"]] + CONFIG["universe"].get("fallback_index_symbols", []):
        local_benchmark = DB.bars([candidate], as_of, 130)
        last_benchmark_date = (pd.Timestamp(local_benchmark["date"].max())
                               if not local_benchmark.empty else pd.NaT)
        staleness = ((pd.Timestamp(as_of) - last_benchmark_date).days
                     if not pd.isna(last_benchmark_date) else math.inf)
        if len(local_benchmark) >= 61 and staleness <= int(CONFIG["data"]["max_staleness_days"]):
            benchmark_bars = local_benchmark
            benchmark_source = candidate
            break
    if benchmark_bars.empty:
        _log("rebalance_aborted", reason="benchmark_signal_unavailable")
        return
    benchmark_closes = benchmark_bars.sort_values("date")["close"].astype(float)
    benchmark_ma20 = float(benchmark_closes.tail(20).mean())
    benchmark_ma60 = float(benchmark_closes.tail(60).mean())
    benchmark_ma20_series = benchmark_closes.rolling(20).mean()
    benchmark_return20 = float(benchmark_closes.iloc[-1] / benchmark_closes.iloc[-21] - 1)
    benchmark_ma20_slope = float(benchmark_ma20 / benchmark_ma20_series.iloc[-6] - 1)
    breadth = calculate_market_breadth(bars)
    minimum_breadth = float(CONFIG["factors"].get("minimum_market_breadth", 0.0))
    target_exposure = market_target_exposure(breadth["ratio"], CONFIG)
    market_entry_ok = (not CONFIG["factors"].get("require_benchmark_bullish_for_new_entries", False) or
                       (float(benchmark_closes.iloc[-1]) > benchmark_ma20 > benchmark_ma60 and
                        benchmark_return20 > 0 and benchmark_ma20_slope > 0 and
                        breadth["ratio"] >= minimum_breadth))
    positions = _positions(context.account_id)
    holding = {p["symbol"]: p for p in positions}
    if context.strategy_role == "live" and not CONFIG.get("deployment", {}).get(
            "live_new_entries_enabled", False):
        market_entry_ok = False
        _log("live_entry_gate", enabled=False,
             validation_status=CONFIG.get("deployment", {}).get("validation_status", "unknown"),
             existing_positions=len(holding))
    if not market_entry_ok and not holding:
        reason = ("live_validation_not_approved" if context.strategy_role == "live" else
                  "benchmark_entry_trend_weak")
        _log("rebalance_aborted", reason=reason,
             benchmark_source=benchmark_source, benchmark_return20=benchmark_return20,
             benchmark_ma20_slope=benchmark_ma20_slope, market_breadth=breadth,
             minimum_market_breadth=minimum_breadth)
        return
    try:
        financials, financial_coverage = _gm_financial_snapshot(snapshot.symbols, as_of)
    except Exception as exc:
        _log("rebalance_aborted", reason="financial_snapshot_unavailable", error=str(exc)); return
    if financial_coverage < float(CONFIG["factors"].get("minimum_financial_coverage", 0.0)):
        _log("rebalance_aborted", reason="financial_coverage_insufficient",
             coverage=financial_coverage,
             minimum=CONFIG["factors"].get("minimum_financial_coverage")); return
    raw_factors = FACTORS.build_raw(snapshot, bars, financials, master, benchmark_bars,
                                    market_breadth=breadth["ratio"])
    rows = FACTORS.score(raw_factors)
    _log("factor_snapshot", as_of=as_of, signal_cutoff="strictly_before_rebalance_date",
         benchmark_source=benchmark_source,
         financials_enabled=CONFIG["factors"].get("financial_factor_enabled", False),
         financial_source="gm_point_in_time_prime", financial_coverage=financial_coverage,
         benchmark_entry_ok=market_entry_ok, benchmark_return20=benchmark_return20,
         benchmark_ma20_slope=benchmark_ma20_slope, market_breadth=breadth,
         minimum_market_breadth=minimum_breadth,
         target_exposure=target_exposure,
         qualified=len(rows), excluded_count=len(snapshot.excluded),
         candidates=[{
             "symbol": row.symbol, "industry": row.industry, "percentile": row.percentile,
             "composite": row.composite, "confirmations": row.confirmations,
             "confirmation_names": row.details.get("confirmation_names", []),
             "details": row.details, "raw": row.raw, "scores": row.scores,
         } for row in rows[:30]])
    cooldown_days = int(CONFIG["portfolio"].get("exit_cooldown_days", 0))
    exit_dates = context.strategy_state.state.get("exit_dates", {})
    if cooldown_days:
        rows = [row for row in rows if row.symbol not in exit_dates or
                (pd.Timestamp(as_of) - pd.Timestamp(exit_dates[row.symbol])).days > cooldown_days]
    if len(rows) < CONFIG["portfolio"]["max_stocks"]:
        _log("rebalance_aborted", reason="factor_coverage", available=len(rows)); return
    risk_multiplier = float(context.strategy_state.state.get("risk_multiplier", 1.0))
    if risk_multiplier <= 0:
        _log("rebalance_aborted", reason="persistent_risk_stop"); return
    # 先按候选最新价与账户净值寻找可按整手实现的持仓数量。
    latest = bars.sort_values("date").groupby("code").tail(1).set_index("code")
    all_prices = {row.symbol: float(latest.loc[to_code(row.symbol), "close"]) for row in rows if to_code(row.symbol) in latest.index}
    _, nav, _ = _account(context)
    if market_entry_ok:
        target_count = PORTFOLIO.affordable_count(
            rows, all_prices, nav, target_exposure, holding.keys(), market_entry_ok)
        if target_count < CONFIG["portfolio"]["minimum_stocks"]:
            _log("rebalance_aborted", reason="minimum_portfolio_unaffordable", nav=nav); return
    else:
        target_count = max(CONFIG["portfolio"]["minimum_stocks"], len(holding))
    if nav <= CONFIG["portfolio"].get("small_account_threshold", 0):
        targets = PORTFOLIO.build_affordable_small(
            rows, all_prices, nav, risk_multiplier, holding.keys(), market_entry_ok,
            target_exposure)
    else:
        targets = PORTFOLIO.build(rows, holding.keys(), risk_multiplier,
                                  target_count=target_count, allow_new_positions=market_entry_ok,
                                  target_exposure=target_exposure)
    target_map = {x.symbol: x.weight for x in targets}
    # 只有到达最短持有期或跌出退出缓冲区才因排名卖出；风控卖出由盘中路径负责。
    exits = []
    for symbol, pos in holding.items():
        state = context.strategy_state.state["positions"].get(symbol, {})
        held = int(state.get("holding_days", 0))
        if symbol not in target_map and held < CONFIG["portfolio"]["minimum_holding_days"]:
            continue
        if symbol not in target_map:
            history_frame = bars[bars["code"].astype(str).map(to_code) == to_code(symbol)]
            leader = trend_leader_signal(history_frame, position_cost(pos), CONFIG)
            if leader["leader"]:
                state["leader_mode"] = True
                state["leader_detected_date"] = as_of
            if (state.get("leader_mode") and leader["trend_intact"] and
                    not leader["distribution"] and not state.get("main_fund_withdrawal", False)):
                _log("leader_exit_suppressed", symbol=symbol, attempted_reason="rank_exit",
                     leader_signal=leader, fund_signal=state.get("main_fund_signal"))
                continue
            last = float(pos.get("price") or pos.get("last_price") or position_cost(pos))
            exits.append((symbol, last))
    prices = {item.symbol: float(latest.loc[to_code(item.symbol), "close"]) for item in targets}
    unaffordable = PORTFOLIO.validate_affordability(targets, prices, nav)
    if unaffordable:
        _log("rebalance_aborted", reason="target_lot_unaffordable", nav=nav, failures=unaffordable)
        return
    # Submit exits first. New buys use the subsequently refreshed available cash on the next
    # trading-day gate when same-day proceeds are unavailable or fills are asynchronous.
    for symbol, last in exits:
        _submit_target(context, symbol, 0, "rank_exit", last)
    for item in targets:
        last = float(latest.loc[to_code(item.symbol), "close"])
        factor = next((row for row in rows if row.symbol == item.symbol), None)
        state = context.strategy_state.state["positions"].setdefault(item.symbol, {})
        if factor is not None:
            state["atr"] = factor.atr
        existing = holding.get(item.symbol)
        current_weight = float(existing.get("market_value", 0)) / nav if existing and nav > 0 else 0
        pyramid_target, pyramid_reason = positive_pyramid_target(
            item.weight, existing, last, CONFIG["execution"].get("positive_pyramid"),
            state.get("pyramid_base_price"), current_weight)
        _submit_target(context, item.symbol, pyramid_target,
                       pyramid_reason if pyramid_reason != "pyramid_disabled" else item.reason, last)
    context.strategy_state.state["last_rebalance"] = as_of
    context.strategy_state.state["market_target_exposure"] = target_exposure
    context.strategy_state.save()
    _log("rebalance_complete", candidates=len(rows), targets=[x.__dict__ for x in targets], excluded=snapshot.excluded)


def daily_risk_check(context):
    maintain_orders(context)
    positions = _positions(context.account_id)
    try:
        required = CONFIG["risk"]["market_ma_days"] + 5
        closes = []
        benchmark_source = CONFIG["universe"]["index_symbol"]
        if context.strategy_role == "backtest":
            as_of = getattr(context, "now", datetime.now()).strftime("%Y-%m-%d")
            candidates = [CONFIG["universe"]["index_symbol"]] + CONFIG["universe"].get("fallback_index_symbols", [])
            for candidate in candidates:
                local = DB.bars([candidate], as_of, required)
                if len(local) >= CONFIG["risk"]["market_ma_days"]:
                    closes = local.sort_values("date")["close"].astype(float).tolist()
                    benchmark_source = candidate
                    break
        if not closes:
            benchmark = history_n(symbol=CONFIG["universe"]["index_symbol"], frequency="1d",
                                  count=required, fields="close", df=True)
            closes = benchmark["close"].astype(float).tolist() if benchmark is not None else []
        account, asset, _ = _account(context)
        previous_multiplier = float(context.strategy_state.state.get("risk_multiplier", 1.0))
        initial_asset = float(context.strategy_state.state.get("initial_asset", 0))
        if initial_asset <= 0:
            initial_asset = asset
            context.strategy_state.state["initial_asset"] = asset
        absolute_drawdown = 1 - asset / initial_asset if initial_asset > 0 else 0
        all_time_peak = max(float(context.strategy_state.state.get("all_time_peak_asset", 0)), asset)
        context.strategy_state.state["all_time_peak_asset"] = all_time_peak
        all_time_drawdown = 1 - asset / all_time_peak if all_time_peak > 0 else 0
        hard_limit = float(CONFIG["risk"].get("portfolio_drawdown_force_exit", 0.12))
        high_water_limit = float(CONFIG["risk"].get("all_time_peak_drawdown_lock", 0.15))
        permanent_lock = bool(CONFIG["risk"].get("permanent_capital_lock", False))
        if permanent_lock and (context.strategy_state.state.get("risk_locked") or
                               absolute_drawdown >= hard_limit or all_time_drawdown >= high_water_limit):
            context.strategy_state.state["risk_locked"] = True
            risk_state = type("LockedRisk", (), {
                "multiplier": 0.0,
                "reason": "capital_loss_lock" if absolute_drawdown >= hard_limit else "all_time_drawdown_lock",
                "allow_new_positions": False
            })()
            peak = max(float(context.strategy_state.state.get("peak_asset", 0)), asset)
        else:
            # v6 uses a recoverable 1.0 -> 0.5 -> 0 risk state machine. Migrate an old
            # permanent lock into a flat cooldown instead of silently re-enabling purchases.
            if context.strategy_state.state.get("risk_locked"):
                context.strategy_state.state["risk_locked"] = False
                previous_multiplier = 0.0
                context.strategy_state.state["risk_multiplier"] = 0.0
                context.strategy_state.state.setdefault(
                    "risk_reduced_since", getattr(context, "now", datetime.now()).strftime("%Y-%m-%d"))
            reduced_since = context.strategy_state.state.get("risk_reduced_since")
            market_recovery = RISK.can_recover(
                closes, reduced_since, getattr(context, "now", datetime.now()))
            degraded_recovery = len(closes) < CONFIG["risk"]["market_ma_days"] and RISK.can_recover_degraded(
                reduced_since, getattr(context, "now", datetime.now()), asset, initial_asset, not positions)
            may_recover = (market_recovery or degraded_recovery) and (previous_multiplier > 0 or not positions)
            if previous_multiplier < 1.0 and may_recover:
                recovered_multiplier = 0.5 if previous_multiplier <= 0 else 1.0
                context.strategy_state.state["peak_asset"] = asset
                context.strategy_state.state["risk_reduced_since"] = (
                    getattr(context, "now", datetime.now()).strftime("%Y-%m-%d")
                    if recovered_multiplier < 1.0 else None)
                _log("risk_recovery", mode="market" if market_recovery else "degraded_flat_cash",
                     from_multiplier=previous_multiplier, to_multiplier=recovered_multiplier,
                     asset=asset, initial_asset=initial_asset)
                peak = asset
                risk_state = type("RecoveredRisk", (), {
                    "multiplier": recovered_multiplier,
                    "reason": "staged_risk_recovery",
                    "allow_new_positions": recovered_multiplier > 0
                })()
            elif previous_multiplier < 1.0:
                peak = max(float(context.strategy_state.state.get("peak_asset", 0)), asset)
                risk_state = type("CooldownRisk", (), {
                    "multiplier": previous_multiplier,
                    "reason": context.strategy_state.state.get("risk_reason", "risk_cooldown"),
                    "allow_new_positions": False
                })()
            else:
                peak = max(float(context.strategy_state.state.get("peak_asset", 0)), asset)
                context.strategy_state.state["peak_asset"] = peak
                risk_state = RISK.market_state(closes, asset, peak)
            if risk_state.multiplier < 1.0 and previous_multiplier == 1.0:
                context.strategy_state.state["risk_reduced_since"] = getattr(
                    context, "now", datetime.now()).strftime("%Y-%m-%d")
        context.strategy_risk_multiplier = risk_state.multiplier
        context.strategy_state.state["risk_multiplier"] = risk_state.multiplier
        context.strategy_state.state["risk_reason"] = risk_state.reason
        context.strategy_state.state["risk_updated"] = getattr(context, "now", datetime.now()).isoformat()
        if risk_state.multiplier < 1:
            base_exposure = float(context.strategy_state.state.get(
                "market_target_exposure", 1 - CONFIG["portfolio"]["cash_reserve"]))
            current_weight = risk_state.multiplier * base_exposure
            per_position = min(CONFIG["portfolio"]["max_stock_weight"], current_weight / max(len(positions), 1))
            for pos in positions:
                pos_weight = float(pos.get("market_value", 0)) / asset if asset > 0 else 0
                if pos_weight <= per_position + float(CONFIG["execution"].get("target_weight_tolerance", 0.01)):
                    continue
                last = float(pos.get("price") or pos.get("last_price") or position_cost(pos))
                _submit_target(context, pos["symbol"], per_position, risk_state.reason, last)
            if risk_state.multiplier == 0 and positions:
                context.strategy_state.state["last_forced_exit_date"] = getattr(context, "now", datetime.now()).strftime("%Y-%m-%d")
        context.strategy_state.save()
        _log("portfolio_risk", multiplier=risk_state.multiplier, previous_multiplier=previous_multiplier,
             reason=risk_state.reason,
             strategy_time=getattr(context, "now", datetime.now()).isoformat(),
             asset=asset, peak_asset=peak, initial_asset=initial_asset,
             absolute_drawdown=absolute_drawdown,
             all_time_peak_asset=all_time_peak, all_time_drawdown=all_time_drawdown,
             benchmark_source=benchmark_source,
             risk_locked=context.strategy_state.state.get("risk_locked", False))
    except Exception as exc:
        # 风险数据失败时禁止新开仓，但不盲目清仓。
        context.strategy_risk_multiplier = 0.0
        context.strategy_state.state["risk_multiplier"] = 0.0
        context.strategy_state.state["risk_reason"] = "risk_data_degraded"
        context.strategy_state.save()
        _log("portfolio_risk_degraded", error=str(exc))
    synthetic_bars = []
    for pos in positions:
        symbol = pos["symbol"]
        try:
            quotes = current(symbols=symbol, fields="symbol,price")
            quote = quotes.iloc[-1] if isinstance(quotes, pd.DataFrame) and len(quotes) else (quotes[-1] if quotes else {})
            price = float(quote.get("price", 0))
            if price <= 0:
                minute = history_n(symbol=symbol, frequency="60s", count=1, fields="close", df=True)
                if minute is not None and len(minute): price = float(minute.iloc[-1]["close"])
            if price > 0: synthetic_bars.append({"symbol": symbol, "close": price})
        except Exception as exc:
            _log("risk_quote_failed", symbol=symbol, error=str(exc))
    if synthetic_bars:
        on_bar(context, synthetic_bars)
    _log("daily_risk_check", positions=len(positions), quotes=len(synthetic_bars))


def maintain_orders(context):
    """撤销超时未完成订单；部分成交后的剩余量留待下一风控周期重新评估。"""
    timeout = int(CONFIG["execution"]["order_timeout_seconds"])
    now = getattr(context, "now", datetime.now())
    try:
        orders = get_unfinished_orders() or []
    except Exception as exc:
        _log("unfinished_orders_failed", error=str(exc)); return
    for order in orders:
        created = order.get("created_at")
        if created is None:
            continue
        created = pd.Timestamp(created).to_pydatetime()
        if created.tzinfo and now.tzinfo is None: created = created.replace(tzinfo=None)
        if now.tzinfo and created.tzinfo is None: now_cmp = now.replace(tzinfo=None)
        else: now_cmp = now
        if (now_cmp - created).total_seconds() >= timeout:
            try:
                order_cancel(order)
                _log("order_cancel_requested", symbol=order.get("symbol"), order_id=order.get("cl_ord_id"), reason="timeout")
            except Exception as exc:
                _log("order_cancel_failed", symbol=order.get("symbol"), error=str(exc))


def on_bar(context, bars):
    items = bars if isinstance(bars, (list, tuple)) else [bars]
    positions = {p["symbol"]: p for p in _positions(context.account_id)}
    today = getattr(context, "now", datetime.now()).strftime("%Y-%m-%d")
    for bar in items:
        symbol = bar["symbol"]
        if symbol not in positions:
            continue
        pos, current = positions[symbol], float(bar["close"])
        state = context.strategy_state.state["positions"].setdefault(symbol, {})
        state.setdefault("entry_date", today)
        state.setdefault("holding_days", 0)
        state.setdefault("last_holding_date", today)
        if state["last_holding_date"] != today:
            state["holding_days"] = int(state.get("holding_days", 0)) + 1
            state["last_holding_date"] = today
        state.setdefault("peak", current)
        state.setdefault("atr", 0.0)
        state["peak"] = max(float(state.get("peak", current)), current)
        held = int(state.get("holding_days", 0))
        if position_available(pos) <= 0:
            _log("risk_exit_deferred", symbol=symbol, reason="t_plus_one_no_available"); continue
        leader = _cached_trend_signal(symbol, today, position_cost(pos))
        if leader["leader"]:
            state["leader_mode"] = True
            state["leader_detected_date"] = today
        if (context.strategy_role == "live" and state.get("leader_mode") and
                getattr(context, "now", datetime.now()).strftime("%H:%M:%S") >= "14:30:00"):
            fund_signal = live_main_fund_signal(symbol, getattr(context, "now", datetime.now()))
            state["main_fund_signal"] = fund_signal
            state["main_fund_withdrawal"] = bool(fund_signal.get("available") and
                                                   fund_signal.get("withdrawal"))
            _log("leader_fund_monitor", symbol=symbol, signal=fund_signal)
        reason = RISK.exit_reason(position_cost(pos), current, state["peak"],
                                  float(state.get("atr", 0)), held)
        if reason == "trailing_profit" and state.get("leader_mode"):
            withdrawal = bool(state.get("main_fund_withdrawal", False))
            if leader["trend_intact"] and not leader["distribution"] and not withdrawal:
                _log("leader_exit_suppressed", symbol=symbol, attempted_reason=reason,
                     leader_signal=leader, fund_signal=state.get("main_fund_signal"))
                reason = None
            elif withdrawal:
                reason = "leader_main_fund_exit"
            elif leader["distribution"]:
                reason = "leader_distribution_exit"
            else:
                reason = "leader_trend_break_exit"
        if reason:
            _submit_target(context, symbol, 0, reason, current)
    context.strategy_state.save()


def on_order_status(context, order):
    status = order.get("status")
    symbol = order.get("symbol")
    terminal = status in (OrderStatus_Filled, OrderStatus_DoneForDay, OrderStatus_Canceled,
                          OrderStatus_Rejected, OrderStatus_Stopped, OrderStatus_Expired)
    if terminal:
        for intent_id, item in list(context.strategy_state.state["active_orders"].items()):
            if item.get("symbol") == symbol:
                context.strategy_state.complete_intent(
                    intent_id,
                    successful=status == OrderStatus_Filled,
                    terminal=True,
                    outcome=str(status),
                )
    if status == OrderStatus_Filled and order.get("side") == OrderSide_Buy:
        state = context.strategy_state.state["positions"].setdefault(symbol, {})
        trade_day = getattr(context, "now", datetime.now()).strftime("%Y-%m-%d")
        state.setdefault("entry_date", trade_day)
        state.setdefault("holding_days", 0)
        state.setdefault("last_holding_date", trade_day)
        state.setdefault("peak", float(order.get("price", 0)))
        state.setdefault("pyramid_base_price", float(order.get("price", 0)))
        state.setdefault("atr", 0.0)
        context.strategy_state.save()
    if status == OrderStatus_Filled and order.get("side") == OrderSide_Sell:
        # Any sell starts a fresh risk/pyramid cycle for the remaining position. This avoids
        # carrying an obsolete peak or pyramid reference through a portfolio-level reduction.
        context.strategy_state.state.setdefault("exit_dates", {})[symbol] = getattr(
            context, "now", datetime.now()).strftime("%Y-%m-%d")
        context.strategy_state.state["positions"].pop(symbol, None)
        context.strategy_state.save()
    _log("order_status", symbol=symbol, status=status, volume=order.get("volume"),
         price=order.get("price"), reject_reason=order.get("ord_rej_reason"),
         reject_detail=order.get("ord_rej_reason_detail"))
