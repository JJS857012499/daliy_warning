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
REPORT = os.path.join(BASE_DIR, "report.py")

parser = argparse.ArgumentParser(description="读取配置并定时启动中证红利监控")
parser.add_argument('--cfg', type=str, default='config.yaml')
parser.add_argument('--once', action='store_true', help='跑一次支撑/压力检查并打印，不推送')
parser.add_argument('--test-send', action='store_true', help='发送一条监控测试微信消息')
parser.add_argument('--report', action='store_true', help='跑一次波段日报并打印，不推送')
parser.add_argument('--report-test', action='store_true', help='发送一条日报测试微信消息')
parser.add_argument('--report-week', action='store_true', help='跑一次周线报告并打印，不推送')
args = parser.parse_args()


def build_env(config, trade):
    """把 config.yaml 的内容转成脚本可读的环境变量。"""
    env = {**os.environ}
    env['SYMBOL'] = str(trade.get('SYMBOL', 'sh515080'))
    env['NAME'] = str(trade.get('NAME', '中证红利ETF'))
    env['WINDOWS'] = json.dumps(trade.get('WINDOWS', [20, 60]))
    env['TOUCH_THRESHOLD'] = str(trade.get('TOUCH_THRESHOLD', 0.01))
    env['COOLDOWN_MINUTES'] = str(trade.get('COOLDOWN_MINUTES', 120))
    env['ROUND_STEP'] = str(trade.get('ROUND_STEP', 0.1))
    env['APP_ID'] = str(config.get('APP_ID', ''))
    env['APP_SECRET'] = str(config.get('APP_SECRET', ''))
    user_ids = config.get('USER_ID') or []
    env['USER_ID'] = '\n'.join(user_ids) if isinstance(user_ids, list) else str(user_ids)
    env['TEMPLATE_ID'] = str(trade.get('TEMPLATE_ID', ''))
    env['FIELDS'] = json.dumps(trade.get('FIELDS', {}))
    report = config.get('REPORT') or {}
    env['REPORT_TEMPLATE_ID'] = str(report.get('TEMPLATE_ID', ''))
    env['REPORT_FIELDS'] = json.dumps(report.get('FIELDS', {}))
    return env


def run_script(path, env, extra_args=None):
    subprocess.run([sys.executable, path] + (extra_args or []), env=env)


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
        run_script(MONITOR, env, ['--once'])
        sys.exit(0)
    if args.test_send:
        run_script(MONITOR, env, ['--test-send'])
        sys.exit(0)
    if args.report:
        run_script(REPORT, env, ['--once'])
        sys.exit(0)
    if args.report_test:
        run_script(REPORT, env, ['--test-send'])
        sys.exit(0)
    if args.report_week:
        run_script(REPORT, env, ['--week', '--once'])
        sys.exit(0)

    # 常驻模式
    interval = trade.get('CHECK_INTERVAL_MINUTES', 5)
    schedule.every(interval).minutes.do(run_script, MONITOR, env)
    report = config.get('REPORT') or {}
    if report.get('TEMPLATE_ID'):
        send_time = report.get('SEND_TIME', '15:40')
        schedule.every().day.at(send_time).do(run_script, REPORT, env)                  # 日报
        schedule.every().friday.at(send_time).do(run_script, REPORT, env, ['--week'])   # 周报
        print("波段日报已开启，每日 %s 推送；周报每周五 %s 推送" % (send_time, send_time))
    print("中证红利监控已开启，每 %d 分钟检查一次（仅交易时段）" % interval)
    run_script(MONITOR, env)  # 启动即检查一次

    def stop(signum, frame):
        print("\n程序结束！")
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)

    while True:
        schedule.run_pending()
        time.sleep(50)
