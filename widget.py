from __future__ import annotations

import argparse
import ctypes
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from tkinter import BOTH, LEFT, RIGHT, X, Button, Canvas, Frame, Label, Menu, TclError, Tk
from typing import Any


BG = "#0b1513"
PANEL = "#11211e"
BORDER = "#294039"
TEXT = "#edf8f3"
MUTED = "#819990"
UP = "#ff786f"
DOWN = "#69dea2"
AMBER = "#f3c76b"
HIGHLIGHT = "#4a3026"
PRIORITY_EVENTS = {"bomb", "open_board_warning", "rapid_rise", "rapid_fall"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A 股迷你置顶盯盘悬浮窗")
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="本地控制台地址")
    parser.add_argument("--poll", type=float, default=2.0, help="状态刷新间隔")
    return parser.parse_args()


def acquire_single_instance() -> object | None:
    if sys.platform != "win32":
        return object()
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\StockAlertMiniWidget")
    if not handle or kernel32.GetLastError() == 183:
        return None
    return handle


class MiniStockWidget:
    compact_width = 258
    expanded_width = 350
    row_height = 28
    empty_height = 32
    control_height = 18
    detail_height = 174
    max_rows = 10

    def __init__(self, api_url: str, poll_seconds: float) -> None:
        self.api_url = api_url.rstrip("/")
        self.poll_seconds = max(1.0, poll_seconds)
        self.root = Tk()
        self.root.title("盘中哨兵")
        self.root.configure(bg=BORDER)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.94)
        try:
            self.root.attributes("-toolwindow", True)
        except Exception:
            pass

        self.quotes: list[dict[str, Any]] = []
        self.alerts: list[dict[str, Any]] = []
        self.selected_code: str | None = None
        self.expanded = False
        self.fetching = False
        self.trend_loading = False
        self.trend: dict[str, Any] = {"points": [], "sources": []}
        self.last_status: dict[str, Any] = {}
        self.row_widgets: dict[str, dict[str, Any]] = {}
        self.compact_height = self.empty_height + self.control_height
        self.minimized = False
        self.closing = False
        self.drag_offset = (0, 0)
        self.collapse_job: str | None = None
        self.expand_job: str | None = None
        self.highlight_job: str | None = None
        self.highlight_step = 0
        self.priority_code: str | None = None
        self.last_alert_id: int | None = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._place_initially()
        self._bind_common(self.root)
        self._bind_common(self.shell)
        self._bind_common(self.control_bar)
        self._bind_common(self.control_title)
        self._bind_common(self.rows_frame)
        self.minimize_button.bind("<Enter>", self._on_enter, add="+")
        self.minimize_button.bind("<Leave>", self._on_leave, add="+")
        self.close_button.bind("<Enter>", self._on_enter, add="+")
        self.close_button.bind("<Leave>", self._on_leave, add="+")
        self.detail.bind("<Enter>", self._on_enter, add="+")
        self.detail.bind("<Leave>", self._on_leave, add="+")
        self.canvas.bind("<Configure>", lambda _event: self._draw_chart())
        self.root.bind("<Escape>", lambda _event: self._collapse())
        self.root.bind("<Map>", self._on_map, add="+")
        self.root.after(100, self._poll)

    def _build_ui(self) -> None:
        self.shell = Frame(self.root, bg=BG, highlightthickness=1, highlightbackground=BORDER)
        self.shell.pack(fill=BOTH, expand=True, padx=1, pady=1)
        self.control_bar = Frame(self.shell, bg="#0e1b18", height=self.control_height, cursor="fleur")
        self.control_bar.pack(fill=X)
        self.control_bar.pack_propagate(False)
        self.control_title = Label(
            self.control_bar,
            text="盘中哨兵",
            bg="#0e1b18",
            fg=MUTED,
            font=("Microsoft YaHei UI", 7),
            anchor="w",
        )
        self.control_title.pack(side=LEFT, padx=(7, 0))
        self.close_button = Button(
            self.control_bar,
            text="×",
            command=self._close,
            bg="#0e1b18",
            fg=MUTED,
            activebackground="#4a2420",
            activeforeground=UP,
            bd=0,
            relief="flat",
            highlightthickness=0,
            padx=6,
            pady=0,
            font=("Segoe UI", 9),
            cursor="hand2",
            takefocus=False,
        )
        self.minimize_button = Button(
            self.control_bar,
            text="—",
            command=self._minimize,
            bg="#0e1b18",
            fg=MUTED,
            activebackground=BORDER,
            activeforeground=TEXT,
            bd=0,
            relief="flat",
            highlightthickness=0,
            padx=6,
            pady=0,
            font=("Segoe UI", 8, "bold"),
            cursor="hand2",
            takefocus=False,
        )
        self.close_button.pack(side=RIGHT, fill="y")
        self.minimize_button.pack(side=RIGHT, fill="y")
        self.rows_frame = Frame(self.shell, bg=BG, cursor="fleur")
        self.rows_frame.pack(fill=X)

        self.detail = Frame(self.shell, bg=PANEL)
        self.detail_header = Frame(self.detail, bg=PANEL)
        self.detail_header.pack(fill=X, padx=11, pady=(7, 2))
        self.detail_text = Label(
            self.detail_header,
            text="悬停股票加载分时走势",
            bg=PANEL,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        )
        self.detail_text.pack(side=LEFT, fill=X, expand=True)
        self.source_text = Label(
            self.detail_header, text="", bg=PANEL, fg=MUTED, font=("Consolas", 7), anchor="e"
        )
        self.source_text.pack(side=RIGHT)
        self.canvas = Canvas(self.detail, bg=PANEL, bd=0, highlightthickness=0, height=142)
        self.canvas.pack(fill=BOTH, expand=True, padx=8, pady=(0, 7))

        self.menu = Menu(self.root, tearoff=False, bg=PANEL, fg=TEXT, activebackground="#203a33", activeforeground=TEXT)
        self.menu.add_command(label="上一只", command=lambda: self._cycle(-1))
        self.menu.add_command(label="下一只", command=lambda: self._cycle(1))
        self.menu.add_separator()
        self.menu.add_command(label="打开完整控制台", command=lambda: webbrowser.open(self.api_url))
        self.menu.add_command(label="关闭悬浮窗", command=self._close)

    def _place_initially(self) -> None:
        self.root.update_idletasks()
        x = max(8, self.root.winfo_screenwidth() - self.compact_width - 26)
        self.root.geometry(f"{self.compact_width}x{self.compact_height}+{x}+28")

    def _bind_common(self, widget, code: str | None = None) -> None:
        widget.bind("<Enter>", lambda event, value=code: self._on_enter(event, value), add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress-1>", self._drag_start, add="+")
        widget.bind("<B1-Motion>", self._drag_move, add="+")
        widget.bind("<Button-3>", self._show_menu, add="+")
        widget.bind("<MouseWheel>", self._mouse_wheel, add="+")

    def _drag_start(self, event) -> None:
        self.drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_move(self, event) -> None:
        self.root.geometry(f"+{event.x_root - self.drag_offset[0]}+{event.y_root - self.drag_offset[1]}")

    def _show_menu(self, event) -> None:
        self.menu.tk_popup(event.x_root, event.y_root)

    def _mouse_wheel(self, event) -> None:
        self._cycle(-1 if event.delta > 0 else 1)

    def _minimize(self) -> None:
        if self.expanded:
            self._collapse()
        self.minimized = True
        self.root.attributes("-topmost", False)
        try:
            self.root.attributes("-toolwindow", False)
        except Exception:
            pass
        self.root.overrideredirect(False)
        self.root.iconify()

    def _close(self) -> None:
        """Close only the floating widget; the monitoring backend keeps running."""
        if self.closing:
            return
        self.closing = True
        self.root.destroy()

    def _on_map(self, _event=None) -> None:
        if self.minimized:
            self.root.after(80, self._restore_if_visible)

    def _restore_if_visible(self) -> None:
        if not self.minimized or self.root.state() != "normal":
            return
        self.minimized = False
        self.root.overrideredirect(True)
        try:
            self.root.attributes("-toolwindow", True)
        except Exception:
            pass
        self.root.attributes("-topmost", True)
        self.root.lift()

    def _on_enter(self, _event=None, code: str | None = None) -> None:
        if code and code != self.selected_code:
            self.selected_code = code
            self._update_detail_header()
            if self.expanded:
                self._load_trend(force=True)
        if self.collapse_job:
            self.root.after_cancel(self.collapse_job)
            self.collapse_job = None
        if not self.expanded and not self.expand_job:
            self.expand_job = self.root.after(180, self._expand_if_inside)

    def _on_leave(self, _event=None) -> None:
        if self.collapse_job:
            self.root.after_cancel(self.collapse_job)
        self.collapse_job = self.root.after(450, self._collapse_if_outside)

    def _pointer_inside(self) -> bool:
        x, y = self.root.winfo_pointerx(), self.root.winfo_pointery()
        left, top = self.root.winfo_rootx(), self.root.winfo_rooty()
        return left <= x <= left + self.root.winfo_width() and top <= y <= top + self.root.winfo_height()

    def _expand_if_inside(self) -> None:
        self.expand_job = None
        if self._pointer_inside():
            self._expand()

    def _collapse_if_outside(self) -> None:
        self.collapse_job = None
        if not self._pointer_inside():
            self._collapse()

    def _expand(self) -> None:
        if self.expanded:
            return
        self.expanded = True
        right_edge = self.root.winfo_x() + self.root.winfo_width()
        height = self.compact_height + self.detail_height
        x = max(0, right_edge - self.expanded_width)
        y = self.root.winfo_y()
        if y + height > self.root.winfo_screenheight():
            y = max(0, self.root.winfo_screenheight() - height - 8)
        self.root.geometry(f"{self.expanded_width}x{height}+{x}+{y}")
        self.detail.pack(fill=BOTH, expand=True)
        self._load_trend()

    def _collapse(self) -> None:
        if not self.expanded:
            return
        self.expanded = False
        right_edge = self.root.winfo_x() + self.root.winfo_width()
        self.detail.pack_forget()
        self.root.geometry(
            f"{self.compact_width}x{self.compact_height}+{max(0, right_edge - self.compact_width)}+{self.root.winfo_y()}"
        )

    def _resize_for_rows(self, row_count: int, has_footer: bool = False) -> None:
        rows_height = max(self.empty_height, row_count * self.row_height + (18 if has_footer else 0) + 2)
        self.compact_height = self.control_height + rows_height
        right_edge = self.root.winfo_x() + self.root.winfo_width()
        width = self.expanded_width if self.expanded else self.compact_width
        height = self.compact_height + self.detail_height if self.expanded else self.compact_height
        self.root.geometry(f"{width}x{height}+{max(0, right_edge - width)}+{self.root.winfo_y()}")

    def _cycle(self, direction: int) -> None:
        if not self.quotes:
            return
        codes = [quote["code"] for quote in self.quotes]
        try:
            current = codes.index(self.selected_code)
        except ValueError:
            current = 0
        self.selected_code = codes[(current + direction) % len(codes)]
        self._update_detail_header()
        if self.expanded:
            self._load_trend(force=True)

    def _get_json(self, path: str, timeout: float = 4.0) -> dict:
        request = urllib.request.Request(self.api_url + path, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _poll(self) -> None:
        if self.closing:
            return
        if not self.fetching:
            self.fetching = True
            threading.Thread(target=self._poll_worker, name="widget-status", daemon=True).start()
        self.root.after(round(self.poll_seconds * 1000), self._poll)

    def _after_if_open(self, callback) -> None:
        if self.closing:
            return
        try:
            self.root.after(0, callback)
        except (RuntimeError, TclError):
            pass

    def _poll_worker(self) -> None:
        try:
            status = self._get_json("/api/status")
            alerts = self._get_json("/api/alerts?limit=100").get("alerts", [])
            self._after_if_open(lambda: self._apply_status(status, alerts))
        except (OSError, ValueError, urllib.error.URLError):
            self._after_if_open(self._show_disconnected)
        finally:
            self.fetching = False

    def _apply_status(self, status: dict, alerts: list[dict]) -> None:
        self.last_status = status
        self.alerts = alerts
        all_quotes = list((status.get("snapshot") or {}).get("quotes") or [])
        self.quotes = [quote for quote in all_quotes if quote.get("widget_enabled", True)]
        codes = {quote.get("code") for quote in self.quotes}
        if self.quotes and self.selected_code not in codes:
            self.selected_code = self.quotes[0]["code"]
        self._detect_priority_alert(alerts, codes)
        self._render_rows()
        self._update_detail_header()

    @staticmethod
    def _alert_is_fresh(alert: dict, seconds: float = 12.0) -> bool:
        try:
            occurred = datetime.fromisoformat(str(alert.get("occurred_at")))
            return 0 <= (datetime.now(occurred.tzinfo) - occurred).total_seconds() <= seconds
        except (TypeError, ValueError):
            return False

    def _detect_priority_alert(self, alerts: list[dict], visible_codes: set[str]) -> None:
        ids = [int(item.get("id") or 0) for item in alerts]
        newest_id = max(ids, default=0)
        if self.last_alert_id is None:
            candidates = [
                item
                for item in alerts
                if item.get("event_type") in PRIORITY_EVENTS
                and item.get("code") in visible_codes
                and self._alert_is_fresh(item)
            ]
        else:
            candidates = [
                item
                for item in alerts
                if int(item.get("id") or 0) > self.last_alert_id
                and item.get("event_type") in PRIORITY_EVENTS
                and item.get("code") in visible_codes
            ]
        self.last_alert_id = max(self.last_alert_id or 0, newest_id)
        if candidates:
            latest = max(candidates, key=lambda item: int(item.get("id") or 0))
            code = str(latest.get("code"))
            self.selected_code = code
            self.priority_code = code
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self._start_highlight()

    def _display_quotes(self) -> tuple[list[dict[str, Any]], int]:
        ordered = list(self.quotes)
        if self.priority_code:
            ordered.sort(key=lambda quote: quote.get("code") != self.priority_code)
        hidden = max(0, len(ordered) - self.max_rows)
        return ordered[: self.max_rows], hidden

    def _render_rows(self) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()
        self.row_widgets.clear()
        visible, hidden = self._display_quotes()
        state = self.last_status.get("state")
        dot_color = DOWN if state in {"running", "waiting"} else AMBER if state == "refreshing" else MUTED
        if not visible:
            row = Frame(self.rows_frame, bg=BG, height=self.empty_height)
            row.pack(fill=X)
            row.pack_propagate(False)
            dot = Label(row, text="●", bg=BG, fg=dot_color, font=("Segoe UI", 7))
            dot.pack(side=LEFT, padx=(8, 6))
            message = Label(row, text="前端未选择悬浮股票", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 8))
            message.pack(side=LEFT)
            for widget in (row, dot, message):
                self._bind_common(widget)
            self._resize_for_rows(0)
            return

        for quote in visible:
            code = str(quote.get("code"))
            row = Frame(self.rows_frame, bg=BG, height=self.row_height)
            row.pack(fill=X)
            row.pack_propagate(False)
            dot = Label(row, text="●", bg=BG, fg=dot_color, font=("Segoe UI", 6))
            dot.pack(side=LEFT, padx=(7, 4))
            name = str(quote.get("name") or "股票")[:7]
            name_label = Label(row, text=name, bg=BG, fg=TEXT, font=("Microsoft YaHei UI", 8, "bold"), width=7, anchor="w")
            name_label.pack(side=LEFT)
            message, message_color = self._key_message(quote)
            alert_label = Label(row, text=message, bg=PANEL, fg=message_color, font=("Microsoft YaHei UI", 7), padx=4, pady=1)
            alert_label.pack(side=RIGHT, padx=(3, 6))
            price = float(quote.get("last") or 0)
            price_label = Label(row, text=f"{price:.2f}", bg=BG, fg=MUTED, font=("Consolas", 8), width=6, anchor="e")
            price_label.pack(side=RIGHT, padx=(2, 1))
            change = float(quote.get("change_pct") or 0)
            color = UP if change > 0 else DOWN if change < 0 else TEXT
            change_label = Label(row, text=f"{change:+.2f}%", bg=BG, fg=color, font=("Consolas", 11, "bold"), width=8, anchor="e")
            change_label.pack(side=RIGHT)
            widgets = (row, dot, name_label, change_label, price_label, alert_label)
            self.row_widgets[code] = {"widgets": widgets, "alert": alert_label}
            for widget in widgets:
                self._bind_common(widget, code)

        if hidden:
            footer = Label(
                self.rows_frame,
                text=f"还有 {hidden} 只 · 在前端调整悬浮选择",
                bg="#0e1b18",
                fg=MUTED,
                font=("Microsoft YaHei UI", 7),
                anchor="center",
            )
            footer.pack(fill=X)
            self._bind_common(footer)
        self._resize_for_rows(len(visible), bool(hidden))

    def _show_disconnected(self) -> None:
        self.last_status = {"state": "error"}
        self.quotes = []
        self._render_rows()
        children = self.rows_frame.winfo_children()
        if children:
            labels = children[0].winfo_children()
            if len(labels) > 1:
                labels[0].configure(fg=UP)
                labels[1].configure(text="后台未连接，正在重连", fg=AMBER)

    def _selected_quote(self) -> dict | None:
        return next((quote for quote in self.quotes if quote.get("code") == self.selected_code), None)

    def _key_message(self, quote: dict) -> tuple[str, str]:
        latest = next((item for item in self.alerts if item.get("code") == quote.get("code")), None)
        if latest:
            try:
                occurred = datetime.fromisoformat(str(latest.get("occurred_at")))
                if (datetime.now(occurred.tzinfo) - occurred).total_seconds() < 1800:
                    color = UP if latest.get("event_type") in {"bomb", "rapid_rise"} else AMBER
                    return str(latest.get("event_label") or "关键提醒"), color
            except (TypeError, ValueError):
                pass
        if quote.get("board_state") == "sealed":
            return "封板", UP
        if quote.get("board_state") == "opened":
            return "开板", AMBER
        if quote.get("below_cost"):
            return "成本下", AMBER
        if quote.get("below_ma5"):
            return "MA5 下", AMBER
        if quote.get("below_average"):
            return "均价下", MUTED
        return "正常", DOWN

    def _update_detail_header(self) -> None:
        quote = self._selected_quote()
        if not quote:
            self.detail_text.configure(text="悬停股票加载分时走势")
            return
        name = str(quote.get("name") or "股票")
        ma5 = quote.get("ma5")
        average = quote.get("average_price")
        line_text = f"均价 {average:.2f} · MA5 {ma5:.2f}" if average and ma5 else f"MA5 {ma5:.2f}" if ma5 else "分时走势"
        self.detail_text.configure(text=f"{name} · {line_text}")

    def _start_highlight(self) -> None:
        if self.highlight_job:
            self.root.after_cancel(self.highlight_job)
        self.highlight_step = 0
        self._pulse_highlight()

    def _pulse_highlight(self) -> None:
        active = self.highlight_step % 2 == 0
        self.shell.configure(highlightbackground=AMBER if active else BORDER, highlightthickness=2 if active else 1)
        row_info = self.row_widgets.get(self.priority_code or "")
        if row_info:
            widgets = row_info["widgets"]
            for widget in widgets[:-1]:
                widget.configure(bg=HIGHLIGHT if active else BG)
            row_info["alert"].configure(bg="#65412c" if active else PANEL)
        self.highlight_step += 1
        if self.highlight_step < 8:
            self.highlight_job = self.root.after(240, self._pulse_highlight)
        else:
            self.highlight_job = self.root.after(2600, self._finish_highlight)

    def _finish_highlight(self) -> None:
        self.highlight_job = None
        self.shell.configure(highlightbackground=BORDER, highlightthickness=1)
        self.priority_code = None
        self._render_rows()

    def _load_trend(self, force: bool = False) -> None:
        if not self.selected_code or self.trend_loading:
            return
        if not force and self.trend.get("code") == self.selected_code and self.trend.get("points"):
            self._draw_chart()
            return
        self.trend_loading = True
        code = self.selected_code
        self.canvas.delete("all")
        self.canvas.create_text(170, 70, text="正在加载分时走势…", fill=MUTED, font=("Microsoft YaHei UI", 8))
        threading.Thread(target=self._trend_worker, args=(code,), name="widget-trend", daemon=True).start()

    def _trend_worker(self, code: str) -> None:
        try:
            payload = self._get_json("/api/trend?code=" + urllib.parse.quote(code), timeout=8.0)
        except (OSError, ValueError, urllib.error.URLError):
            payload = {"code": code, "points": [], "sources": [], "error": "分时数据暂不可用"}
        self._after_if_open(lambda: self._apply_trend(payload))

    def _apply_trend(self, payload: dict) -> None:
        self.trend_loading = False
        if payload.get("code") == self.selected_code:
            self.trend = payload
            sources = "+".join(payload.get("sources") or [])
            self.source_text.configure(text=f"{payload.get('trade_date') or ''}  {sources}")
            self._draw_chart()

    def _draw_chart(self) -> None:
        if not self.expanded:
            return
        self.canvas.delete("all")
        points = self.trend.get("points") or []
        width = max(self.canvas.winfo_width(), 320)
        height = max(self.canvas.winfo_height(), 132)
        left, right, top, bottom = 12, width - 12, 13, height - 18
        if len(points) < 2:
            self.canvas.create_text(
                width / 2,
                height / 2,
                text=self.trend.get("error") or "暂无分时数据",
                fill=MUTED,
                font=("Microsoft YaHei UI", 8),
            )
            return
        prices = [float(item["price"]) for item in points]
        averages = [float(item["average_price"]) for item in points if item.get("average_price")]
        low = min(prices + averages)
        high = max(prices + averages)
        padding = max((high - low) * 0.12, high * 0.001)
        low -= padding
        high += padding

        def coordinates(values: list[tuple[int, float]]) -> list[float]:
            coords: list[float] = []
            denominator = max(1, len(points) - 1)
            for index, value in values:
                coords.extend(
                    (
                        left + (right - left) * index / denominator,
                        bottom - (value - low) / (high - low) * (bottom - top),
                    )
                )
            return coords

        for fraction in (0.0, 0.5, 1.0):
            y = top + (bottom - top) * fraction
            self.canvas.create_line(left, y, right, y, fill="#20332e", dash=(2, 4))
        price_coords = coordinates(list(enumerate(prices)))
        line_color = UP if prices[-1] >= prices[0] else DOWN
        self.canvas.create_polygon([left, bottom, *price_coords, right, bottom], fill="#382522" if line_color == UP else "#153028", outline="")
        self.canvas.create_line(price_coords, fill=line_color, width=2, smooth=True)
        average_values = [(index, float(item["average_price"])) for index, item in enumerate(points) if item.get("average_price")]
        if len(average_values) > 1:
            self.canvas.create_line(coordinates(average_values), fill=AMBER, width=1, dash=(4, 3), smooth=True)
        self.canvas.create_text(left, 3, text=f"高 {max(prices):.2f}", anchor="nw", fill=MUTED, font=("Consolas", 7))
        self.canvas.create_text(right, 3, text=f"低 {min(prices):.2f}", anchor="ne", fill=MUTED, font=("Consolas", 7))
        self.canvas.create_text(left, height - 11, text=points[0]["time"], anchor="w", fill=MUTED, font=("Consolas", 7))
        self.canvas.create_text(right, height - 11, text=points[-1]["time"], anchor="e", fill=MUTED, font=("Consolas", 7))

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    mutex = acquire_single_instance()
    if mutex is None:
        return 0
    args = parse_args()
    try:
        MiniStockWidget(args.url, args.poll).run()
    except Exception as exc:  # show startup errors when launched manually
        print(f"悬浮盯盘窗启动失败: {exc}", file=sys.stderr)
        return 2
    finally:
        _ = mutex
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
