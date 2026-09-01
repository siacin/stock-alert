from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .analysis import TechnicalAnalysisService
from .app import AlertApplication, is_market_open, market_phase, seconds_until_next_session
from .config import load_config
from .detector import EVENT_LABELS
from .models import normalize_code
from .market_monitor import MarketMonitorError, MarketMonitorService
from .news_agent import NewsAgentError, NewsAgentService
from .news_radar import NewsRadarError, NewsRadarService, normalize_settings
from .remote_access import RemoteAccessPolicy, merge_remote_config, remote_config_view
from .trend import IntradayTrendService


TZ = ZoneInfo("Asia/Shanghai")
LOGGER = logging.getLogger(__name__)
SOURCE_LABELS = {"tencent": "腾讯", "eastmoney": "东方财富", "sina": "新浪"}


class DashboardError(RuntimeError):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = int(status)


class DashboardController:
    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path).resolve()
        self._lock = threading.RLock()
        self._config_lock = threading.RLock()
        self.remote_policy = RemoteAccessPolicy(self.config_path)
        self.remote_port: int | None = None
        self._agent_result_lock = threading.Lock()
        self._agent_run_lock = threading.Lock()
        self._agent_result_path = self.config_path.parent / "data" / "news-agent-result.json"
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._application: AlertApplication | None = None
        self._snapshot: dict[str, Any] = {
            "updated_at": None,
            "ok": None,
            "message": "点击“刷新行情”获取最新数据",
            "sources": [],
            "quotes": [],
            "events": [],
        }
        self._state = "stopped"
        self._message = "监控已停止"
        self._last_error: str | None = None
        self.trend_service = IntradayTrendService()
        self.analysis_service = TechnicalAnalysisService()
        self.news_radar_service = NewsRadarService(self.config_path)
        self.news_agent_service = NewsAgentService(self.config_path)
        self.market_monitor = MarketMonitorService(
            self.config_path, hotlist_provider=self.news_radar_service.cached_hot_stocks)

    def get_config(self) -> dict[str, Any]:
        try:
            with self._config_lock:
                config = json.loads(self.config_path.read_text(encoding="utf-8"))
                config.pop("_revision", None)
                config["_revision"] = hashlib.sha256(
                    json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")
                ).hexdigest()
                return config
        except (OSError, json.JSONDecodeError) as exc:
            raise DashboardError(f"配置读取失败：{exc}") from exc

    def save_config(self, payload: Any, *, remote: bool = False, require_revision: bool = False) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise DashboardError("配置必须是 JSON 对象")
        with self._config_lock:
            current = self.get_config()
            self._check_revision(payload, current, require_revision)
            if remote:
                try:
                    payload = merge_remote_config(current, payload)
                except ValueError as exc:
                    raise DashboardError(str(exc), HTTPStatus.FORBIDDEN) from exc
            with self._lock:
                if self._worker and self._worker.is_alive():
                    raise DashboardError("请先停止监控，再保存配置；保存后重新启动即生效", HTTPStatus.CONFLICT)
                saved = self._write_config(payload)
            self.news_radar_service.invalidate()
            return remote_config_view(saved) if remote else saved

    @staticmethod
    def _check_revision(payload: dict, current: dict, required: bool) -> None:
        if (required or "_revision" in payload) and payload.get("_revision") != current["_revision"]:
            raise DashboardError("配置已在其他设备更新。请先载入最新配置，再重新编辑，避免覆盖。", HTTPStatus.CONFLICT)

    def _write_config(self, payload: dict) -> dict[str, Any]:
        payload = {key: value for key, value in payload.items() if key != "_revision"}
        pending_path = self.config_path.with_suffix(self.config_path.suffix + ".pending")
        try:
            if "news_radar" in payload:
                payload["news_radar"] = normalize_settings(payload["news_radar"])
            pending_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            load_config(pending_path)
            pending_path.replace(self.config_path)
        except (OSError, ValueError, TypeError, NewsRadarError) as exc:
            try:
                pending_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise DashboardError(f"配置校验失败：{exc}") from exc
        return self.get_config()

    def _set_runtime(self, state: str, message: str, error: str | None = None) -> None:
        with self._lock:
            self._state = state
            self._message = message
            self._last_error = error

    def _copy_application_snapshot(self, app: AlertApplication) -> None:
        snapshot = app.get_snapshot()
        with self._lock:
            self._snapshot = snapshot

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return self.status()
            try:
                config = load_config(self.config_path)
            except (OSError, ValueError, TypeError) as exc:
                raise DashboardError(f"配置加载失败：{exc}") from exc
            self._stop_event = threading.Event()
            self._state = "starting"
            self._message = "正在启动监控"
            self._last_error = None
            self._worker = threading.Thread(
                target=self._monitor_loop,
                args=(config, self._stop_event),
                name="stock-alert-monitor",
                daemon=True,
            )
            self._worker.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            worker = self._worker
            if not worker or not worker.is_alive():
                self._state = "stopped"
                self._message = "监控已停止"
                return self.status()
            self._state = "stopping"
            self._message = "正在停止监控"
            self._stop_event.set()
        worker.join(timeout=5.0)
        return self.status()

    def refresh_once(self) -> dict[str, Any]:
        with self._lock:
            if self._worker and self._worker.is_alive():
                raise DashboardError("持续监控已在运行，无需手动刷新", HTTPStatus.CONFLICT)
            try:
                config = load_config(self.config_path)
            except (OSError, ValueError, TypeError) as exc:
                raise DashboardError(f"配置加载失败：{exc}") from exc
            self._stop_event = threading.Event()
            self._state = "refreshing"
            self._message = "正在请求三路行情"
            self._last_error = None
            self._worker = threading.Thread(
                target=self._refresh_worker,
                args=(config,),
                name="stock-alert-refresh",
                daemon=True,
            )
            self._worker.start()
        return self.status()

    def _refresh_worker(self, config) -> None:
        app: AlertApplication | None = None
        try:
            app = AlertApplication(config, notifications_disabled=True)
            with self._lock:
                self._application = app
            ok = app.run_once(enforce_freshness=False)
            self._copy_application_snapshot(app)
            self._set_runtime("stopped", "行情刷新完成" if ok else "行情刷新失败")
        except Exception as exc:  # noqa: BLE001 - surfaced to the local dashboard
            LOGGER.exception("手动刷新失败")
            self._set_runtime("stopped", "行情刷新失败", str(exc))
        finally:
            if app:
                app.close()
            with self._lock:
                self._application = None

    def _monitor_loop(self, config, stop_event: threading.Event) -> None:
        app: AlertApplication | None = None
        try:
            app = AlertApplication(config)
            with self._lock:
                self._application = app
            while not stop_event.is_set():
                now = datetime.now(TZ)
                if not is_market_open(now, config):
                    wait = min(seconds_until_next_session(now, config), 60.0)
                    self._set_runtime("waiting", "当前休市，已进入等待模式")
                    stop_event.wait(wait)
                    continue

                self._set_runtime("running", "盘中实时监控中")
                started = time.monotonic()
                app.run_once(enforce_freshness=True)
                self._copy_application_snapshot(app)
                elapsed = time.monotonic() - started
                stop_event.wait(max(0.1, config.poll_interval_seconds - elapsed))
        except Exception as exc:  # noqa: BLE001 - background failures must be visible in UI
            LOGGER.exception("监控线程退出")
            self._set_runtime("error", "监控异常停止", str(exc))
        finally:
            if app:
                self._copy_application_snapshot(app)
                app.close()
            with self._lock:
                self._application = None
                if self._state != "error":
                    self._state = "stopped"
                    self._message = "监控已停止"

    def status(self) -> dict[str, Any]:
        with self._lock:
            app = self._application
            state = self._state
            message = self._message
            error = self._last_error
            snapshot = dict(self._snapshot)
        if app:
            live_snapshot = app.get_snapshot()
            if live_snapshot.get("updated_at"):
                snapshot = live_snapshot
        try:
            config = load_config(self.config_path)
            now = datetime.now(TZ)
            market_open = is_market_open(now, config)
            phase = market_phase(now, config)
            stock_count = len(config.stocks)
            poll_interval = config.poll_interval_seconds
        except (OSError, ValueError, TypeError):
            now = datetime.now(TZ)
            market_open = False
            phase = "closed"
            stock_count = 0
            poll_interval = None
        return {
            "app": "stock-alert-dashboard",
            "state": state,
            "message": message,
            "error": error,
            "market_open": market_open,
            "market_phase": phase,
            "server_time": now.isoformat(),
            "stock_count": stock_count,
            "poll_interval_seconds": poll_interval,
            "snapshot": snapshot,
        }

    def technical_analysis(self, raw_code: str) -> dict[str, Any]:
        try:
            code = normalize_code(raw_code)
            config_payload = self.get_config()
        except (DashboardError, ValueError, TypeError) as exc:
            raise DashboardError(str(exc)) from exc
        watch = next((item for item in config_payload.get("stocks", []) if normalize_code(item.get("code", "")) == code), None)
        if watch is None:
            raise DashboardError("只能分析当前自选列表中的股票", HTTPStatus.NOT_FOUND)
        with self._lock:
            snapshot = dict(self._snapshot)
            app = self._application
        if app:
            live = app.get_snapshot()
            if live.get("updated_at"):
                snapshot = live
        quote = next((item for item in snapshot.get("quotes", []) if item.get("code") == code), None)
        try:
            result = self.analysis_service.get(code, quote)
        except RuntimeError as exc:
            raise DashboardError(f"技术分析暂不可用：{exc}", HTTPStatus.BAD_GATEWAY) from exc
        if result.get("name") == code and watch.get("name"):
            result["name"] = watch["name"]
        return result

    def news_radar(self, force: bool = False) -> dict[str, Any]:
        try:
            with self._lock:
                snapshot = dict(self._snapshot)
                app = self._application
            if app:
                live = app.get_snapshot()
                if live.get("updated_at"):
                    snapshot = live
            return self.news_radar_service.fetch(
                force=force,
                watch_quotes=snapshot.get("quotes", []),
            )
        except NewsRadarError as exc:
            raise DashboardError(f"资讯雷达暂不可用：{exc}", HTTPStatus.BAD_GATEWAY) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise DashboardError(f"资讯雷达配置无效：{exc}") from exc

    def news_radar_settings(self) -> dict[str, Any]:
        try:
            with self._config_lock:
                return {
                    "settings": self.news_radar_service.settings(),
                    "catalog": self.news_radar_service.catalog(),
                    "_revision": self.get_config()["_revision"],
                }
        except NewsRadarError as exc:
            raise DashboardError(str(exc)) from exc

    def save_news_radar_settings(self, payload: Any, *, require_revision: bool = False) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise DashboardError("资讯设置必须是 JSON 对象")
        try:
            settings = normalize_settings(payload)
        except NewsRadarError as exc:
            raise DashboardError(str(exc)) from exc
        with self._config_lock:
            config_payload = self.get_config()
            self._check_revision(payload, config_payload, require_revision)
            config_payload["news_radar"] = settings
            saved = self._write_config(config_payload)
        self.news_radar_service.invalidate()
        return {"settings": settings, "catalog": self.news_radar_service.catalog(), "_revision": saved["_revision"]}

    def news_agent_settings(self) -> dict[str, Any]:
        try:
            return {"settings": self.news_agent_service.settings()}
        except NewsAgentError as exc:
            raise DashboardError(str(exc)) from exc

    def save_news_agent_settings(self, payload: Any) -> dict[str, Any]:
        try:
            return {"settings": self.news_agent_service.save_settings(payload)}
        except NewsAgentError as exc:
            raise DashboardError(str(exc)) from exc

    def news_agent_analyze(self, payload: Any) -> dict[str, Any]:
        if not self._agent_run_lock.acquire(blocking=False):
            raise DashboardError("另一台设备正在分析，请稍后查看共享结果", HTTPStatus.CONFLICT)
        try:
            include_radar = not (isinstance(payload, dict) and payload.get("include_radar") is False)
            radar_payload = self.news_radar(force=False) if include_radar else {"items": []}
            watchlist = self.get_config().get("stocks", []) if include_radar else []
            market_snapshot = self.market_monitor.status().get("snapshot") if include_radar else None
            result = self.news_agent_service.analyze(
                payload, radar_payload, watchlist, market_snapshot)
            result["result_id"] = str(time.time_ns())
            result["saved_at"] = datetime.now(TZ).isoformat()
            with self._agent_result_lock:
                self._agent_result_path.parent.mkdir(parents=True, exist_ok=True)
                pending = self._agent_result_path.with_suffix(".pending")
                pending.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                pending.replace(self._agent_result_path)
            return result
        except NewsAgentError as exc:
            raise DashboardError(str(exc), HTTPStatus.BAD_GATEWAY) from exc
        except OSError as exc:
            raise DashboardError("分析已完成，但结果无法保存，请检查磁盘空间") from exc
        finally:
            self._agent_run_lock.release()

    def news_agent_test(self, payload: Any) -> dict[str, Any]:
        if not self._agent_run_lock.acquire(blocking=False):
            raise DashboardError("另一个 Agent 请求正在运行，请稍后测试", HTTPStatus.CONFLICT)
        try:
            return self.news_agent_service.test_connection(payload)
        except NewsAgentError as exc:
            raise DashboardError(str(exc), HTTPStatus.BAD_GATEWAY) from exc
        finally:
            self._agent_run_lock.release()

    def news_agent_result(self) -> dict[str, Any]:
        with self._agent_result_lock:
            try:
                result = json.loads(self._agent_result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result = None
        return {"result": result}

    def recent_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        try:
            config = load_config(self.config_path)
        except (OSError, ValueError, TypeError):
            return []
        if not config.database_path.exists():
            return []
        try:
            connection = sqlite3.connect(config.database_path, timeout=1.0)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id, event_type, code, name, occurred_at, price,
                       line_price, sources, severity, message
                FROM alerts ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            connection.close()
        except sqlite3.Error as exc:
            LOGGER.warning("提醒历史读取失败: %s", exc)
            return []
        return [
            {
                **dict(row),
                "event_label": EVENT_LABELS.get(row["event_type"], row["event_type"]),
                "sources": row["sources"].split(",") if row["sources"] else [],
            }
            for row in rows
        ]

    def shutdown(self) -> None:
        self.market_monitor.shutdown()
        self.stop()


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "StockAlertDashboard/1.0"

    @property
    def controller(self) -> DashboardController:
        return self.server.controller  # type: ignore[attr-defined]

    @property
    def static_dir(self) -> Path:
        return self.server.static_dir  # type: ignore[attr-defined]

    @property
    def remote(self) -> bool:
        return self.server.remote  # type: ignore[attr-defined]

    def _authorize(self) -> None:
        if self.client_address[0] != "127.0.0.1":
            raise DashboardError("仅接受本机代理连接", HTTPStatus.FORBIDDEN)
        if self.remote:
            if not self.controller.remote_policy.authorize(self.headers):
                raise DashboardError("远程访问未启用或账号未获授权。请通过同一账号的 Tailscale 私有地址访问。", HTTPStatus.FORBIDDEN)
            path = urlparse(self.path).path
            if path.startswith("/api/remote") or path in {"/api/news-agent/settings", "/api/news-agent/test"}:
                raise DashboardError("此设置只能在电脑本机操作", HTTPStatus.FORBIDDEN)
        else:
            port = self.server.server_address[1]
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get_all("Host", []) not in [[host] for host in hosts]:
                raise DashboardError("无效的本机访问地址", HTTPStatus.FORBIDDEN)
            if any(name.lower().startswith("tailscale-") for name in self.headers):
                raise DashboardError("远程代理必须使用专用端口，不能转发本机管理入口", HTTPStatus.FORBIDDEN)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise DashboardError("拒绝跨站请求", HTTPStatus.FORBIDDEN)

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str, content_type: str) -> None:
        try:
            body = (self.static_dir / name).read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise DashboardError("请求必须使用 application/json", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise DashboardError("无效的请求长度") from exc
        if size <= 0 or size > 2_000_000:
            raise DashboardError("请求内容为空或过大")
        try:
            return json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DashboardError("无法解析 JSON") from exc

    def _verify_origin(self) -> None:
        self._authorize()
        origin = self.headers.get("Origin")
        if not origin and not self.remote:
            return
        expected = self.controller.remote_policy.read().get("origin") if self.remote else f"http://{self.headers.get('Host')}"
        if not origin or self.headers.get_all("Origin") != [expected]:
            raise DashboardError("请求来源不匹配，请从软件页面操作", HTTPStatus.FORBIDDEN)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            self._authorize()
            self._get()
        except DashboardError as exc:
            self._send_json({"error": str(exc)}, exc.status)

    def _get(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_static("index.html", "text/html; charset=utf-8")
        elif parsed.path == "/styles.css":
            self._send_static("styles.css", "text/css; charset=utf-8")
        elif parsed.path == "/app.js":
            self._send_static("app.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/market.js":
            self._send_static("market.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/market.css":
            self._send_static("market.css", "text/css; charset=utf-8")
        elif parsed.path == "/api/market-monitor":
            self._send_json(self.controller.market_monitor.status())
        elif parsed.path == "/api/ping":
            self._send_json({"app": "stock-alert-dashboard", "ok": True, "remote": self.remote, "version": 2})
        elif parsed.path == "/api/access":
            settings = self.controller.news_agent_settings()["settings"]
            self._send_json({
                "remote": self.remote,
                "remote_port": self.controller.remote_port,
                "policy": self.controller.remote_policy.read() if not self.remote else {"enabled": True},
                "agent": {key: settings.get(key) for key in ("configured", "api_key_configured")},
            })
        elif parsed.path == "/api/config":
            config = self.controller.get_config()
            self._send_json(remote_config_view(config) if self.remote else config)
        elif parsed.path == "/api/status":
            self._send_json(self.controller.status())
        elif parsed.path == "/api/alerts":
            query = parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            self._send_json({"alerts": self.controller.recent_alerts(limit)})
        elif parsed.path == "/api/trend":
            code = parse_qs(parsed.query).get("code", [""])[0]
            try:
                self._send_json(self.controller.trend_service.get(code))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif parsed.path == "/api/analysis":
            code = parse_qs(parsed.query).get("code", [""])[0]
            try:
                self._send_json(self.controller.technical_analysis(code))
            except DashboardError as exc:
                self._send_json({"error": str(exc)}, exc.status)
        elif parsed.path == "/api/news-radar":
            force = parse_qs(parsed.query).get("refresh", ["0"])[0] in {"1", "true"}
            try:
                self._send_json(self.controller.news_radar(force=force))
            except DashboardError as exc:
                self._send_json({"error": str(exc)}, exc.status)
        elif parsed.path == "/api/news-radar/settings":
            try:
                self._send_json(self.controller.news_radar_settings())
            except DashboardError as exc:
                self._send_json({"error": str(exc)}, exc.status)
        elif parsed.path == "/api/news-agent/settings":
            try:
                self._send_json(self.controller.news_agent_settings())
            except DashboardError as exc:
                self._send_json({"error": str(exc)}, exc.status)
        elif parsed.path == "/api/news-agent/result":
            self._send_json(self.controller.news_agent_result())
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            self._verify_origin()
            path = urlparse(self.path).path
            if path == "/api/config":
                payload = self.controller.save_config(self._read_json(), remote=self.remote, require_revision=True)
            elif path == "/api/news-radar/settings":
                payload = self.controller.save_news_radar_settings(self._read_json(), require_revision=True)
            elif path == "/api/news-agent/settings":
                payload = self.controller.save_news_agent_settings(self._read_json())
            elif path == "/api/market-monitor/settings":
                payload = self.controller.market_monitor.save_settings(self._read_json())
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_json(payload)
        except (DashboardError, MarketMonitorError) as exc:
            self._send_json({"error": str(exc)}, exc.status)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            self._verify_origin()
            path = urlparse(self.path).path
            if path == "/api/monitor/start":
                payload = self.controller.start()
            elif path == "/api/monitor/stop":
                payload = self.controller.stop()
            elif path == "/api/monitor/refresh":
                payload = self.controller.refresh_once()
            elif path == "/api/market-monitor/start":
                payload = self.controller.market_monitor.start()
            elif path == "/api/market-monitor/stop":
                payload = self.controller.market_monitor.stop()
            elif path == "/api/market-monitor/refresh":
                payload = self.controller.market_monitor.refresh()
            elif path == "/api/news-agent/analyze":
                payload = self.controller.news_agent_analyze(self._read_json())
                self._send_json(payload)
                return
            elif path == "/api/news-agent/test":
                payload = self.controller.news_agent_test(self._read_json())
                self._send_json(payload)
                return
            elif path == "/api/remote/disable":
                self.controller.remote_policy.write({"enabled": False})
                payload = {"enabled": False}
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_json(payload, HTTPStatus.ACCEPTED)
        except (DashboardError, MarketMonitorError) as exc:
            self._send_json({"error": str(exc)}, exc.status)

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.debug("dashboard %s", format % args)


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], controller: DashboardController, static_dir: Path, *, remote: bool = False) -> None:
        if address[0] not in {"127.0.0.1", "localhost"}:
            raise ValueError("服务只能监听本机；远程访问请使用 Tailscale Serve 专用入口")
        self.controller = controller
        self.static_dir = static_dir
        self.remote = remote
        super().__init__(address, DashboardRequestHandler)
