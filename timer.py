import argparse
import json
import os
import signal
import subprocess
import sys
import time

import schedule
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MONITOR = os.path.join(BASE_DIR, "monitor.py")

parser = argparse.ArgumentParser(description="读取配置并定时启动中证红利监控")
parser.add_argument('--cfg', type=str, default='config.yaml')
parser.add_argument('--once', action='store_true', help='跑一次并打印结果，不推送')
parser.add_argument('--test-send', action='store_true', help='发送一条测试微信消息')
args = parser.parse_args()


def build_env(config, trade):
    """把 config.yaml 的内容转成 monitor.py 可读的环境变量。"""
    env = {**os.environ}
    env['SYMBOL'] = str(trade.get('SYMBOL', 'sh515080'))
    env['NAME'] = str(trade.get('NAME', '中证红利ETF'))
    env['WINDOWS'] = json.dumps(trade.get('WINDOWS', [20, 60]))
    env['TOUCH_THRESHOLD'] = str(trade.get('TOUCH_THRESHOLD', 0.01))
    env['COOLDOWN_MINUTES'] = str(trade.get('COOLDOWN_MINUTES', 120))
    env['ROUND_STEP'] = str(trade.get('ROUND_STEP', 0))
    env['APP_ID'] = str(config.get('APP_ID', ''))
    env['APP_SECRET'] = str(config.get('APP_SECRET', ''))
    user_ids = config.get('USER_ID') or []
    env['USER_ID'] = '\n'.join(user_ids) if isinstance(user_ids, list) else str(user_ids)
    env['TEMPLATE_ID'] = str(trade.get('TEMPLATE_ID', ''))
    env['FIELDS'] = json.dumps(trade.get('FIELDS', {}))
    return env


def run_monitor(env, extra_args=None):
    subprocess.run([sys.executable, MONITOR] + (extra_args or []), env=env)


if __name__ == '__main__':
    with open(args.cfg, "r", encoding="utf-8") as fd:
        config = yaml.safe_load(fd)

    trade = config.get('TRADE_ALERT')
    if not trade:
        print("未配置 TRADE_ALERT，请检查 config.yaml")
        sys.exit(1)

    env = build_env(config, trade)

    # 单次测试模式：跑一次就退出
    if args.once:
        run_monitor(env, ['--once'])
        sys.exit(0)
    if args.test_send:
        run_monitor(env, ['--test-send'])
        sys.exit(0)

    # 常驻模式：每隔 N 分钟拉起一次 monitor.py（交易时段判断在 monitor 内部）
    interval = trade.get('CHECK_INTERVAL_MINUTES', 5)
    schedule.every(interval).minutes.do(run_monitor, env)
    print("中证红利监控已开启，每 %d 分钟检查一次（仅交易时段）" % interval)
    run_monitor(env)  # 启动即检查一次

    def stop(signum, frame):
        print("\n程序结束！")
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)

    while True:
        schedule.run_pending()
        time.sleep(50)
