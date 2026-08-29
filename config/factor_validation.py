# -*- coding: utf-8 -*-
"""Read-only rolling factor validation using point-in-time local data."""
import json
import os
import sqlite3
import sys
from copy import deepcopy

import numpy as np
import pandas as pd

from config_loader import data_root, load_config
from strategy_core import (FactorEngine, LocalDatabase, UniverseSnapshot,
                           calculate_market_breadth, market_entry_allowed,
                           to_gm_symbol)


def evaluation_dates(start="2022-01-01", end="2026-06-30"):
    """Monthly point-in-time observations; the evaluation date itself is never a signal bar."""
    # Monthly observations improve coverage of regime transitions while remaining reproducible
    # on the local SQLite store. Every feature still uses only bars strictly before the date.
    return [x.strftime("%Y-%m-%d") for x in pd.date_range(start, end, freq="MS")]


def forward_returns(db, codes, as_of, horizons=(5, 10, 20)):
    marks = ",".join("?" for _ in codes)
    query = """SELECT code,date,close FROM history WHERE code IN (%s) AND date>=?
               ORDER BY code,date""" % marks
    with db._connect(db.stocks_path) as con:
        frame = pd.read_sql_query(query, con, params=list(codes) + [as_of])
    output = {}
    for code, group in frame.groupby("code"):
        prices = group.close.astype(float).tolist()
        if not prices:
            continue
        output[to_gm_symbol(code)] = {
            h: prices[h] / prices[0] - 1 for h in horizons if len(prices) > h
        }
    return output


def forward_returns_from_frame(frame, codes, as_of, horizons=(5, 10, 20)):
    """Calculate forward returns from the immutable validation snapshot."""
    wanted = set(str(code).split(".")[-1] for code in codes)
    future = frame[(frame["code"].isin(wanted)) & (frame["date"] >= as_of)]
    output = {}
    for code, group in future.groupby("code", sort=False):
        prices = group.sort_values("date")["close"].astype(float).head(max(horizons) + 1).tolist()
        output[to_gm_symbol(code)] = {
            h: prices[h] / prices[0] - 1 for h in horizons if len(prices) > h
        }
    return output


def forward_metrics_from_frame(frame, codes, as_of, benchmark_code=None,
                               horizons=(5, 10, 20), stop_loss=0.10):
    """Return forward return, excess return, path risk and rebound labels.

    The first price is the hypothetical next-session entry mark.  This helper is
    for offline validation only; no future column is consumed by live scoring.
    """
    wanted = {str(code).split(".")[-1] for code in codes}
    if benchmark_code:
        wanted.add(str(benchmark_code).split(".")[-1])
    future = frame[(frame["code"].astype(str).isin(wanted)) &
                   (frame["date"] >= as_of)]
    paths = {}
    for code, group in future.groupby("code", sort=False):
        path = group.sort_values("date")["close"].astype(float).head(max(horizons) + 1)
        if len(path) < 2:
            continue
        paths[str(code)] = path.to_numpy()
    benchmark_path = (paths.get(str(benchmark_code).split(".")[-1])
                      if benchmark_code else None)
    output = {}
    for code, path in paths.items():
        if benchmark_code and code == str(benchmark_code).split(".")[-1]:
            continue
        result = {}
        for horizon in horizons:
            if len(path) <= horizon:
                continue
            returns = path[:horizon + 1] / path[0] - 1.0
            result[horizon] = {
                "return": float(returns[-1]),
                "max_drawdown": float((returns - np.maximum.accumulate(returns)).min()),
                "hit_stop": bool(returns.min() <= -abs(float(stop_loss))),
                "first_dip_then_rise": bool(returns.min() < 0 and returns[-1] > 0),
            }
            if benchmark_path is not None and len(benchmark_path) > horizon:
                result[horizon]["excess_return"] = float(
                    returns[-1] - (benchmark_path[horizon] / benchmark_path[0] - 1.0))
        output[to_gm_symbol(code)] = result
    return output


