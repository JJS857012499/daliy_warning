# -*- coding: utf-8 -*-
"""中证红利ETF（515080）每日波段报告 + 波段预测。

收盘后输出当日行情总结，并根据均线/MACD/RSI/波段位置等给出方向结论与操作建议。
复用 monitor.py 的数据获取与微信推送能力；参数从环境变量读取。
"""

import argparse
import json
import os
import sys

from monitor import compute_levels, fetch_kline, local_now, send_wechat

MA_PERIODS = [5, 10, 20, 60, 120, 250]


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def macd(closes):
    """返回 (dif, dea, hist列表)。"""
    if len(closes) < 35:
        return None, None, []
    k12, k26 = 2 / 13, 2 / 27
    ema12 = closes[0]
    ema26 = closes[0]
    difs = []
    for c in closes:
        ema12 = c * k12 + ema12 * (1 - k12)
        ema26 = c * k26 + ema26 * (1 - k26)
        difs.append(ema12 - ema26)
    k9 = 2 / 10
    dea = difs[0]
    deas = []
    for x in difs:
        dea = x * k9 + dea * (1 - k9)
        deas.append(dea)
    hist = [2 * (d - e) for d, e in zip(difs, deas)]
    return difs[-1], deas[-1], hist


def hist_trend(hist):
    if len(hist) < 3:
        return "平稳"
    a, b, c = hist[-3], hist[-2], hist[-1]
    if c > b > a:
        return "放大"
    if c < b < a:
        return "缩小"
    return "平稳"


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def position_pct(closes, highs, lows, n):
    if len(closes) < n:
        return None
    lo, hi = min(lows[-n:]), max(highs[-n:])
    if hi == lo:
        return 50.0
    return (closes[-1] - lo) / (hi - lo) * 100


def volume_ratio(volumes):
    if len(volumes) < 6:
        return None
    avg5 = sum(volumes[-6:-1]) / 5
    if avg5 == 0:
        return None
    return volumes[-1] / avg5


def bollinger(closes, n=20, k=2.0):
    if len(closes) < n:
        return None, None, None
    mid = sum(closes[-n:]) / n
    var = sum((c - mid) ** 2 for c in closes[-n:]) / n
    std = var ** 0.5
    return mid + k * std, mid, mid - k * std


def trend_text(close, ma):
    ma5, ma10, ma20, ma60 = ma.get(5), ma.get(10), ma.get(20), ma.get(60)
    if all(v is not None for v in (ma5, ma10, ma20, ma60)):
        if ma5 > ma10 > ma20 > ma60:
            return "多头排列"
        if ma5 < ma10 < ma20 < ma60:
            return "空头排列"
    if ma20 is not None and ma60 is not None:
        if close > ma20 > ma60:
            return "偏多（价上MA20/60）"
        if close < ma20 < ma60:
            return "偏空（价下MA20/60）"
    if ma20 is not None:
        return "价上MA20" if close > ma20 else "价下MA20"
    return "--"


def advice_text(direction, pos):
    if direction == "偏多":
        if pos >= 80:
            return "趋势偏多但近波段高位，冲高可部分止盈，勿追高"
        if pos <= 20:
            return "趋势转多且处低位，可逢回踩分批建仓"
        return "上行趋势，持有为主，回踩MA20/60可分批加仓"
    if direction == "偏空":
        if pos >= 80:
            return "趋势转空且处高位，反弹减仓离场观望"
        if pos <= 20:
            return "超跌但趋势未反转，可小仓试仓等企稳信号"
        return "空头趋势，反抽MA20压力减仓，观望为主"
    if pos >= 80:
        return "区间震荡偏高位，接近压力分批高抛"
    if pos <= 20:
        return "区间震荡偏低位，接近支撑分批低吸"
    return "区间震荡，支撑低吸压力高抛，控制仓位"


