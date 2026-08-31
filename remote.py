"""Interactive, fail-closed setup for a private Tailscale Serve endpoint."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from stock_alert.remote_access import REMOTE_PORT, RemoteAccessPolicy

TARGET = f"http://127.0.0.1:{REMOTE_PORT}"


def tailscale_binary() -> str:
    installed = Path("C:/Program Files/Tailscale/tailscale.exe")
    binary = str(installed) if installed.exists() else shutil.which("tailscale")
    if not binary:
        raise RuntimeError("请先从 https://tailscale.com/download/windows 安装官方 Tailscale。")
    return binary


def run_cli(binary: str, *args: str) -> dict:
    result = subprocess.run([binary, *args], capture_output=True, text=True, encoding="utf-8", timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "无法连接 Tailscale 服务")
    return json.loads(result.stdout or "{}")


def owned_serve(config: dict, hostname: str) -> bool:
    """Only our exact HTTPS root is considered safe to replace/remove."""
    endpoint = f"{hostname}:443"
    if config.get("Services") or config.get("Foreground"):
        return False
    if any(config.get("AllowFunnel", {}).values()):
        return False
    if set(config.get("TCP", {})) != {"443"} or config["TCP"]["443"] != {"HTTPS": True}:
        return False
    return config.get("Web") == {endpoint: {"Handlers": {"/": {"Proxy": TARGET}}}}


def empty_serve(config: dict) -> bool:
    return not any(config.get(key) for key in ("TCP", "Web", "AllowFunnel", "Services", "Foreground"))


def local_dashboard(start_port: int) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for port in range(start_port, start_port + 10):
        if port == REMOTE_PORT:
            continue
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/access", timeout=1) as response:
                access = json.load(response)
                if access.get("remote") is False and access.get("remote_port") == REMOTE_PORT:
                    return access
        except (OSError, ValueError, urllib.error.URLError):
            pass
    raise RuntimeError("未找到新版控制台。请先双击 start.cmd；旧版服务需关闭后重新启动。")


def main() -> int:
    parser = argparse.ArgumentParser(description="手机安全连接向导（仅 Tailscale 私有 HTTPS，无公网入口）")
    parser.add_argument("action", nargs="?", choices=["enable", "disable", "status"], default="enable")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    policy = RemoteAccessPolicy(args.config)
    try:
        if args.action == "disable":
            old = policy.read()
            policy.write({"enabled": False})
            print("软件已拒绝所有远程请求，本机使用不受影响。")
            binary = tailscale_binary()
            config = run_cli(binary, "serve", "status", "--json")
            hostname = old.get("origin", "").removeprefix("https://")
            if not hostname:
                status = run_cli(binary, "status", "--json")
                hostname = str((status.get("Self") or {}).get("DNSName", "")).rstrip(".")
            if hostname and owned_serve(config, hostname):
                subprocess.run([binary, "serve", "--bg", "--https=443", "off"], check=True, timeout=20)
            return 0

        binary = tailscale_binary()
        status = run_cli(binary, "status", "--json")
        if args.action == "status":
            print("Tailscale:", status.get("BackendState", "Unknown"))
            print("软件远程授权:", "已启用" if policy.read()["enabled"] else "未启用")
            print("私有地址:", policy.read().get("origin", "尚未生成"))
            return 0

        local_dashboard(args.port)
        if status.get("BackendState") != "Running":
            print("请在 Tailscale 官方登录页面完成登录。手机必须使用同一个账号。", flush=True)
            subprocess.run([binary, "login", "--timeout=3m", "--accept-routes=false"], check=True, timeout=185)
            status = run_cli(binary, "status", "--json")
        own = status.get("Self") or {}
        hostname = str(own.get("DNSName", "")).rstrip(".")
        user = (status.get("User") or {}).get(str(own.get("UserID")), {})
        login = user.get("LoginName", "")
        if status.get("BackendState") != "Running" or not hostname.endswith(".ts.net") or not login or own.get("Tags"):
            raise RuntimeError("需要正常登录的个人设备及 MagicDNS 域名，不支持带标签的设备账号。")
        config = run_cli(binary, "serve", "status", "--json")
        if not empty_serve(config) and not owned_serve(config, hostname):
            raise RuntimeError("检测到其他 Serve/Funnel 配置，未进行覆盖。请先检查已有配置并为本软件预留独立入口。")
        # Keep disabled until HTTPS proxy setup has completed and its configuration is verified.
        policy.write({"enabled": False})
        print("正在配置私有 HTTPS。若提示启用 HTTPS，请按官方链接确认后重新运行本向导。", flush=True)
        print("HTTPS 证书中的设备域名会进入公开证书透明度日志；业务页面本身不会开放公网。", flush=True)
        subprocess.run([binary, "serve", "--bg", "--https=443", TARGET], check=True, timeout=45)
        if not owned_serve(run_cli(binary, "serve", "status", "--json"), hostname):
            raise RuntimeError("代理配置校验未通过，远程授权保持关闭。")
        policy.write({"enabled": True, "origin": f"https://{hostname}", "owner_login": login})
        if not policy.read()["enabled"]:
            raise RuntimeError("远程账号或域名校验失败，授权未启用。")
        print("\n连接已配置。仅允许以下账号的设备访问:", login)
        print("手机安装 Tailscale、登录同一账号并连接后，用浏览器打开:")
        print(f"https://{hostname}/")
        print("电脑和本软件必须保持运行；手机网页不保证锁屏/后台推送。")
        return 0
    except subprocess.TimeoutExpired:
        print("等待登录或 HTTPS 确认超时。请完成上方官方页面操作，再运行 remote.cmd；远程授权未开启。", file=sys.stderr)
        return 1
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"连接向导未完成: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
