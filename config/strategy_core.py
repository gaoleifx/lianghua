# -*- coding: utf-8 -*-
"""共享策略核心：本地数据、截面因子、组合构建、风控状态与交易意图。"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from config_loader import data_root, load_config


def to_gm_symbol(code: str) -> str:
    code = str(code).split(".")[-1]
    return ("SHSE." if code.startswith(("5", "6", "9")) else "SZSE.") + code


def to_code(symbol: str) -> str:
    return str(symbol).split(".")[-1]


@dataclass
class UniverseSnapshot:
    as_of: str
    symbols: List[str]
    excluded: Dict[str, str] = field(default_factory=dict)
    degraded: bool = False


@dataclass
class FactorRow:
    symbol: str
    industry: str
    raw: Dict[str, Optional[float]]
    scores: Dict[str, Optional[float]]
    composite: float
    percentile: float
    volatility: float
    atr: float
    confirmations: int = 0
    details: Dict[str, object] = field(default_factory=dict)


@dataclass
class TargetPosition:
    symbol: str
    weight: float
    rank: int
    score: float
    reason: str


@dataclass
class TradeIntent:
    symbol: str
    target_weight: float
    side: str
    reason: str
    intent_id: str


@dataclass
class Tradability:
    can_buy: bool
    can_sell: bool
    reason: str
    last_price: float
    buy_price: float
    sell_price: float


@dataclass
class RiskState:
    multiplier: float
    reason: str
    allow_new_positions: bool


class LocalDatabase:
    """只读访问本地SQLite；所有查询显式限制as_of，避免未来数据。"""

    def __init__(self, root=None, config=None):
        self.config = config or load_config()
        self.root = root or data_root(self.config)
        self.stocks_path = os.path.join(self.root, "stocks.db")
        self.financial_path = os.path.join(self.root, "financial.db")

    @staticmethod
    def _connect(path):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        return sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)

    def stock_master(self, codes: Iterable[str]) -> pd.DataFrame:
        codes = list(dict.fromkeys(to_code(c) for c in codes))
        if not codes:
            return pd.DataFrame()
        marks = ",".join("?" for _ in codes)
        with closing(self._connect(self.stocks_path)) as con:
            return pd.read_sql_query(
                "SELECT code,name,sector,market,last_update FROM stocks WHERE code IN (%s)" % marks,
                con, params=codes,
            )

    def bars(self, codes: Iterable[str], as_of: str, count=130) -> pd.DataFrame:
        requested = list(dict.fromkeys(str(c) for c in codes))
        if not requested:
            return pd.DataFrame()
        benchmark_symbols = set([self.config["universe"]["index_symbol"]] +
                                self.config["universe"].get("fallback_index_symbols", []))
        index_symbols = [symbol for symbol in requested if symbol in benchmark_symbols]
        stock_codes = list(dict.fromkeys(to_code(symbol) for symbol in requested
                                         if symbol not in benchmark_symbols))
        frames = []
        with closing(self._connect(self.stocks_path)) as con:
            if stock_codes:
                marks = ",".join("?" for _ in stock_codes)
                query = """SELECT code,date,open,close,high,low,volume FROM (
                  SELECT code,date,open,close,high,low,volume,
                         ROW_NUMBER() OVER(PARTITION BY code ORDER BY date DESC) AS rn
                  FROM history WHERE code IN (%s) AND date<?
                ) WHERE rn<=? ORDER BY code,date""" % marks
                frames.append(pd.read_sql_query(query, con, params=stock_codes + [as_of, count]))
            if index_symbols:
                exists = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_history'").fetchone()
                if exists:
                    marks = ",".join("?" for _ in index_symbols)
                    query = """SELECT symbol AS code,date,open,close,high,low,volume FROM (
                      SELECT symbol,date,open,close,high,low,volume,
                             ROW_NUMBER() OVER(PARTITION BY symbol ORDER BY date DESC) AS rn
                      FROM index_history WHERE symbol IN (%s) AND date<?
                    ) WHERE rn<=? ORDER BY code,date""" % marks
                    frames.append(pd.read_sql_query(query, con, params=index_symbols + [as_of, count]))
        usable = [frame for frame in frames if not frame.empty]
        return pd.concat(usable, ignore_index=True) if usable else pd.DataFrame()

    def financials(self, codes: Iterable[str], as_of: str) -> pd.DataFrame:
        codes = list(dict.fromkeys(to_code(c) for c in codes))
        if not codes:
            return pd.DataFrame()
        # 本地库无公告日期，使用报告期+保守滞后作为可用日期。
        lag = int(self.config["data"]["financial_lag_days"])
        cutoff = (pd.Timestamp(as_of) - pd.Timedelta(days=lag)).strftime("%Y-%m-%d")
        marks = ",".join("?" for _ in codes)
        query = """SELECT * FROM (
          SELECT code,report_date,revenue,net_profit,total_assets,total_liabilities,equity,roe,eps,pe,pb,
                 ROW_NUMBER() OVER(PARTITION BY code ORDER BY report_date DESC, id DESC) AS rn
          FROM financial WHERE code IN (%s) AND report_date<=?
        ) WHERE rn=1""" % marks
        with closing(self._connect(self.financial_path)) as con:
            return pd.read_sql_query(query, con, params=codes + [cutoff])


def _safe_div(a, b):
    return np.nan if b is None or not np.isfinite(b) or b == 0 else a / b


def _number(value):
    try:
        return float(value) if value is not None else np.nan
    except (TypeError, ValueError):
        return np.nan


def _winsorized_z(series: pd.Series, q: float, higher_is_better=True) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    valid = numeric.dropna()
    if len(valid) < 3:
        return pd.Series(np.nan, index=series.index)
    lo, hi = valid.quantile(q), valid.quantile(1 - q)
    clipped = numeric.clip(lo, hi)
    std = clipped.std(ddof=0)
    z = (clipped - clipped.mean()) / std if std and np.isfinite(std) else clipped * 0
    return z if higher_is_better else -z


def calculate_market_breadth(bars: pd.DataFrame) -> Dict[str, float]:
    """Share of point-in-time stocks in a confirmed 20/60-day uptrend."""
    total = bullish = 0
    if bars is None or bars.empty:
        return {"total": 0, "bullish": 0, "ratio": 0.0}
    for _, frame in bars.groupby("code", sort=False):
        closes = frame.sort_values("date")["close"].astype(float)
        if len(closes) < 61:
            continue
        total += 1
        ma20 = float(closes.tail(20).mean())
        ma60 = float(closes.tail(60).mean())
        ma20_prior = float(closes.rolling(20).mean().iloc[-6])
        return20 = float(closes.iloc[-1] / closes.iloc[-21] - 1)
        if closes.iloc[-1] > ma20 > ma60 and ma20 > ma20_prior and return20 > 0:
            bullish += 1
    return {"total": total, "bullish": bullish, "ratio": bullish / total if total else 0.0}


def market_target_exposure(breadth_ratio: float, config=None) -> float:
    """Return the configured gross exposure for the observed market breadth."""
    cfg = config or load_config()
    portfolio = cfg["portfolio"]
    if not portfolio.get("dynamic_exposure_enabled", False):
        return 1.0 - float(portfolio["cash_reserve"])
    exposure = 0.0
    levels = sorted(portfolio.get("breadth_exposure_levels", []),
                    key=lambda item: float(item["minimum_breadth"]))
    for level in levels:
        if float(breadth_ratio) + 1e-12 >= float(level["minimum_breadth"]):
            exposure = float(level["target_exposure"])
    return min(1.0, max(0.0, exposure))


class FactorEngine:
    NAMES = ("relative_strength", "trend_acceleration", "breakout", "volume_confirmation",
             "trend_efficiency", "downside_risk", "liquidity", "quality_growth")

    def __init__(self, config=None):
        self.config = config or load_config()

    def build_raw(self, universe: UniverseSnapshot, bars: pd.DataFrame, financials: pd.DataFrame,
                  master: pd.DataFrame, benchmark_bars: pd.DataFrame = None) -> pd.DataFrame:
        """Build price/volume factors plus an optional validated point-in-time GM snapshot."""
        meta = master.set_index("code") if not master.empty else pd.DataFrame()
        required_financial = {"symbol", "pub_date", "roe_weight_avg", "inc_oper_yoy",
                              "net_prof_pcom_yoy", "net_cf_oper", "net_prof_pcom"}
        financial_map = (financials.set_index("symbol") if financials is not None and
                         not financials.empty and required_financial.issubset(financials.columns)
                         else pd.DataFrame())
        candidates = []
        benchmark_bars = benchmark_bars if benchmark_bars is not None else pd.DataFrame()
        benchmark_closes = (benchmark_bars.sort_values("date")["close"].astype(float)
                             if not benchmark_bars.empty else pd.Series(dtype=float))
        benchmark_20 = (float(benchmark_closes.iloc[-1] / benchmark_closes.iloc[-21] - 1)
                        if len(benchmark_closes) >= 61 else np.nan)
        benchmark_60 = (float(benchmark_closes.iloc[-1] / benchmark_closes.iloc[-61] - 1)
                        if len(benchmark_closes) >= 61 else np.nan)
        # Build the lookup once. Repeated boolean scans made every cross-section O(stocks^2)
        # and were especially costly during rolling validation.
        bar_groups = {str(code): frame.sort_values("date").copy()
                      for code, frame in bars.groupby("code", sort=False)}
        for symbol in universe.symbols:
            code = to_code(symbol)
            frame = bar_groups.get(code, pd.DataFrame())
            if len(frame) < self.config["data"]["minimum_history_days"]:
                universe.excluded[symbol] = "history_short"
                continue
            closes = frame["close"].astype(float)
            highs, lows = frame["high"].astype(float), frame["low"].astype(float)
            volumes = frame["volume"].astype(float)
            returns = closes.pct_change().dropna()
            turnover = float((closes * volumes).tail(self.config["universe"]["liquidity_lookback"]).mean())
            if turnover < self.config["universe"]["minimum_average_turnover"]:
                universe.excluded[symbol] = "illiquid"
                continue
            return_20 = float(closes.iloc[-1] / closes.iloc[-21] - 1)
            return_60 = float(closes.iloc[-1] / closes.iloc[-61] - 1)
            return_5 = float(closes.iloc[-1] / closes.iloc[-6] - 1)
            ma20 = float(closes.tail(20).mean())
            ma60 = float(closes.tail(60).mean())
            rolling_ma20 = closes.rolling(20).mean()
            rolling_ma60 = closes.rolling(60).mean()
            ma20_slope_5d = float(ma20 / rolling_ma20.iloc[-6] - 1)
            ma60_slope_5d = float(ma60 / rolling_ma60.iloc[-6] - 1)
            volatility = float(returns.tail(20).std(ddof=0) * math.sqrt(252))
            prev = closes.shift(1)
            tr = pd.concat([(highs-lows), (highs-prev).abs(), (lows-prev).abs()], axis=1).max(axis=1)
            atr = float(tr.tail(14).mean())
            atr_pct = _safe_div(atr, float(closes.iloc[-1]))
            if return_20 < self.config["factors"].get("minimum_20d_return", -1):
                universe.excluded[symbol] = "negative_momentum"
                continue
            if return_60 < self.config["factors"].get("minimum_60d_return", -1):
                universe.excluded[symbol] = "weak_60d_momentum"
                continue
            if return_5 > self.config["factors"].get("maximum_5d_return", math.inf):
                universe.excluded[symbol] = "short_term_overheated"
                continue
            if return_20 > self.config["factors"]["overheat_20d_return"]:
                universe.excluded[symbol] = "overheated"
                continue
            if self.config["factors"].get("require_bullish_ma_alignment", False) and not (
                    float(closes.iloc[-1]) > ma20 > ma60):
                universe.excluded[symbol] = "ma_not_bullish"
                continue
            if ma20_slope_5d <= self.config["factors"].get("minimum_ma20_slope_5d", 0):
                universe.excluded[symbol] = "ma20_slope_not_positive"
                continue
            if volatility > self.config["factors"].get("maximum_annualized_volatility", math.inf):
                universe.excluded[symbol] = "excessive_volatility"
                continue
            if atr_pct > self.config["factors"].get("maximum_atr_ratio", math.inf):
                universe.excluded[symbol] = "excessive_atr_ratio"
                continue
            industry = str(meta.loc[code].get("sector", "UNKNOWN")) if not meta.empty and code in meta.index else "UNKNOWN"
            name = str(meta.loc[code].get("name", "")) if not meta.empty and code in meta.index else ""
            theme_text = (name + " " + industry).upper()
            theme_match = any(str(keyword).upper() in theme_text
                              for keyword in self.config["factors"].get("theme_keywords", []))
            quality_growth = np.nan
            if not financial_map.empty and symbol in financial_map.index:
                fin = financial_map.loc[symbol]
                if isinstance(fin, pd.DataFrame):
                    fin = fin.sort_values(["pub_date"]).iloc[-1]
                roe = _number(fin.get("roe_weight_avg"))
                revenue_yoy = _number(fin.get("inc_oper_yoy"))
                profit_yoy = _number(fin.get("net_prof_pcom_yoy"))
                net_cash = _number(fin.get("net_cf_oper"))
                net_profit = _number(fin.get("net_prof_pcom"))
                cash_conversion = _safe_div(net_cash, abs(net_profit))
                components = [(0.35, roe), (0.25, revenue_yoy), (0.30, profit_yoy),
                              (0.10, np.clip(cash_conversion, -2, 2))]
                available_weight = sum(weight for weight, value in components if np.isfinite(value))
                if available_weight >= 0.60:
                    quality_growth = sum(weight * value for weight, value in components
                                         if np.isfinite(value)) / available_weight
            daily_amount = closes * volumes
            volume_ratio = _safe_div(volumes.tail(5).mean(), volumes.tail(20).mean())
            amount_stability = _safe_div(daily_amount.tail(20).std(ddof=0), daily_amount.tail(20).mean())
            up_volume = volumes.tail(20)[returns.tail(20).reindex(volumes.tail(20).index).fillna(0) > 0].mean()
            down_volume = volumes.tail(20)[returns.tail(20).reindex(volumes.tail(20).index).fillna(0) < 0].mean()
            up_down_volume = _safe_div(up_volume, down_volume)
            if not np.isfinite(up_down_volume):
                up_down_volume = 1.0
            if volume_ratio > self.config["factors"].get("maximum_volume_ratio", math.inf):
                universe.excluded[symbol] = "abnormal_volume_spike"
                continue
            high_60 = float(highs.tail(60).max())
            prior_high_20 = float(highs.iloc[-21:-1].max())
            distance_60_high = float(closes.iloc[-1] / high_60 - 1)
            breakout_20 = float(closes.iloc[-1] / prior_high_20 - 1)
            path = float(returns.tail(20).abs().sum())
            efficiency = _safe_div(return_20, path)
            downside = returns.tail(20)[returns.tail(20) < 0]
            downside_vol = float(downside.std(ddof=0) * math.sqrt(252)) if len(downside) > 1 else 0.0
            worst_day = abs(float(min(returns.tail(20).min(), 0)))
            candidates.append({
                "symbol": symbol, "industry": industry,
                "return_5": return_5, "return_20": return_20, "return_60": return_60,
                "ma20_slope_5d": ma20_slope_5d, "ma60_slope_5d": ma60_slope_5d,
                "volume_ratio": float(volume_ratio), "up_down_volume_ratio": float(up_down_volume),
                "distance_60_high": distance_60_high, "breakout_20": breakout_20,
                "trend_efficiency": float(efficiency), "downside_volatility": downside_vol,
                "worst_day": worst_day, "atr_ratio": float(atr_pct),
                "turnover": turnover, "amount_stability": float(amount_stability),
                "volatility": volatility, "atr": atr, "last_close": float(closes.iloc[-1]),
                "theme_match": 1.0 if theme_match else 0.0,
                "quality_growth": quality_growth,
            })
        raw = pd.DataFrame(candidates)
        if raw.empty:
            return raw
        # In production the benchmark is mandatory; unit-level callers may use the point-in-time
        # universe median as a deterministic fallback.
        market_20 = benchmark_20 if np.isfinite(benchmark_20) else float(raw["return_20"].median())
        market_60 = benchmark_60 if np.isfinite(benchmark_60) else float(raw["return_60"].median())
        raw["industry_return_20"] = raw.groupby("industry")["return_20"].transform("median")
        raw["industry_return_60"] = raw.groupby("industry")["return_60"].transform("median")
        raw["industry_excess_market_20"] = raw["industry_return_20"] - market_20
        raw["industry_excess_market_60"] = raw["industry_return_60"] - market_60
        raw["excess_market_20"] = raw["return_20"] - market_20
        raw["excess_market_60"] = raw["return_60"] - market_60
        raw["relative_strength_20"] = 0.5 * raw["excess_market_20"] + 0.5 * (
            raw["return_20"] - raw["industry_return_20"])
        raw["relative_strength_60"] = 0.5 * raw["excess_market_60"] + 0.5 * (
            raw["return_60"] - raw["industry_return_60"])
        # Relative strength is an entry gate, not a forced exit. Keeping weak-relative rows in
        # the scored matrix allows the wider 25% holding buffer to work as designed.
        raw["entry_relative_strength_ok"] = ((raw["excess_market_20"] > 0) &
                                              (raw["excess_market_60"] > 0))
        raw["entry_industry_strength_ok"] = ((raw["industry_excess_market_20"] > 0) &
                                              (raw["industry_excess_market_60"] > 0))
        raw["relative_strength"] = 0.45 * raw["relative_strength_20"] + 0.55 * raw["relative_strength_60"]
        raw["trend_acceleration"] = (0.60 * (raw["return_20"] - raw["return_60"] / 3.0) +
                                     0.25 * raw["ma20_slope_5d"] + 0.15 * raw["ma60_slope_5d"])
        raw["breakout"] = (-raw["distance_60_high"].abs() +
                           0.5 * raw["breakout_20"].clip(-0.05, 0.03))
        raw["volume_confirmation"] = (0.60 * np.log(raw["volume_ratio"].clip(0.2, 3.0)) +
                                      0.40 * np.log(raw["up_down_volume_ratio"].clip(0.2, 5.0)))
        raw["downside_risk"] = -(0.50 * raw["downside_volatility"] +
                                 0.30 * raw["worst_day"] + 0.20 * raw["atr_ratio"])
        raw["liquidity"] = np.log1p(raw["turnover"]) - 0.20 * raw["amount_stability"].clip(0, 3)
        raw["rs20_percentile"] = raw["relative_strength_20"].rank(ascending=False, pct=True)
        raw["rs60_percentile"] = raw["relative_strength_60"].rank(ascending=False, pct=True)
        raw["confirm_rs20"] = raw["rs20_percentile"] <= 0.30
        raw["confirm_rs60"] = raw["rs60_percentile"] <= 0.30
        raw["confirm_near_high"] = raw["distance_60_high"] >= -0.05
        raw["confirm_volume"] = raw["volume_ratio"] >= 1.05
        raw["confirm_efficiency"] = raw["trend_efficiency"] >= 0.35
        confirmation_factors = self.config["factors"].get(
            "confirmation_factors", ["near_high", "volume", "efficiency"])
        confirm_cols = ["confirm_" + name for name in confirmation_factors]
        unknown = [name for name in confirm_cols if name not in raw.columns]
        if unknown:
            raise ValueError("unknown confirmation factors: %s" % ",".join(unknown))
        raw["confirmation_count"] = raw[confirm_cols].sum(axis=1).astype(int)
        return raw

    def score(self, raw: pd.DataFrame) -> List[FactorRow]:
        if raw.empty:
            return []
        q = self.config["factors"]["winsorize_quantile"]
        scored = raw.copy()
        for factor in self.NAMES:
            # A factor may be intentionally unavailable (for example, callers that
            # do not have a validated point-in-time financial snapshot).  Treat it
            # as missing and let the coverage rule reweight the remaining factors.
            if factor not in scored.columns:
                scored[factor] = np.nan
            scored[factor + "_score"] = scored.groupby("industry", dropna=False)[factor].transform(
                lambda s: _winsorized_z(s, q)
            )
            fallback = _winsorized_z(scored[factor], q)
            scored[factor + "_score"] = scored[factor + "_score"].fillna(fallback)
        weights = self.config["factors"]["weights"]
        composites = []
        for _, row in scored.iterrows():
            available = [(f, row[f + "_score"]) for f in self.NAMES if pd.notna(row[f + "_score"])]
            coverage = sum(weights[f] for f, _ in available)
            composites.append(np.nan if coverage < self.config["factors"]["minimum_coverage"] else
                              sum(weights[f] * v for f, v in available) / coverage)
        scored["composite"] = composites
        scored = scored.dropna(subset=["composite"]).sort_values("composite", ascending=False)
        scored["percentile"] = scored["composite"].rank(ascending=False, pct=True)
        result = []
        for _, row in scored.iterrows():
            confirmations = int(row.get("confirmation_count", 0))
            configured_confirmations = self.config["factors"].get(
                "confirmation_factors", ["near_high", "volume", "efficiency"])
            confirm_names = [name for name in configured_confirmations
                             if bool(row.get("confirm_" + name, False))]
            result.append(FactorRow(
                row.symbol, row.industry, {f: None if pd.isna(row[f]) else float(row[f]) for f in self.NAMES},
                {f: None if pd.isna(row[f+"_score"]) else float(row[f+"_score"]) for f in self.NAMES},
                float(row.composite), float(row.percentile), float(row.volatility), float(row.atr),
                confirmations, {
                    "confirmation_names": confirm_names,
                    "relative_strength_20": float(row.get("relative_strength_20", np.nan)),
                    "relative_strength_60": float(row.get("relative_strength_60", np.nan)),
                    "distance_60_high": float(row.get("distance_60_high", np.nan)),
                    "volume_ratio": float(row.get("volume_ratio", np.nan)),
                    "trend_efficiency": float(row.get("trend_efficiency", np.nan)),
                    "theme_match": bool(row.get("theme_match", 0)),
                    "entry_relative_strength_ok": bool(row.get("entry_relative_strength_ok", False)),
                    "entry_industry_strength_ok": bool(row.get("entry_industry_strength_ok", False)),
                    "industry_excess_market_20": float(row.get("industry_excess_market_20", np.nan)),
                    "industry_excess_market_60": float(row.get("industry_excess_market_60", np.nan)),
                    "return_5": float(row.get("return_5", np.nan)),
                    "return_20": float(row.get("return_20", np.nan)),
                    "return_60": float(row.get("return_60", np.nan)),
                    "quality_growth": (None if pd.isna(row.get("quality_growth"))
                                       else float(row.get("quality_growth"))),
                }))
        return result


class PortfolioBuilder:
    def __init__(self, config=None):
        self.config = config or load_config()

    def build(self, factors: List[FactorRow], holdings=None, risk_multiplier=1.0,
              target_count=None, allow_new_positions=True, target_exposure=None) -> List[TargetPosition]:
        holdings = set(holdings or [])
        p, fc = self.config["portfolio"], self.config["factors"]
        minimum_confirmations = int(fc.get("minimum_potential_confirmations", 0))
        require_relative_strength = bool(fc.get("require_relative_strength_for_entry", False))
        dynamic_minimum = p["max_stocks"] if p.get("dynamic_exposure_enabled", False) else p["minimum_stocks"]
        entry_count = max(int(fc.get("minimum_entry_rank_count", dynamic_minimum)), dynamic_minimum,
                          int(math.ceil(len(factors) * fc["entry_percentile"])))
        entry_symbols = {row.symbol for row in factors[:entry_count]}
        eligible = [x for x in factors if
                    (allow_new_positions and x.symbol not in holdings and x.symbol in entry_symbols and
                     (not require_relative_strength or
                      x.details.get("entry_relative_strength_ok", True)) and
                     x.confirmations >= minimum_confirmations) or
                    (x.symbol in holdings and x.percentile <= fc["exit_percentile"])]
        selected, industries = [], {}
        target_count = int(target_count or p["max_stocks"])
        target_count = max(p["minimum_stocks"], min(target_count, p["max_stocks"]))
        industry_slots = max(1, int(p["max_industry_weight"] / min(p["max_stock_weight"], 1.0/target_count) + 1e-9))
        for item in eligible:
            if industries.get(item.industry, 0) >= industry_slots:
                continue
            selected.append(item); industries[item.industry] = industries.get(item.industry, 0) + 1
            if len(selected) >= target_count:
                break
        if not selected:
            return []
        inv = np.array([1 / max(x.volatility, 0.08) for x in selected])
        base_exposure = (1 - p["cash_reserve"] if target_exposure is None
                         else min(1.0, max(0.0, float(target_exposure))))
        investable = base_exposure * risk_multiplier
        weights = inv / inv.sum() * investable
        weights = np.minimum(weights, p["max_stock_weight"])
        return [TargetPosition(x.symbol, float(w), rank + 1, x.composite, "cross_section_rank")
                for rank, (x, w) in enumerate(zip(selected, weights))]

    def affordable_count(self, factors, prices, nav, target_exposure=None,
                         holdings=None, allow_new_positions=True):
        """从最多持仓向下寻找可按整手实现的组合，最低为minimum_stocks。"""
        p = self.config["portfolio"]
        if nav <= p.get("small_account_threshold", 0):
            small = self.build_affordable_small(
                factors, prices, nav, target_exposure=target_exposure,
                holdings=holdings, allow_new_positions=allow_new_positions)
            return len(small) if len(small) >= p["minimum_stocks"] else 0
        for count in range(p["max_stocks"], p["minimum_stocks"] - 1, -1):
            targets = self.build(factors, holdings=holdings, target_count=count,
                                 allow_new_positions=allow_new_positions,
                                 target_exposure=target_exposure)
            if len(targets) == count and not self.validate_affordability(targets, prices, nav):
                return count
        return 0

    def build_affordable_small(self, factors, prices, nav, risk_multiplier=1.0,
                               holdings=None, allow_new_positions=True, target_exposure=None):
        """Small accounts prefer three affordable industries and degrade safely to two."""
        p = self.config["portfolio"]
        if risk_multiplier <= 0:
            return []
        base_exposure = (1 - p["cash_reserve"] if target_exposure is None
                         else min(1.0, max(0.0, float(target_exposure))))
        lot = self.config["execution"]["lot_size"]
        holdings = set(holdings or [])
        fc = self.config["factors"]
        minimum_confirmations = int(fc.get("minimum_potential_confirmations", 0))
        require_relative_strength = bool(fc.get("require_relative_strength_for_entry", False))
        dynamic_minimum = p["max_stocks"] if p.get("dynamic_exposure_enabled", False) else p["minimum_stocks"]
        entry_count = max(int(fc.get("minimum_entry_rank_count", dynamic_minimum)), dynamic_minimum,
                          int(math.ceil(len(factors) * fc["entry_percentile"])))
        entry_symbols = {row.symbol for row in factors[:entry_count]}
        eligible = [row for row in factors if row.symbol in holdings and row.percentile <= fc["exit_percentile"]]
        eligible += [row for row in factors if allow_new_positions and row.symbol not in holdings and
                     row.symbol in entry_symbols and
                     (not require_relative_strength or
                      row.details.get("entry_relative_strength_ok", True)) and
                     row.confirmations >= minimum_confirmations]
        retained_scores = [row.composite for row in eligible if row.symbol in holdings]
        replacement_floor = (min(retained_scores) + fc.get("replacement_score_margin", 0.0)
                             if retained_scores else -math.inf)
        if not p.get("dynamic_exposure_enabled", False):
            count = p["minimum_stocks"]
            weight = min(p["max_stock_weight"], base_exposure / count) * risk_multiplier
            budget = nav * weight
            chosen, industries = [], set()
            for row in eligible:
                if retained_scores and row.symbol not in holdings and row.composite < replacement_floor:
                    continue
                price = float(prices.get(row.symbol, 0))
                if row.industry in industries or price <= 0 or round_lot(budget / price, lot) < lot:
                    continue
                chosen.append(TargetPosition(row.symbol, weight, len(chosen) + 1,
                                             row.composite, "small_account_rank"))
                industries.add(row.industry)
                if len(chosen) == count:
                    break
            return chosen
        for count in range(p["max_stocks"], p["minimum_stocks"] - 1, -1):
            replacing_full_portfolio = len(retained_scores) >= count
            filtered = [row for row in eligible if not replacing_full_portfolio or
                        row.symbol in holdings or row.composite >= replacement_floor]
            selected, industries = [], set()
            for row in filtered:
                if row.industry in industries or float(prices.get(row.symbol, 0)) <= 0:
                    continue
                selected.append(row); industries.add(row.industry)
                if len(selected) == count:
                    break
            if len(selected) != count:
                continue
            inv = np.array([1 / max(row.volatility, 0.08) for row in selected])
            weights = np.minimum(inv / inv.sum() * base_exposure * risk_multiplier,
                                 p["max_stock_weight"])
            targets = [TargetPosition(row.symbol, float(weight), index + 1,
                                      row.composite, "small_account_rank")
                       for index, (row, weight) in enumerate(zip(selected, weights))]
            if all(round_lot(nav * target.weight / float(prices[target.symbol]), lot) >= lot
                   for target in targets):
                return targets
        return []

    def validate_affordability(self, targets, prices, nav):
        """目标仓位必须至少能买一手，否则实盘组合不可实现。"""
        lot = self.config["execution"]["lot_size"]
        failures = []
        for target in targets:
            price = float(prices.get(target.symbol, 0))
            budget = float(nav) * target.weight
            if price <= 0 or round_lot(budget / price, lot) < lot:
                failures.append({"symbol": target.symbol, "budget": budget,
                                 "required": price * lot if price > 0 else None})
        return failures


class StateStore:
    def __init__(self, path):
        self.path = path
        self.state = {"positions": {}, "active_orders": {}, "completed_intents": {}, "last_rebalance": None, "peak_asset": 0.0}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    self.state.update(json.load(handle))
            except (OSError, ValueError):
                pass
        return self.state

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(self.state, handle, ensure_ascii=False, indent=2)
        os.replace(temp, self.path)

    def intent_once(self, intent: TradeIntent) -> bool:
        if intent.intent_id in self.state["active_orders"] or intent.intent_id in self.state["completed_intents"]:
            return False
        self.state["active_orders"][intent.intent_id] = asdict(intent)
        self.save(); return True

    def complete_intent(self, intent_id, successful=True, terminal=False, outcome=None):
        item = self.state["active_orders"].pop(intent_id, None)
        if (successful or terminal) and item:
            if outcome:
                item["outcome"] = outcome
            self.state["completed_intents"][intent_id] = item
            # 只保留最近10个交易日左右，防止状态无限增长。
            if len(self.state["completed_intents"]) > 500:
                for key in list(self.state["completed_intents"])[:-500]:
                    self.state["completed_intents"].pop(key, None)
        self.save()


class RiskEngine:
    def __init__(self, config=None): self.config = config or load_config()

    def market_state(self, closes: Iterable[float], asset: float, peak_asset: float) -> RiskState:
        closes = np.asarray(list(closes), dtype=float)
        cfg = self.config["risk"]
        market_ratio = closes[-1] / np.mean(closes[-cfg["market_ma_days"]:]) if len(closes) >= cfg["market_ma_days"] else 1
        drawdown = 1 - asset / peak_asset if peak_asset > 0 else 0
        if drawdown >= cfg.get("portfolio_drawdown_force_exit", cfg["portfolio_drawdown_stop"]):
            return RiskState(0.0, "portfolio_drawdown_force_exit", False)
        if market_ratio <= cfg["market_stop_below_ma"] or drawdown >= cfg["portfolio_drawdown_stop"]:
            return RiskState(0.0, "market_or_portfolio_stop", False)
        if market_ratio <= cfg["market_reduce_below_ma"] or drawdown >= cfg["portfolio_drawdown_reduce"]:
            return RiskState(0.5, "market_or_portfolio_reduce", True)
        return RiskState(1.0, "normal", True)

    def can_recover(self, closes: Iterable[float], reduced_since, now) -> bool:
        """Reduced risk may recover only after a cooldown and confirmed index strength."""
        closes = np.asarray(list(closes), dtype=float)
        cfg = self.config["risk"]
        days = int(cfg.get("risk_recovery_cooldown_days", 10))
        if not reduced_since or len(closes) < cfg["market_ma_days"]:
            return False
        if (pd.Timestamp(now).tz_localize(None) - pd.Timestamp(reduced_since).tz_localize(None)).days < days:
            return False
        ratio = closes[-1] / np.mean(closes[-cfg["market_ma_days"]:])
        return bool(ratio >= cfg.get("risk_recovery_above_ma", 1.01))

    def can_recover_degraded(self, reduced_since, now, asset, initial_asset, is_flat) -> bool:
        """Conservative fallback when benchmark history is unavailable."""
        cfg = self.config["risk"]
        if not is_flat or not reduced_since or initial_asset <= 0:
            return False
        days = int(cfg.get("risk_recovery_degraded_cooldown_days", 20))
        elapsed = (pd.Timestamp(now).tz_localize(None) - pd.Timestamp(reduced_since).tz_localize(None)).days
        capital_ok = asset / initial_asset >= cfg.get("risk_recovery_min_initial_asset_ratio", 0.98)
        return bool(elapsed >= days and capital_ok)

    def exit_reason(self, entry, current, peak, atr, holding_days):
        cfg = self.config["risk"]
        loss = (current - entry) / entry
        atr_stop = entry - cfg["atr_stop_multiple"] * atr if atr and atr > 0 else -math.inf
        if loss <= -cfg["hard_stop_loss"] or current <= atr_stop:
            return "hard_or_atr_stop"
        trailing_drawdown = None
        levels = cfg.get("trailing_levels") or [{
            "activation": cfg["trailing_activation"], "drawdown": cfg["trailing_drawdown"]}]
        peak_profit = peak / entry - 1
        for level in sorted(levels, key=lambda item: float(item["activation"])):
            if peak_profit + 1e-12 >= float(level["activation"]):
                trailing_drawdown = float(level["drawdown"])
        if trailing_drawdown is not None and current <= peak * (1 - trailing_drawdown):
            return "trailing_profit"
        if (self.config["portfolio"].get("enforce_maximum_holding_days", True) and
                holding_days >= self.config["portfolio"]["maximum_holding_days"]):
            return "maximum_holding_days"
        return None


def protected_limit_price(last_price, side, bps):
    factor = 1 + bps / 10000.0 if side == "BUY" else 1 - bps / 10000.0
    return round(float(last_price) * factor + 1e-9, 2)


def round_lot(shares, lot_size=100):
    return max(0, int(shares // lot_size) * lot_size)


def position_cost(position):
    return float(position.get("vwap_open") or position.get("vwap") or position.get("avg_cost") or 0)


def position_available(position):
    return int(position.get("available_now") if position.get("available_now") is not None
               else position.get("available", position.get("available_volume", 0)))


def can_increase_existing_position(position, current_price, minimum_profit=0.005):
    """Never average down: an existing position may grow only after confirmed profit."""
    if not position:
        return True
    cost = position_cost(position)
    price = float(current_price or 0)
    return bool(cost > 0 and price >= cost * (1 + float(minimum_profit)))


def positive_pyramid_target(desired_target, position, current_price, pyramid_config,
                            base_price=None, current_weight=0.0):
    """Return a non-decreasing target weight unlocked only by confirmed profit."""
    desired = float(desired_target)
    if not pyramid_config or not pyramid_config.get("enabled"):
        return desired, "pyramid_disabled"
    initial = min(desired, float(pyramid_config["initial_weight"]))
    if not position:
        return initial, "pyramid_initial"
    reference = float(base_price or position_cost(position))
    price = float(current_price or 0)
    if reference <= 0 or price <= 0:
        return min(desired, max(float(current_weight), initial)), "pyramid_reference_missing"
    profit = price / reference - 1
    unlocked, stage = initial, "pyramid_hold_initial"
    for index, level in enumerate(pyramid_config.get("levels", []), 1):
        if profit + 1e-12 >= float(level["profit_threshold"]):
            unlocked = min(desired, float(level["target_weight"]))
            stage = "pyramid_add_%d" % index
    # A profit retracement never causes a pyramid reduction; exits remain the risk engine's job.
    return min(desired, max(float(current_weight), unlocked)), stage


def evaluate_tradability(instrument, quote, protection_bps=20):
    """依据真实盘口和当日涨跌停判断可交易性。"""
    last = float(quote.get("price") or 0)
    upper = float(instrument.get("upper_limit") or 0)
    lower = float(instrument.get("lower_limit") or 0)
    if int(instrument.get("is_suspended") or 0):
        return Tradability(False, False, "suspended", last, 0, 0)
    if last <= 0:
        return Tradability(False, False, "no_quote", last, 0, 0)
    quotes = quote.get("quotes") or []
    best_bid = float(quotes[0].get("bid_p") or 0) if quotes else 0
    best_ask = float(quotes[0].get("ask_p") or 0) if quotes else 0
    at_upper = upper > 0 and (last >= upper - 1e-6 or best_ask <= 0)
    at_lower = lower > 0 and (last <= lower + 1e-6 or best_bid <= 0)
    buy_base = best_ask or last
    sell_base = best_bid or last
    buy_price = min(upper, protected_limit_price(buy_base, "BUY", protection_bps)) if upper else protected_limit_price(buy_base, "BUY", protection_bps)
    sell_price = max(lower, protected_limit_price(sell_base, "SELL", protection_bps)) if lower else protected_limit_price(sell_base, "SELL", protection_bps)
    return Tradability(not at_upper, not at_lower,
                       "limit_up" if at_upper else ("limit_down" if at_lower else "ok"),
                       last, buy_price, sell_price)