def analyze(close, ma, dif, dea, hist, rsi_val, pos):
    score = 0.0
    ma20, ma60 = ma.get(20), ma.get(60)
    ma5, ma10 = ma.get(5), ma.get(10)
    if ma20 is not None:
        score += 1 if close > ma20 else -1
    if ma20 is not None and ma60 is not None:
        score += 1 if ma20 > ma60 else -1
    if ma60 is not None:
        score += 0.5 if close > ma60 else -0.5
    if all(v is not None for v in (ma5, ma10, ma20, ma60)):
        if ma5 > ma10 > ma20 > ma60:
            score += 1
        elif ma5 < ma10 < ma20 < ma60:
            score -= 1
    if dif is not None and dea is not None:
        score += 0.5 if dif > dea else -0.5
    ht = hist_trend(hist)
    if ht == "放大":
        score += 0.5 if (hist and hist[-1] > 0) else -0.5
    elif ht == "缩小":
        score += 0.5 if (hist and hist[-1] < 0) else -0.5
    if rsi_val is not None:
        if rsi_val > 70:
            score -= 0.5
        elif rsi_val < 30:
            score += 0.5
    pos = pos if pos is not None else 50.0
    if pos > 80:
        score -= 0.5
    elif pos < 20:
        score += 0.5

    if score >= 1.5:
        direction = "偏多"
    elif score <= -1.5:
        direction = "偏空"
    else:
        direction = "震荡"
    return direction, score, advice_text(direction, pos)


def macd_text(dif, dea, hist):
    if dif is None or dea is None:
        return "--"
    sign = "红柱" if dif > dea else "绿柱"
    ht = hist_trend(hist)
    if ht in ("放大", "缩小"):
        sign += ht
    return sign


def rsi_text(v):
    if v is None:
        return "--"
    if v >= 70:
        return "%.1f 超买" % v
    if v <= 30:
        return "%.1f 超卖" % v
    return "%.1f 中性" % v


def position_text(pos):
    if pos is None:
        return "--"
    if pos >= 80:
        return "高位 %.0f%%" % pos
    if pos <= 20:
        return "低位 %.0f%%" % pos
    return "中位 %.0f%%" % pos


def fmt(v):
    return "--" if v is None else "%.3f" % v


def fmtp(v):
    return "--" if v is None else "%.1f%%" % v


def collect(symbol, name, round_step, ktype="day", ma_periods=None, unit="日", title_word="波段日报"):
    ma_periods = ma_periods or MA_PERIODS
    count = 260
    dates, closes, highs, lows, volumes, current = fetch_kline(symbol, count, ktype)
    closes[-1] = current
    latest_date = dates[-1]

    ma = {p: sma(closes, p) for p in ma_periods}
    dif, dea, hist = macd(closes)
    rsi_val = rsi(closes)
    pos60 = position_pct(closes, highs, lows, 60)
    pos120 = position_pct(closes, highs, lows, 120)
    vol_ratio = volume_ratio(volumes)
    boll_up, boll_mid, boll_low = bollinger(closes)

    _, support, resistance, _, _ = compute_levels(closes, highs, lows, [20, 60], round_step)

    close = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else close
    change_pct = (close - prev_close) / prev_close * 100 if prev_close else 0.0

    direction, score, advice = analyze(close, ma, dif, dea, hist, rsi_val, pos60)

    sup_txt = "%.3f" % support[1] if support else "--"
    res_txt = "%.3f" % resistance[1] if resistance else "--"

    return {
        "title": "%s %s" % (name, title_word),
        "name": name,
        "symbol": symbol,
        "unit": unit,
        "latest_date": latest_date,
        "date": latest_date,
        "close": close,
        "close_text": "%.3f (%+.2f%%)" % (close, change_pct),
        "trend": trend_text(close, ma),
        "ma": ma,
        "dif": dif, "dea": dea, "hist": hist,
        "macd_text": macd_text(dif, dea, hist),
        "rsi": rsi_val,
        "rsi_text": rsi_text(rsi_val),
        "bias20": ((close - ma[20]) / ma[20] * 100) if ma.get(20) else None,
        "bias60": ((close - ma[60]) / ma[60] * 100) if ma.get(60) else None,
        "pos60": pos60,
        "pos120": pos120,
        "position_text": position_text(pos60),
        "vol_ratio": vol_ratio,
        "boll_up": boll_up, "boll_mid": boll_mid, "boll_low": boll_low,
        "support": support, "resistance": resistance,
        "remark": "支撑 %s / 压力 %s" % (sup_txt, res_txt),
        "direction": direction,
        "score": score,
        "advice": advice,
    }


def print_report(d):
    line = "=" * 46
    print(line)
    print(d["title"])
    print("日期: %s" % d["date"])
    print("收盘: %s   量比: %s" % (d["close_text"], fmt(d["vol_ratio"])))
    print("趋势: %s" % d["trend"])
    ma_items = ", ".join("MA%d %s" % (p, fmt(v)) for p, v in sorted(d["ma"].items()))
    print("均线: " + ma_items)
    print("MACD: %s（DIF %s / DEA %s）" % (d["macd_text"], fmt(d["dif"]), fmt(d["dea"])))
    print("RSI(14): %s" % d["rsi_text"])
    print("乖离: BIAS20 %s / BIAS60 %s" % (fmtp(d["bias20"]), fmtp(d["bias60"])))
    print("波段位置: 60%s %s / 120%s %s" % (d["unit"], fmtp(d["pos60"]), d["unit"], fmtp(d["pos120"])))
    print("布林带: 上 %s / 中 %s / 下 %s" % (fmt(d["boll_up"]), fmt(d["boll_mid"]), fmt(d["boll_low"])))
    print("支撑压力: %s" % d["remark"])
    print("-" * 46)
    print("波段预测: %s（评分 %+.1f）" % (d["direction"], d["score"]))
    print("操作建议: %s" % d["advice"])
    print(line)


