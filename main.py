from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from stock_alert.app import AlertApplication
from stock_alert.config import load_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A 股自选股多数据源实时提醒器")
    parser.add_argument("--config", default="config.json", help="JSON 配置文件，默认 config.json")
    parser.add_argument("--once", action="store_true", help="只请求一次，用于检查数据源和配置")
    parser.add_argument("--ignore-market-hours", action="store_true", help="忽略交易时段限制（调试用）")
    parser.add_argument("--print-quotes", action="store_true", help="每轮打印多源聚合行情")
    parser.add_argument("--no-notify", action="store_true", help="只记录事件，不发提示音和 Webhook")
    parser.add_argument("--debug", action="store_true", help="输出 DEBUG 日志")
    parser.add_argument("--validate-config", action="store_true", help="只校验配置，不访问行情")
    return parser.parse_args()


def setup_logging(log_path: Path, debug: bool) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if debug else logging.INFO
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[console, file_handler], force=True)


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
    except (OSError, ValueError, TypeError) as exc:
        print(f"配置加载失败: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_path, args.debug)
    if args.validate_config:
        print(f"配置有效：{len(config.stocks)} 只股票，数据源={','.join(config.providers)}")
        return 0

    app = AlertApplication(config, notifications_disabled=args.no_notify)
    try:
        if args.once:
            ok = app.run_once(enforce_freshness=False, print_quotes=True)
            return 0 if ok else 3
        app.run_forever(ignore_market_hours=args.ignore_market_hours, print_quotes=args.print_quotes)
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("收到 Ctrl+C，提醒器退出")
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