def market_regime(benchmark, breadth_ratio):
    """Classify the signal date using only benchmark history and breadth."""
    closes = benchmark.sort_values("date")["close"].astype(float).to_numpy()
    if len(closes) < 61:
        return "unknown"
    ma20, ma60 = closes[-20:].mean(), closes[-60:].mean()
    ret20 = closes[-1] / closes[-21] - 1.0
    slope20 = ma20 / pd.Series(closes).rolling(20).mean().iloc[-6] - 1.0
    if breadth_ratio < 0.20:
        return "extreme_weak"
    if closes[-1] > ma20 > ma60 and ret20 > 0 and slope20 > 0:
        return "strong"
    if closes[-1] < ma60 and ret20 < 0:
        return "weak"
    return "range"


def regime_transition(benchmark, breadth_ratio):
    """Classify turning points from benchmark history available at the signal date."""
    current = market_regime(benchmark, breadth_ratio)
    ordered = benchmark.sort_values("date").reset_index(drop=True)
    if len(ordered) < 82:
        return current
    prior = ordered.iloc[:-20]
    previous = market_regime(prior, breadth_ratio)
    closes = ordered["close"].astype(float).to_numpy()
    fast_fall = (closes[-1] / closes[-11] - 1.0 <= -0.08 or
                 closes[-1] / closes[-21] - 1.0 <= -0.12)
    if fast_fall:
        return "fast_fall"
    if previous in ("weak", "extreme_weak") and current == "strong":
        return "weak_to_strong"
    if previous == "strong" and current in ("weak", "extreme_weak"):
        return "strong_to_weak"
    return current


def _apply_weight_override(cfg):
    """Apply an explicit offline-only weight override for controlled ablations."""
    for env_name, key in (("FACTOR_VALIDATION_WEIGHTS", "weights"),
                          ("FACTOR_VALIDATION_WEAK_WEIGHTS", "weak_market_weights")):
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        target = cfg["factors"].setdefault(key, {})
        for item in raw.split(","):
            name, value = item.split("=", 1)
            if name not in FactorEngine.NAMES:
                raise ValueError("unknown validation factor weight: %s" % name)
            target[name] = float(value)
    return cfg