def build_report_data(fields, d):
    color = "#173177"

    def f(key):
        return fields.get(key, key)

    unit = d.get("unit", "日")
    ma = d.get("ma") or {}
    keep = [5, 20, 60]
    ma_names = "/".join(str(p) for p in keep if p in ma)
    ma_vals = "/".join(fmt(ma[p]) for p in keep if p in ma)
    ma_text = ("MA%s %s" % (ma_names, ma_vals)) if ma_names else "--"
    boll_text = "上 %s / 中 %s / 下 %s" % (fmt(d["boll_up"]), fmt(d["boll_mid"]), fmt(d["boll_low"]))

    return {
        f("title"): {"value": d.get("title", "--"), "color": color},
        f("date"): {"value": d.get("date", "--"), "color": color},
        f("close"): {"value": "%s 量比 %s" % (d.get("close_text", "--"), fmt(d.get("vol_ratio"))), "color": color},
        f("trend"): {"value": d.get("trend", "--"), "color": color},
        f("position"): {"value": "60%s %s / 120%s %s" % (unit, fmtp(d.get("pos60")), unit, fmtp(d.get("pos120"))), "color": color},
        f("macd"): {"value": "%s(DIF%s/DEA%s)" % (d.get("macd_text", "--"), fmt(d.get("dif")), fmt(d.get("dea"))), "color": color},
        f("rsi"): {"value": "%s 乖离 BIAS20 %s/60 %s" % (d.get("rsi_text", "--"), fmtp(d.get("bias20")), fmtp(d.get("bias60"))), "color": color},
        f("predict"): {"value": "%s 评分%+.1f" % (d.get("direction", "--"), d.get("score", 0.0)), "color": color},
        f("advice"): {"value": d.get("advice", "--"), "color": color},
        f("ma"): {"value": ma_text, "color": color},
        f("boll"): {"value": boll_text, "color": color},
        f("remark"): {"value": d.get("remark", "--"), "color": color},
    }


def main():
    parser = argparse.ArgumentParser(description="中证红利ETF 波段报告")
    parser.add_argument('--once', action='store_true', help='跑一次并打印，不推送')
    parser.add_argument('--test-send', action='store_true', help='发送一条测试微信消息（真实数据）')
    parser.add_argument('--week', action='store_true', help='周线级别（每周五推送）')
    args = parser.parse_args()

    symbol = os.getenv('SYMBOL', 'sh515080')
    name = os.getenv('NAME', '中证红利ETF')
    round_step = float(os.getenv('ROUND_STEP', '0.1'))
    wechat = {
        'app_id': os.getenv('APP_ID', ''),
        'app_secret': os.getenv('APP_SECRET', ''),
        'user_ids': [u for u in os.getenv('USER_ID', '').split('\n') if u],
        'template_id': os.getenv('REPORT_TEMPLATE_ID', ''),
        'fields': json.loads(os.getenv('REPORT_FIELDS', '{}')),
    }

    if args.week:
        ktype, ma_periods, unit, title_word = "week", [5, 10, 20, 30, 60], "周", "波段周报"
    else:
        ktype, ma_periods, unit, title_word = "day", MA_PERIODS, "日", "波段日报"

    try:
        d = collect(symbol, name, round_step, ktype, ma_periods, unit, title_word)
    except Exception as e:
        print("数据获取失败: %s" % e)
        sys.exit(1)

    if args.test_send:
        send_wechat(wechat, build_report_data(wechat["fields"], d))
        return

    if args.once:
        print_report(d)
        return

    if d["latest_date"] != local_now().strftime("%Y-%m-%d"):
        print("今日无行情数据（休市），跳过推送")
        return

    if not wechat["template_id"]:
        print("[提醒] 未配置 REPORT.TEMPLATE_ID，跳过微信推送")
        return

    send_wechat(wechat, build_report_data(wechat["fields"], d))


if __name__ == "__main__":
    main()
