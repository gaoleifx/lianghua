# -*- coding: utf-8 -*-
"""持仓资金监控：实盘使用真实日级主力资金，失败时安全降级为无信号。"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_CACHE = {}
_LOCK = threading.Lock()
_LAST_REQUEST = 0.0


def _request_json(url, params=None, headers=None, timeout=10):
    global _LAST_REQUEST
    with _LOCK:
        delay = 1.2 - (time.time() - _LAST_REQUEST)
        if delay > 0:
            time.sleep(delay)
        target = url + (("?" + urlencode(params)) if params else "")
        req = Request(target, headers=headers or {"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=timeout) as response:
            payload = response.read().decode("utf-8", "ignore")
        _LAST_REQUEST = time.time()
    return json.loads(payload)


def _eastmoney_daily_flow(code):
    market = 1 if str(code).startswith(("5", "6", "9")) else 0
    data = _request_json(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
        {
            "secid": "%d.%s" % (market, code),
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "lmt": "20",
        },
        {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
    ).get("data") or {}
    rows = []
    for line in data.get("klines") or []:
        parts = line.split(",")
        if len(parts) >= 6:
            rows.append({"date": parts[0], "main_net": float(parts[1]) if parts[1] != "-" else 0.0})
    return rows


def _sina_daily_flow(code):
    prefix = ("bj" if str(code).startswith(("92", "8")) else
              "sh" if str(code).startswith(("5", "6", "9")) else "sz")
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "MoneyFlow.ssl_qsfx_zjlrqs")
    params = {"page": 1, "num": 20, "sort": "opendate", "asc": 0,
              "daima": prefix + str(code)}
    target = url + "?" + urlencode(params)
    req = Request(target, headers={"User-Agent": "Mozilla/5.0",
                                   "Referer": "https://finance.sina.com.cn/"})
    with urlopen(req, timeout=10) as response:
        text = response.read().decode("utf-8", "ignore")
    payload = json.loads(text[text.index("["):text.rindex("]") + 1])
    return [{"date": str(row.get("opendate", ""))[:10],
             "main_net": float(row.get("netamount") or 0)} for row in reversed(payload)]


def live_main_fund_signal(symbol, now=None):
    """返回主力撤退信号；数据缺失/接口异常绝不等同于撤退。"""
    now = now or datetime.now()
    day = now.strftime("%Y-%m-%d")
    key = (symbol, day)
    if key in _CACHE:
        return _CACHE[key]
    code = str(symbol).split(".")[-1]
    rows, source, error = [], None, None
    try:
        rows, source = _eastmoney_daily_flow(code), "eastmoney_20d"
    except Exception as exc:
        error = str(exc)
        try:
            rows, source = _sina_daily_flow(code), "sina_backup_20d"
        except Exception as backup_exc:
            error += "; backup=" + str(backup_exc)
    usable = [row for row in rows if row.get("date") and row["date"] <= day]
    recent = usable[-5:]
    last3 = recent[-3:]
    negatives = sum(float(row["main_net"]) < 0 for row in last3)
    withdrawal = bool(len(last3) == 3 and negatives == 3 and
                      sum(float(row["main_net"]) for row in last3) < 0 and
                      sum(float(row["main_net"]) for row in recent) < 0)
    result = {
        "available": bool(recent), "withdrawal": withdrawal, "source": source,
        "negative_days_3": negatives,
        "main_net_3d": sum(float(row["main_net"]) for row in last3),
        "main_net_5d": sum(float(row["main_net"]) for row in recent),
        "error": error,
    }
    _CACHE[key] = result
    return result

