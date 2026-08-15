# -*- coding: utf-8 -*-
"""统一配置加载、校验与环境变量覆盖。"""
import json
import os
from copy import deepcopy

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
_configs = {}


def strategy_profile():
    """Return the only supported strategy profile.

    The profile label remains in runtime state filenames so existing conservative
    state can be reused safely, but profile switching is intentionally removed.
    """
    return "conservative"


def validate_config(cfg):
    required = ("data", "universe", "factors", "portfolio", "risk", "execution")
    missing = [key for key in required if key not in cfg]
    if missing:
        raise ValueError("配置缺少字段: " + ", ".join(missing))
    weights = cfg["factors"]["weights"]
    if not weights or abs(sum(weights.values()) - 1.0) > 1e-8:
        raise ValueError("因子权重之和必须为1")
    for name, value in weights.items():
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError("非法因子权重: %s" % name)
    p = cfg["portfolio"]
    if not 1 <= p.get("minimum_stocks", 1) <= p["max_stocks"] <= 50:
        raise ValueError("max_stocks必须在1到50之间")
    if not 0 < p["max_stock_weight"] <= 1 or not 0 <= p["cash_reserve"] < 1:
        raise ValueError("仓位参数越界")
    if p["minimum_stocks"] * p["max_stock_weight"] < 1 - p["cash_reserve"] - 1e-8:
        raise ValueError("持仓数量与单股上限无法容纳目标投资比例")
    if p.get("dynamic_exposure_enabled"):
        levels = p.get("breadth_exposure_levels", [])
        if not levels:
            raise ValueError("dynamic exposure requires breadth_exposure_levels")
        previous_breadth = previous_exposure = -1.0
        for level in levels:
            breadth = float(level.get("minimum_breadth", -1))
            exposure = float(level.get("target_exposure", -1))
            if not 0 <= breadth <= 1 or not 0 < exposure <= 1:
                raise ValueError("dynamic exposure level out of range")
            if breadth <= previous_breadth or exposure < previous_exposure:
                raise ValueError("dynamic exposure levels must increase by breadth")
            previous_breadth, previous_exposure = breadth, exposure
    for section in ("risk", "execution"):
        if not isinstance(cfg[section], dict):
            raise ValueError("%s必须为对象" % section)
    r = cfg["risk"]
    previous_activation = -1.0
    for level in r.get("trailing_levels", []):
        activation = float(level.get("activation", -1))
        drawdown = float(level.get("drawdown", -1))
        if activation <= previous_activation or not 0 < drawdown < 1:
            raise ValueError("trailing levels must have increasing activation and valid drawdown")
        previous_activation = activation
    force_exit = r.get("portfolio_drawdown_force_exit", r["portfolio_drawdown_stop"])
    if not (0 < r["portfolio_drawdown_reduce"] < force_exit <= r["portfolio_drawdown_stop"] < 1):
        raise ValueError("portfolio drawdown thresholds must be ordered: reduce < force_exit <= stop")
    if not force_exit <= r.get("all_time_peak_drawdown_lock", r["portfolio_drawdown_stop"]) <= r["portfolio_drawdown_stop"]:
        raise ValueError("all_time_peak_drawdown_lock must be between force_exit and portfolio stop")
    if not 0 <= cfg["execution"].get("target_weight_tolerance", 0.01) <= 0.05:
        raise ValueError("target_weight_tolerance must be between 0 and 0.05")
    pyramid = cfg["execution"].get("positive_pyramid", {"enabled": False})
    if pyramid.get("enabled"):
        initial = float(pyramid.get("initial_weight", 0))
        levels = pyramid.get("levels", [])
        if not 0 < initial < p["max_stock_weight"] or not levels:
            raise ValueError("positive pyramid requires 0 < initial_weight < max_stock_weight and levels")
        previous_profit, previous_weight = -1.0, initial
        for level in levels:
            profit = float(level.get("profit_threshold", -1))
            weight = float(level.get("target_weight", 0))
            if profit <= previous_profit or weight <= previous_weight or weight > p["max_stock_weight"] + 1e-12:
                raise ValueError("positive pyramid levels must have increasing profit and weight")
            previous_profit, previous_weight = profit, weight
    return cfg


def load_config(path=None):
    global _configs
    cache_key = os.path.abspath(path or CONFIG_PATH)
    if path is None and cache_key in _configs:
        return _configs[cache_key]
    with open(path or CONFIG_PATH, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    cfg = deepcopy(raw)
    cfg["profile"] = strategy_profile()
    cfg = validate_config(cfg)
    if path is None:
        _configs[cache_key] = cfg
    return cfg


def reload_config():
    global _configs
    _configs = {}
    return load_config()


def get(path, default=None):
    value = load_config()
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def data_root(cfg=None):
    cfg = cfg or load_config()
    section = cfg["data"]
    return os.path.abspath(os.environ.get(section["root_env"], section["default_root"]))
