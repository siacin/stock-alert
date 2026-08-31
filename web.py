from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from main import setup_logging
from stock_alert.config import load_config
from stock_alert.dashboard import DashboardController, DashboardHTTPServer
from stock_alert.remote_access import REMOTE_PORT


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A 股多源提醒可视化控制台")
    parser.add_argument("--config", default="config.json", help="JSON 配置文件")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认只允许本机访问")
    parser.add_argument("--port", type=int, default=8765, help="本地端口")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--auto-start", action="store_true", help="界面启动后自动开始盘中监控")
    parser.add_argument("--widget", action="store_true", help="同时启动迷你置顶盯盘窗")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    return parser.parse_args()


def find_existing(host: str, start_port: int) -> str | None:
    if host not in {"127.0.0.1", "localhost"}:
        return None
    for port in range(start_port, start_port + 10):
        url = f"http://{host}:{port}"
        try:
            with urllib.request.urlopen(f"{url}/api/ping", timeout=0.25) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if payload.get("app") == "stock-alert-dashboard":
                    return url
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return None


def launch_widget(url: str) -> None:
    widget_path = Path(__file__).resolve().parent / "widget.py"
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [sys.executable, str(widget_path), "--url", url],
            cwd=widget_path.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except OSError as exc:
        logging.getLogger(__name__).warning("悬浮盯盘窗启动失败：%s", exc)


def main() -> int:
    args = parse_args()
    if args.host not in {"127.0.0.1", "localhost"} or args.port == REMOTE_PORT:
        print("仅允许本机监听；8766 为安全远程专用端口。请用 remote.cmd 连接手机。", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
    except (OSError, ValueError, TypeError) as exc:
        print(f"配置加载失败: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_path, args.debug)

    existing = find_existing(args.host, args.port)
    if existing:
        logging.getLogger(__name__).info("控制台已在运行：%s", existing)
        if args.auto_start:
            try:
                request = urllib.request.Request(f"{existing}/api/monitor/start", data=b"", method="POST")
                urllib.request.urlopen(request, timeout=3).close()
            except (OSError, urllib.error.URLError):
                logging.getLogger(__name__).warning("已有控制台未能自动启动监控，请在页面中点击启动")
        if args.widget:
            launch_widget(existing)
        if not args.no_browser:
            webbrowser.open(existing)
        return 0

    controller = DashboardController(Path(args.config))
    static_dir = Path(__file__).resolve().parent / "web"
    server = None
    selected_port = args.port
    for selected_port in range(args.port, args.port + 10):
        if selected_port == REMOTE_PORT:
            continue
        try:
            server = DashboardHTTPServer((args.host, selected_port), controller, static_dir)
            break
        except OSError:
            continue
    if server is None:
        print(f"无法启动：端口 {args.port}–{args.port + 9} 均被占用", file=sys.stderr)
        return 4

    url = f"http://{args.host}:{selected_port}"
    remote_server = None
    try:
        remote_server = DashboardHTTPServer(("127.0.0.1", REMOTE_PORT), controller, static_dir, remote=True)
        controller.remote_port = REMOTE_PORT
        threading.Thread(target=remote_server.serve_forever, name="private-remote-http", daemon=True).start()
    except OSError:
        logging.getLogger(__name__).warning("安全远程端口 %s 被占用，远程不可用；本机控制台不受影响", REMOTE_PORT)
    logging.getLogger(__name__).info("可视化控制台：%s", url)
    if args.auto_start:
        controller.start()
    if args.widget:
        launch_widget(url)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("控制台退出")
    finally:
        if remote_server:
            remote_server.shutdown()
            remote_server.server_close()
        controller.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
