# -*- coding: utf-8 -*-
"""中证红利ETF（515080）支撑/压力位监控。

- 接近支撑位 -> 提醒加仓
- 接近压力位 -> 提醒减仓

数据源：腾讯行情接口（日K线，含当日实时 bar），复用项目已有 requests，无需新增依赖。
main() 从系统环境变量读取参数，可独立运行；config.yaml 的读取与转环境变量由 timer.py 负责。
"""

import argparse
import datetime as dt
import json
import math
import os
import sys
import time

import requests
from wechatpy import WeChatClient, WeChatClientException
from wechatpy.client.api import WeChatMessage

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None


def local_now():
    """当前时间，默认北京时间（可用 MONITOR_TZ 环境变量覆盖）。"""
    tz_name = os.getenv('MONITOR_TZ', 'Asia/Shanghai')
    if ZoneInfo is not None:
        try:
            return dt.datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return dt.datetime.now()


STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_state.json")


def load_state():
    """读取上次的提醒状态（跨进程去重冷却用）。"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_signal": None, "last_alert_ts": 0}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


def fetch_kline(symbol, count):
    """拉取日K线，返回 (dates, closes, highs, lows, current)。"""
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={symbol},day,,,{count},qfq"
    )
    res = requests.get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    payload = res.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"接口返回异常: {payload}")
    node = payload["data"][symbol]
    rows = []
    if isinstance(node, dict):
        for key in ("qfqday", "day", "hfqday"):
            if key in node:
                rows = node[key]
                break
    else:
        rows = node
    if not rows:
        raise RuntimeError("接口未返回K线数据")
    # 每行格式：[日期, 开盘, 收盘, 最高, 最低, 成交量]
    dates, closes, highs, lows = [], [], [], []
    for r in rows:
        dates.append(r[0])
        closes.append(float(r[2]))
        highs.append(float(r[3]))
        lows.append(float(r[4]))
    # 当前价：优先取 qt 节点里的实时价（精确到 0.001），缺失时退回最后一根K线收盘价
    current = closes[-1]
    if isinstance(node, dict):
        qt = node.get("qt", {}).get(symbol)
        if isinstance(qt, list) and len(qt) > 3:
            try:
                current = float(qt[3])
            except (TypeError, ValueError):
                pass
    return dates, closes, highs, lows, current


def auto_round_step(price):
    """按价格量级自动取整数关口间隔：1.57->0.1，15.7->1，157->10。"""
    if price <= 0:
        return 0.01
    return 10 ** math.floor(math.log10(price)) / 10


def round_levels(low, high, step):
    """在 [low, high] 区间内生成 step 的整数倍关口。"""
    levels = []
    k = math.ceil(low / step - 1e-9) * step
    while k <= high + 1e-9:
        levels.append(round(k, 6))
        k += step
    return levels


def compute_levels(closes, highs, lows, windows, round_step=0):
    """计算支撑/压力位。

    支撑候选：近 N 日最低价 + 位于现价下方的 MA + 整数关口
    压力候选：近 N 日最高价 + 位于现价上方的 MA + 整数关口
    有效支撑 = 现价下方最近的一档；有效压力 = 现价上方最近的一档。
    """
    current = closes[-1]
    supports, resistances = [], []
    n = len(closes)
    for w in windows:
        if n < w:
            continue
        supports.append(("近%d日低点" % w, min(lows[-w:])))
        resistances.append(("近%d日高点" % w, max(highs[-w:])))
        ma = sum(closes[-w:]) / w
        if ma <= current:
            supports.append(("MA%d" % w, ma))
        else:
            resistances.append(("MA%d" % w, ma))

    if round_step <= 0:
        round_step = auto_round_step(current)
    for lv in round_levels(min(lows), max(highs), round_step):
        if lv < current:
            supports.append(("整数关口", lv))
        elif lv > current:
            resistances.append(("整数关口", lv))

    below = [s for s in supports if s[1] <= current]
    above = [r for r in resistances if r[1] >= current]
    support = max(below, key=lambda x: x[1]) if below else None
    resistance = min(above, key=lambda x: x[1]) if above else None
    return current, support, resistance, supports, resistances


def judge_signal(current, support, resistance, threshold):
    """返回 (信号, 触发位, 距离) 或 (None, None, None)。"""
    candidates = []
    if support is not None:
        d = (current - support[1]) / support[1]
        if d <= threshold:
            candidates.append(("加仓", support, d))
    if resistance is not None:
        d = (resistance[1] - current) / resistance[1]
        if d <= threshold:
            candidates.append(("减仓", resistance, d))
    if not candidates:
        return None, None, None
    # 同时贴近两侧时，取距离更近的一侧
    return min(candidates, key=lambda c: c[2])


def build_advice(signal, dist):
    if signal == "加仓":
        if dist < 0:
            return "现价已跌破支撑位，可关注分批加仓机会。"
        return "现价已接近支撑位，可考虑分批加仓。"
    return "现价已接近压力位，可考虑减仓或止盈。"


def build_template_data(fields, index_name, price_text, signal, levels_text, advice, remark):
    color = "#173177"
    return {
        fields["title"]: {"value": "%s 行情提醒" % index_name, "color": color},
        fields["index_name"]: {"value": index_name, "color": color},
        fields["price"]: {"value": price_text, "color": color},
        fields["signal"]: {"value": signal, "color": color},
        fields["levels"]: {"value": levels_text, "color": color},
        fields["advice"]: {"value": advice, "color": color},
        fields["remark"]: {"value": remark, "color": color},
    }


def send_wechat(wechat, data):
    """wechat: {app_id, app_secret, user_ids, template_id, fields}"""
    print("推送信息 %s" % data)
    template_id = wechat["template_id"]
    if not template_id:
        print("[提醒] 未配置 TRADE_ALERT.TEMPLATE_ID，跳过微信推送")
        return
    try:
        client = WeChatClient(wechat["app_id"], wechat["app_secret"])
        wm = WeChatMessage(client)
    except WeChatClientException as e:
        print("[提醒] 微信获取 token 失败，请检查 APP_ID/APP_SECRET：%s" % e)
        return
    for uid in wechat["user_ids"]:
        try:
            wm.send_template(uid, template_id, data)
            print("[提醒] 已推送给 %s" % uid)
        except WeChatClientException as e:
            print("[提醒] 推送失败 %s：%s" % (uid, e))


def evaluate(symbol, windows, threshold, round_step=0):
    """拉取数据并计算，返回结果 dict。"""
    count = max(windows) + 20
    dates, closes, highs, lows, current = fetch_kline(symbol, count)
    # 用实时价修正最后一根K线收盘，保证均线/关口判断用的是同一现价
    closes[-1] = current
    current, support, resistance, supports, resistances = compute_levels(
        closes, highs, lows, windows, round_step
    )
    signal, level, dist = judge_signal(current, support, resistance, threshold)
    return {
        "latest_date": dates[-1],
        "current": current,
        "support": support,
        "resistance": resistance,
        "all_supports": supports,
        "all_resistances": resistances,
        "signal": signal,
        "level": level,
        "dist": dist,
    }


def send_alert(name, wechat, r):
    sup, res = r["support"], r["resistance"]
    if sup and res:
        levels_text = "支撑 %.3f / 压力 %.3f" % (sup[1], res[1])
    elif sup:
        levels_text = "支撑 %.3f" % sup[1]
    else:
        levels_text = "压力 %.3f" % res[1]
    signal = r["signal"]
    dist = r["dist"]
    level = r["level"]
    data = build_template_data(
        wechat["fields"],
        name,
        "%.3f" % r["current"],
        signal,
        levels_text,
        build_advice(signal, dist),
        "触发位 %s %.3f（距 %.3f%%）" % (level[0], level[1], dist * 100),
    )
    send_wechat(wechat, data)


def print_result(name, symbol, r):
    print("=" * 44)
    print("指数: %s (%s)" % (name, symbol))
    print("最新交易日: %s" % r["latest_date"])
    print("现价: %.3f" % r["current"])
    print("支撑位候选: " + ", ".join("%s %.3f" % (n, v) for n, v in r["all_supports"]))
    print("压力位候选: " + ", ".join("%s %.3f" % (n, v) for n, v in r["all_resistances"]))
    sup, res = r["support"], r["resistance"]
    print("有效支撑: " + ("%s %.3f" % (sup[0], sup[1]) if sup else "无"))
    print("有效压力: " + ("%s %.3f" % (res[0], res[1]) if res else "无"))
    if r["signal"]:
        print("信号: %s（触发位 %s %.3f，距离 %.3f%%）"
              % (r["signal"], r["level"][0], r["level"][1], r["dist"] * 100))
        print("建议: %s" % build_advice(r["signal"], r["dist"]))
    else:
        print("信号: 无（未触及支撑/压力位）")
    print("=" * 44)


def is_trading_time(latest_date):
    now = local_now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    am = dt.time(9, 30) <= t <= dt.time(11, 30)
    pm = dt.time(13, 0) <= t <= dt.time(15, 0)
    if not (am or pm):
        return False
    # 最后一根K线不是今天，说明今日无行情（周末/节假日），跳过
    return latest_date == now.strftime("%Y-%m-%d")


def handle_signal(name, cooldown_minutes, wechat, state, r):
    now_ts = time.time()
    cooldown = cooldown_minutes * 60
    signal = r["signal"]
    if signal != state["last_signal"] or (now_ts - state["last_alert_ts"] >= cooldown):
        state["last_signal"] = signal
        state["last_alert_ts"] = now_ts
        send_alert(name, wechat, r)
    else:
        print("[%s] 信号 %s 处于冷却期内，不重复推送"
              % (local_now().strftime("%H:%M:%S"), signal))


def tick(symbol, name, windows, threshold, cooldown_minutes, round_step, wechat, state):
    try:
        r = evaluate(symbol, windows, threshold, round_step)
    except Exception as e:
        print("[%s] 数据获取失败: %s" % (local_now().strftime("%H:%M:%S"), e))
        return
    if not is_trading_time(r["latest_date"]):
        print("[%s] 非交易时段或今日无数据，跳过"
              % local_now().strftime("%H:%M:%S"))
        return
    print("[%s] %s 现价 %.3f，信号: %s"
          % (local_now().strftime("%H:%M:%S"),
             name, r["current"], r["signal"] or "无"))
    if r["signal"]:
        handle_signal(name, cooldown_minutes, wechat, state, r)
    else:
        state["last_signal"] = None


def send_test(name, wechat):
    data = build_template_data(
        wechat["fields"], name, "0.00", "测试",
        "支撑 - / 压力 -", "这是一条测试消息，用于验证模板配置。",
        "监控脚本测试",
    )
    send_wechat(wechat, data)


def main():
    parser = argparse.ArgumentParser(description="中证红利支撑/压力位监控")
    parser.add_argument('--once', action='store_true', help='跑一次并打印结果，不推送')
    parser.add_argument('--test-send', action='store_true', help='发送一条测试微信消息')
    args = parser.parse_args()

    # 参数从系统环境变量读取
    symbol = os.getenv('SYMBOL', 'sh515080')
    name = os.getenv('NAME', '中证红利ETF')
    windows = json.loads(os.getenv('WINDOWS', '[20, 60]'))
    threshold = float(os.getenv('TOUCH_THRESHOLD', '0.01'))
    cooldown = int(os.getenv('COOLDOWN_MINUTES', '120'))
    round_step = float(os.getenv('ROUND_STEP', '0'))
    wechat = {
        'app_id': os.getenv('APP_ID', ''),
        'app_secret': os.getenv('APP_SECRET', ''),
        'user_ids': [u for u in os.getenv('USER_ID', '').split('\n') if u],
        'template_id': os.getenv('TEMPLATE_ID', ''),
        'fields': json.loads(os.getenv('FIELDS', '{}')),
    }

    if args.test_send:
        send_test(name, wechat)
        return

    if args.once:
        try:
            print_result(name, symbol, evaluate(symbol, windows, threshold, round_step))
        except Exception as e:
            print("数据获取失败: %s" % e)
            sys.exit(1)
        return

    # 单次检查：读上次状态 -> 检查 -> 写回（定时调度由 timer.py 负责）
    state = load_state()
    try:
        tick(symbol, name, windows, threshold, cooldown, round_step, wechat, state)
    except KeyboardInterrupt:
        pass
    save_state(state)


if __name__ == "__main__":
    main()