def run(start="2022-01-01", end="2026-06-30"):
    cfg = _apply_weight_override(load_config())
    db = LocalDatabase(config=cfg); engine = FactorEngine(cfg)
    with db._connect(db.stocks_path) as con:
        master_all = pd.read_sql_query("SELECT code,name,sector,market,last_update FROM stocks", con)
        preload_start = (pd.Timestamp(start) - pd.Timedelta(days=260)).strftime("%Y-%m-%d")
        preload_end = (pd.Timestamp(end) + pd.Timedelta(days=45)).strftime("%Y-%m-%d")
        all_bars = pd.read_sql_query(
            "SELECT code,date,open,close,high,low,volume FROM history WHERE date>=? AND date<=? ORDER BY code,date",
            con, params=[preload_start, preload_end])
    symbols = [to_gm_symbol(x) for x in master_all.code.astype(str)]
    records = []
    for as_of in evaluation_dates(start, end):
        bars = all_bars[all_bars["date"] < as_of].groupby("code", sort=False).tail(130).copy()
        available = set(bars.code.astype(str))
        current = [s for s in symbols if s.split(".")[-1] in available]
        snapshot = UniverseSnapshot(as_of, current)
        benchmark = pd.DataFrame()
        benchmark_source = None
        for candidate in [cfg["universe"]["index_symbol"]] + cfg["universe"].get("fallback_index_symbols", []):
            code = candidate.split(".")[-1]
            local = all_bars[(all_bars["code"] == code) & (all_bars["date"] < as_of)].tail(130)
            if len(local) >= 61:
                benchmark = local
                benchmark_source = candidate
                break
        if benchmark.empty:
            continue
        # Financial factors are deliberately disabled until the local field semantics,
        # announcement dates and coverage pass validation.
        breadth = calculate_market_breadth(bars)
        raw = engine.build_raw(snapshot, bars, pd.DataFrame(), master_all, benchmark,
                               market_breadth=breadth["ratio"])
        if raw.empty:
            continue
        scored_rows = engine.score(raw)
        score_map = {row.symbol: row for row in scored_rows}
        old_cfg = deepcopy(cfg)
        old_cfg["factors"]["require_benchmark_bullish_for_new_entries"] = True
        old_gate_allowed = market_entry_allowed(
            benchmark.sort_values("date")["close"], breadth["ratio"], old_cfg)
        new_gate_allowed = market_entry_allowed(
            benchmark.sort_values("date")["close"], breadth["ratio"], cfg)
        future = forward_metrics_from_frame(
            all_bars, raw.symbol.str.split(".").str[-1].tolist(), as_of,
            benchmark_code=benchmark_source)
        regime = market_regime(benchmark, breadth["ratio"])
        transition = regime_transition(benchmark, breadth["ratio"])
        for _, row in raw.iterrows():
            item = {"date": as_of, "year": as_of[:4], "regime": regime,
                    "regime_transition": transition,
                    "market_breadth": breadth["ratio"], "symbol": row.symbol,
                    "old_gate_allowed": old_gate_allowed,
                    "new_gate_allowed": new_gate_allowed,
                    "old_gate_blocked": not old_gate_allowed}
            scored_row = score_map.get(row.symbol)
            item.update({"composite": (scored_row.composite if scored_row else np.nan),
                         "score_percentile": (scored_row.percentile if scored_row else np.nan),
                         "selected_top15": bool(scored_row and scored_row.percentile <=
                                                 float(cfg["factors"].get("entry_percentile", 0.15)))})
            item.update({f: row.get(f) for f in engine.NAMES})
            item.update({name: row.get(name) for name in (
                "return_5", "return_20", "return_60", "prior_return_5", "early_strength_5d",
                "excess_market_5", "recent_relative_improvement",
                "volume_ratio", "trend_efficiency", "confirmation_count",
                "industry_excess_market_20", "industry_excess_market_60",
                "entry_relative_strength_ok", "entry_industry_strength_ok", "confirmation_count")})
            for h in (5, 10, 20):
                metrics = future.get(row.symbol, {}).get(h, {})
                item.update({"fwd_%d" % h: metrics.get("return"),
                             "excess_%d" % h: metrics.get("excess_return"),
                             "drawdown_%d" % h: metrics.get("max_drawdown"),
                             "hit_stop_%d" % h: metrics.get("hit_stop"),
                             "first_dip_then_rise_%d" % h: metrics.get("first_dip_then_rise")})
            records.append(item)
    data = pd.DataFrame(records)
    results = []
    for year, group in data.groupby("year"):
        for factor in engine.NAMES:
            for horizon in (5, 10, 20):
                pair = group[[factor, "fwd_%d" % horizon]].dropna()
                ic = pair.corr(method="spearman").iloc[0, 1] if len(pair) >= 30 else np.nan
                results.append({"year": year, "factor": factor, "horizon": horizon,
                                "observations": len(pair), "rank_ic": ic})
    result = pd.DataFrame(results)
    summary = result.groupby(["factor", "horizon"]).agg(
        mean_ic=("rank_ic", "mean"), positive_years=("rank_ic", lambda x: int((x > 0).sum())),
        years=("rank_ic", "count"), min_ic=("rank_ic", "min")).reset_index()
    data["return5_bucket"] = pd.cut(
        data["return_5"], [-np.inf, .02, .04, .06, .08, .10, .12, np.inf],
        labels=["<=2%", "2-4%", "4-6%", "6-8%", "8-10%", "10-12%", ">12%"])
    return5_diagnostics = data.groupby("return5_bucket", observed=True).agg(
        observations=("symbol", "size"), fwd_5=("fwd_5", "mean"),
        fwd_10=("fwd_10", "mean"), fwd_20=("fwd_20", "mean")).reset_index()
    gate_diagnostics = data.groupby(
        ["entry_industry_strength_ok", "entry_relative_strength_ok"], dropna=False).agg(
        observations=("symbol", "size"), fwd_5=("fwd_5", "mean"),
        fwd_10=("fwd_10", "mean"), fwd_20=("fwd_20", "mean")).reset_index()
    regime_diagnostics = data.groupby("regime", dropna=False).agg(
        observations=("symbol", "size"), breadth=("market_breadth", "mean"),
        fwd_5=("fwd_5", "mean"), fwd_10=("fwd_10", "mean"),
        excess_5=("excess_5", "mean"), excess_10=("excess_10", "mean"),
        drawdown_5=("drawdown_5", "mean"), drawdown_10=("drawdown_10", "mean"),
        stop_rate_5=("hit_stop_5", "mean"), stop_rate_10=("hit_stop_10", "mean")
    ).reset_index()
    transition_diagnostics = data.groupby("regime_transition", dropna=False).agg(
        observations=("symbol", "size"), breadth=("market_breadth", "mean"),
        fwd_5=("fwd_5", "mean"), fwd_10=("fwd_10", "mean"),
        excess_5=("excess_5", "mean"), excess_10=("excess_10", "mean"),
        drawdown_10=("drawdown_10", "mean"), stop_rate_10=("hit_stop_10", "mean")
    ).reset_index()
    gate_opportunity_diagnostics = data.groupby(
        ["old_gate_blocked", "new_gate_allowed"], dropna=False).agg(
        observations=("symbol", "size"), fwd_5=("fwd_5", "mean"),
        fwd_10=("fwd_10", "mean"), excess_5=("excess_5", "mean"),
        excess_10=("excess_10", "mean"), drawdown_10=("drawdown_10", "mean"),
        stop_rate_10=("hit_stop_10", "mean")
    ).reset_index()
    selection_diagnostics = data.groupby(["regime", "selected_top15"], dropna=False).agg(
        observations=("symbol", "size"), score_percentile=("score_percentile", "mean"),
        fwd_5=("fwd_5", "mean"), fwd_10=("fwd_10", "mean"),
        excess_5=("excess_5", "mean"), excess_10=("excess_10", "mean"),
        drawdown_10=("drawdown_10", "mean"), stop_rate_10=("hit_stop_10", "mean")
    ).reset_index()
    data["opportunity_class"] = np.select(
        [data["old_gate_blocked"] & data["new_gate_allowed"],
         ~data["new_gate_allowed"],
         data["new_gate_allowed"] & ~data["selected_top15"],
         data["new_gate_allowed"] & data["selected_top15"]],
        ["legacy_gate_blocked_but_new_allowed", "current_extreme_gate_blocked",
         "not_selected_after_market_control", "top15_candidate"],
        default="unclassified")
    opportunity_diagnostics = data.groupby(["regime", "opportunity_class"], dropna=False).agg(
        observations=("symbol", "size"), fwd_5=("fwd_5", "mean"),
        fwd_10=("fwd_10", "mean"), excess_5=("excess_5", "mean"),
        excess_10=("excess_10", "mean"), drawdown_10=("drawdown_10", "mean"),
        stop_rate_10=("hit_stop_10", "mean"), score_percentile=("score_percentile", "mean")
    ).reset_index()
    composite_diagnostics = []
    for regime, group in data.groupby("regime", dropna=False):
        for horizon in (5, 10):
            target = group[["composite", "fwd_%d" % horizon,
                            "excess_%d" % horizon]].dropna()
            if target.empty:
                continue
            rank_ic = target[["composite", "fwd_%d" % horizon]].corr(
                method="spearman").iloc[0, 1]
            top = target[target["composite"] >= target["composite"].quantile(.85)]
            composite_diagnostics.append({
                "regime": regime, "horizon": horizon,
                "observations": len(target), "rank_ic": rank_ic,
                "top15_observations": len(top),
                "top15_fwd": float(top["fwd_%d" % horizon].mean()),
                "top15_excess": float(top["excess_%d" % horizon].mean()),
                "top15_drawdown_10": float(
                    group.loc[top.index, "drawdown_10"].mean()),
                "top15_stop_rate_10": float(
                    group.loc[top.index, "hit_stop_10"].mean()),
            })
    early_strength_diagnostics = []
    improvement_signals = ("early_strength_5d", "recent_relative_improvement")
    for regime, group in data.groupby("regime", dropna=False):
        for signal in improvement_signals:
            for horizon in (5, 10):
                pair = group[[signal, "fwd_%d" % horizon]].dropna()
                early_strength_diagnostics.append({
                    "regime": regime, "signal": signal, "horizon": horizon,
                    "observations": len(pair),
                    "rank_ic": (pair.corr(method="spearman").iloc[0, 1]
                                if len(pair) >= 30 else np.nan),
                    "mean_signal": float(group[signal].mean()),
                })
    confirmation_diagnostics = []
    for regime, group in data.groupby("regime", dropna=False):
        for minimum in (0, 1, 2):
            selected = group[group["confirmation_count"].fillna(0) >= minimum]
            confirmation_diagnostics.append({
                "regime": regime, "minimum_confirmations": minimum,
                "observations": len(selected),
                "fwd_5": float(selected["fwd_5"].mean()),
                "fwd_10": float(selected["fwd_10"].mean()),
                "excess_5": float(selected["excess_5"].mean()),
                "excess_10": float(selected["excess_10"].mean()),
                "drawdown_10": float(selected["drawdown_10"].mean()),
                "stop_rate_10": float(selected["hit_stop_10"].mean()),
            })
    payload = {"period": [start, end], "rows": len(data), "signal_cutoff": "strictly_before_date",
               "financials_enabled": False,
               "annual": result.replace({np.nan: None}).to_dict("records"),
               "summary": summary.replace({np.nan: None}).to_dict("records"),
               "return5_diagnostics": return5_diagnostics.replace({np.nan: None}).to_dict("records"),
               "gate_diagnostics": gate_diagnostics.replace({np.nan: None}).to_dict("records"),
               "regime_diagnostics": regime_diagnostics.replace({np.nan: None}).to_dict("records"),
               "transition_diagnostics": transition_diagnostics.replace(
                   {np.nan: None}).to_dict("records"),
               "gate_opportunity_diagnostics": gate_opportunity_diagnostics.replace(
                   {np.nan: None}).to_dict("records"),
               "selection_diagnostics": selection_diagnostics.replace(
                   {np.nan: None}).to_dict("records"),
               "opportunity_diagnostics": opportunity_diagnostics.replace(
                   {np.nan: None}).to_dict("records"),
               "composite_diagnostics": pd.DataFrame(
                   composite_diagnostics).replace({np.nan: None}).to_dict("records"),
               "early_strength_diagnostics": pd.DataFrame(
                   early_strength_diagnostics).replace({np.nan: None}).to_dict("records"),
               "confirmation_diagnostics": pd.DataFrame(
                   confirmation_diagnostics).replace({np.nan: None}).to_dict("records"),
               # Keep the point-in-time cross-section so later diagnostics can test
               # combinations of improvement, industry strength and risk without
               # re-reading the database.  This file contains no account data.
               "observations": data.replace({np.nan: None}).to_dict("records")}
    path = os.environ.get(
        "FACTOR_VALIDATION_OUTPUT",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "factor_validation_result.json"))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(summary.sort_values(["horizon", "mean_ic"], ascending=[True, False]).to_string(index=False))
    print("saved", path)
    return payload


if __name__ == "__main__":
    run(*(sys.argv[1:3] or ["2022-01-01", "2026-06-30"]))
