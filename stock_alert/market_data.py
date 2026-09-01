"""Bounded, read-only public market feeds. Gateways are NOT independent vendors."""
from __future__ import annotations

import json
import math
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import requests

from .models import secid_for, symbol_for

TZ = ZoneInfo("Asia/Shanghai")
INDUSTRIES = frozenset("农林牧渔 基础化工 钢铁 有色金属 电子 家用电器 食品饮料 纺织服饰 轻工制造 医药生物 公用事业 交通运输 房地产 商贸零售 社会服务 综合 建筑材料 建筑装饰 电力设备 国防军工 计算机 传媒 通信 银行 非银金融 汽车 机械设备 煤炭 石油石化 环保 美容护理".split())
STOCK_FILTER = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"
FIELDS = "f2,f3,f6,f12,f13,f14,f15,f18,f124,f104,f105,f128,f136"
CONSTITUENT_FIELDS = "f2,f3,f6,f8,f12,f13,f14,f15,f18,f20,f21,f100,f124"
GATEWAYS = ("https://push2delay.eastmoney.com", "https://82.push2.eastmoney.com")
A_CODE = re.compile(r"(?:60\d{4}|68[89]\d{3}|00\d{4}|30[01]\d{3}|[48]\d{5}|92\d{4})$")
POSITION_HISTORY_LIMIT = 120


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def timestamp(value):
    value = number(value)
    if value is None or not 946684800 < value < 4102444800:
        return None
    return datetime.fromtimestamp(value, TZ).isoformat()


def normalize_row(row, *, sector=False):
    code, name = str(row.get("f12", "")), str(row.get("f14", ""))
    if (sector and name not in INDUSTRIES) or (not sector and not A_CODE.fullmatch(code)):
        return None
    last, previous, change = number(row.get("f2")), number(row.get("f18")), number(row.get("f3"))
    if last is None or last <= 0 or change is None:
        return None  # suspended/unlisted rows are not flat stocks
    if not sector and (previous is None or previous <= 0):
        return None
    amount = number(row.get("f6"))
    return {"code": code, "name": name, "change_pct": change, "last": last,
            "amount": amount if amount is not None and amount >= 0 else None,
            "timestamp": timestamp(row.get("f124")), "up": number(row.get("f104")),
            "down": number(row.get("f105")), "leader": str(row.get("f128") or ""),
            "leader_pct": number(row.get("f136"))}


def normalize_constituent(row):
    result = normalize_row(row)
    if not result:
        return None
    result.update({
        "turnover_pct": number(row.get("f8")),
        "total_market_cap": number(row.get("f20")),
        "float_market_cap": number(row.get("f21")),
        "industry": str(row.get("f100") or "").strip(),
    })
    return result


def pool_time(value):
    try:
        numeric = int(value)
        if numeric <= 0:
            return None
        text = str(numeric).zfill(6)
    except (TypeError, ValueError):
        return None
    if len(text) != 6 or not (0 <= int(text[:2]) <= 23 and 0 <= int(text[2:4]) <= 59
                              and 0 <= int(text[4:]) <= 59):
        return None
    return f"{text[:2]}:{text[2:4]}:{text[4:]}"


def normalize_pool_row(row, kind):
    code, name = str(row.get("c") or ""), str(row.get("n") or "").strip()
    if not A_CODE.fullmatch(code) or not name:
        return None
    statistics = row.get("zttj")
    if isinstance(statistics, dict):
        days, boards = int(number(statistics.get("days")) or 0), int(number(statistics.get("ct")) or 0)
        statistics = f"{days}天{boards}板" if days or boards else ""
    return {
        "code": code, "name": name, "kind": kind,
        "change_pct": number(row.get("zdp")), "amount": number(row.get("amount")),
        "turnover_pct": number(row.get("hs")), "float_market_cap": number(row.get("ltsz")),
        "streak": int(number(row.get("lbc")) or 0), "first_limit_time": pool_time(row.get("fbt")),
        "last_limit_time": pool_time(row.get("lbt")), "seal_amount": number(row.get("fund")),
        "open_count": int(number(row.get("zbc")) or 0), "industry": str(row.get("hybk") or "").strip(),
        "limit_statistics": str(statistics or "").strip(),
    }


class MarketDataClient:
    def __init__(self, directory: Path, timeout=6, budget=40):
        self.directory = directory
        self.timeout = timeout
        self.budget = budget
        self.universe_path = directory / "market-universe.json"
        self.position_history_path = directory / "market-position-history.json"
        try:
            saved = json.loads(self.position_history_path.read_text(encoding="utf-8"))
            self._position_history = saved if isinstance(saved, dict) else {}
        except (OSError, ValueError, TypeError):
            self._position_history = {}

    def _get(self, url, params=None):
        # Fixed public endpoints only; no user credentials or private watchlist.
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(url, params=params, timeout=(3, self.timeout), headers={
                "User-Agent": "Mozilla/5.0 StockAlert/1.0", "Referer": "https://quote.eastmoney.com/"})
            response.raise_for_status()
            return response

    def _page(self, page, sector, deadline, *, stock_filter=None, fields=None, fid="f12"):
        for host in GATEWAYS:
            if time.monotonic() >= deadline:
                break
            try:
                payload = self._get(host + "/api/qt/clist/get", {
                    "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                    "fid": fid, "fs": stock_filter or ("m:90 t:2 f:!50" if sector else STOCK_FILTER),
                    "fields": fields or FIELDS}).json()
                data = payload.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("total"), int):
                    continue
                rows = data.get("diff")
                if isinstance(rows, dict):
                    rows = list(rows.values())
                if isinstance(rows, list) and rows and all(isinstance(x, dict) for x in rows):
                    return rows, data["total"]
            except (requests.RequestException, ValueError, TypeError):
                pass
        return [], None

    def clist(self, sector=False):
        started = time.monotonic()
        deadline = started + self.budget
        first, total = self._page(1, sector, deadline)
        if not first or not total or total > (2000 if sector else 15000):
            return {"source": "eastmoney", "rows": [], "complete": False, "expected": total or 0,
                    "received": 0, "error": "公开行情列表不可用"}
        # Do not trust pz: the service currently caps each page at 100.
        page_size = len(first)
        pages = math.ceil(total / page_size)
        with ThreadPoolExecutor(max_workers=4) as executor:
            remaining = list(executor.map(lambda p: self._page(p, sector, deadline), range(2, pages + 1)))
        batches = [(first, total), *remaining]
        raw = {str(x.get("f12")): x for rows, _ in batches for x in rows if x.get("f12")}
        complete = len(raw) == total and all(count == total for _, count in batches)
        rows = [parsed for row in raw.values() if (parsed := normalize_row(row, sector=sector))]
        result = {"source": "eastmoney", "rows": rows, "complete": complete,
                  "expected": 31 if sector else total, "received": len(rows) if sector else len(raw),
                  "raw_expected": total, "raw_received": len(raw), "elapsed_seconds": round(time.monotonic() - started, 1),
                  "error": None if complete else "分页缺失、重复或总数变化，本轮不产生信号"}
        if sector:
            result["complete"] = complete and len(rows) == 31 and len({r["name"] for r in rows}) == 31
            if not result["complete"]:
                result["error"] = "一级行业口径不完整，本轮不产生轮动信号"
        elif complete and len(rows) >= 3000:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                pending = self.universe_path.with_suffix(".pending")
                pending.write_text(json.dumps({"date": datetime.now(TZ).date().isoformat(),
                                               "codes": sorted({r["code"] for r in rows})}), encoding="utf-8")
                pending.replace(self.universe_path)
            except OSError:
                result["error"] = "本轮行情完整，但备用名单缓存保存失败"
        return result

    def stocks(self):
        primary = self.clist()
        if primary["complete"]:
            return primary
        try:
            universe = json.loads(self.universe_path.read_text(encoding="utf-8"))
            age = (datetime.now(TZ).date() - datetime.fromisoformat(universe["date"]).date()).days
            codes = sorted({c for c in universe["codes"] if isinstance(c, str) and A_CODE.fullmatch(c)})
            if not 0 <= age <= 7 or len(codes) < 3000:
                return primary
        except (OSError, ValueError, KeyError, TypeError):
            return primary
        deadline = time.monotonic() + self.budget

        def batch(items):
            if time.monotonic() >= deadline:
                return []
            symbols = [("bj" if c.startswith(("4", "8", "92")) else "sh" if c.startswith("6") else "sz") + c for c in items]
            try:
                body = self._get("https://qt.gtimg.cn/q=" + ",".join(symbols)).content.decode("gb18030", errors="replace")
                rows = []
                for match in re.finditer(r'v_[^=]+="(.*?)";', body):
                    f = match.group(1).split("~")
                    if len(f) < 39 or f[2] not in items:
                        continue
                    try:
                        stamp = datetime.strptime(f[30], "%Y%m%d%H%M%S").replace(tzinfo=TZ).timestamp()
                    except ValueError:
                        stamp = None  # never substitute fetch time for quote time
                    amount = number(f[37])  # Tencent turnover is in 10,000 yuan
                    row = normalize_row({"f12": f[2], "f14": f[1], "f2": f[3], "f18": f[4],
                                         "f3": f[32], "f6": amount * 10000 if amount is not None else None,
                                         "f124": stamp})
                    if row:
                        rows.append(row)
                return rows
            except requests.RequestException:
                return []
        with ThreadPoolExecutor(max_workers=4) as executor:
            rows = [r for batch_rows in executor.map(batch, [codes[i:i+300] for i in range(0, len(codes), 300)]) for r in batch_rows]
        rows = list({r["code"]: r for r in rows}.values())
        if len(rows) <= len(primary["rows"]):
            return primary
        return {"source": "tencent", "rows": rows, "expected": len(codes), "received": len(rows),
                "complete": len(rows) >= len(codes) * .98,
                "error": "东财失败；腾讯使用最近完整市场名单降级，可能缺少新上市股票",
                "universe_date": universe["date"], "universe_age_days": age}

    def sector_constituents(self, sector_code):
        started = time.monotonic()
        deadline = started + min(self.budget, 20)
        first, total = self._page(1, False, deadline, stock_filter=f"b:{sector_code}",
                                  fields=CONSTITUENT_FIELDS, fid="f3")
        if not first or not total or total > 2000:
            return {"source": "eastmoney", "rows": [], "complete": False, "expected": total or 0,
                    "received": 0, "error": "行业成分股列表不可用"}
        page_size = len(first)
        pages = math.ceil(total / page_size)
        with ThreadPoolExecutor(max_workers=3) as executor:
            remaining = list(executor.map(
                lambda page: self._page(page, False, deadline, stock_filter=f"b:{sector_code}",
                                        fields=CONSTITUENT_FIELDS, fid="f3"),
                range(2, pages + 1)))
        batches = [(first, total), *remaining]
        raw = {str(item.get("f12")): item for rows, _ in batches for item in rows if item.get("f12")}
        complete = len(raw) == total and all(count == total for _, count in batches)
        rows = [parsed for item in raw.values() if (parsed := normalize_constituent(item))]
        return {"source": "eastmoney", "rows": rows, "complete": complete,
                "expected": total, "received": len(raw),
                "error": None if complete else "行业成分股分页不完整"}

    def pool(self, kind, date):
        endpoint = {"up": "getTopicZTPool", "down": "getTopicDTPool", "broken": "getTopicZBPool"}[kind]
        try:
            data = self._get("https://push2ex.eastmoney.com/" + endpoint, {
                "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt", "Pageindex": 0,
                "pagesize": 10000, "sort": "fund:asc" if kind == "down" else "fbt:asc", "date": date}).json().get("data")
            if not isinstance(data, dict) or str(data.get("qdate")) != date:
                raise ValueError()
            rows, total = data.get("pool"), data.get("tc")
            if not isinstance(rows, list) or not isinstance(total, int) or len(rows) != total:
                raise ValueError()
            if len({r["c"] for r in rows}) != total:
                raise ValueError()
            normalized = [item for row in rows if (item := normalize_pool_row(row, kind))]
            return {"ok": True, "count": total, "date": date, "rows": normalized,
                    "max_streak": max((item["streak"] for item in normalized), default=0),
                    "normalized_count": len(normalized)}
        except (requests.RequestException, ValueError, TypeError, KeyError):
            return {"ok": False, "count": None, "date": date, "error": "股池未返回同日完整数据"}

    @staticmethod
    def _parse_history(payload):
        result = []
        for row in ((payload.get("data") or {}).get("klines") or []):
            fields = row.split(",") if isinstance(row, str) else row
            if not isinstance(fields, (list, tuple)) or len(fields) < 3:
                continue
            try:
                day, close = date.fromisoformat(str(fields[0])), float(fields[2])
            except (TypeError, ValueError):
                continue
            if close > 0:
                result.append((day, close))
        return result

    def _eastmoney_history(self, code, sector=False):
        for attempt in range(2):
            try:
                response = self._get("https://push2his.eastmoney.com/api/qt/stock/kline/get", {
                    "secid": f"90.{code}" if sector else secid_for(code), "klt": "101",
                    "fqt": "0" if sector else "1", "lmt": "28", "end": "20500101",
                    "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                })
                return self._parse_history(response.json())
            except (requests.RequestException, ValueError):
                if attempt:
                    raise
                time.sleep(.15)
        return []

    def _tencent_history(self, code):
        payload = None
        for attempt in range(2):
            try:
                response = self._get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", {
                    "param": f"{symbol_for(code)},day,,,28,qfq"})
                payload = response.json()
                break
            except (requests.RequestException, ValueError):
                if attempt:
                    raise
                time.sleep(.15)
        block = (((payload or {}).get("data") or {}).get(symbol_for(code)) or {})
        rows = block.get("qfqday") or block.get("day") or []
        result = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            try:
                day, close = date.fromisoformat(str(row[0])), float(row[2])
            except (TypeError, ValueError):
                continue
            if close > 0:
                result.append((day, close))
        return result

    def _history_is_ready(self, key, quote_date):
        entry = self._position_history.get(key, {})
        if entry.get("quote_date") != quote_date.isoformat():
            return False
        enough_rows = len(entry.get("closes", [])) >= 20
        enough_sources = key.startswith("sector:") or len(entry.get("sources", [])) >= 2
        if enough_rows and enough_sources:
            return True
        return time.time() - float(entry.get("attempted_at") or 0) < 60

    def _save_position_history(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            pending = self.position_history_path.with_suffix(".pending")
            pending.write_text(json.dumps(self._position_history, ensure_ascii=False), encoding="utf-8")
            pending.replace(self.position_history_path)
        except OSError:
            pass

    def position_histories(self, strongest, member_feeds, pools, quote_date):
        """Fetch bounded daily histories once per day for ladder evidence, never for alerts."""
        pool_codes = {row.get("code") for kind in ("up", "broken")
                      for row in pools.get(kind, {}).get("rows", []) if row.get("code")}
        priority = []
        for sector in strongest:
            rows = member_feeds.get(sector["code"], {}).get("rows", [])
            matched = [row for row in rows if row.get("code") in pool_codes]
            representatives = sorted(rows, key=lambda row: -(row.get("total_market_cap") or 0))[:8]
            movers = sorted(rows, key=lambda row: (-(row.get("change_pct") or -999),
                                                   -(row.get("amount") or 0)))[:4]
            capacity = max(rows, key=lambda row: row.get("amount") or 0, default=None)
            priority.extend(matched + representatives + movers + ([capacity] if capacity else []))
        ordered_codes = list(dict.fromkeys(row["code"] for row in priority if row and row.get("code")))
        selected_codes = ordered_codes[:POSITION_HISTORY_LIMIT]
        keys = [(f"stock:{code}", code, False) for code in selected_codes]
        keys += [(f"sector:{sector['code']}", sector["code"], True) for sector in strongest]
        pending = [(key, code, sector) for key, code, sector in keys
                   if not self._history_is_ready(key, quote_date)]
        grouped, errors = defaultdict(dict), defaultdict(list)
        tasks = {}
        with ThreadPoolExecutor(max_workers=min(16, max(1, len(pending) * 2))) as executor:
            for key, code, sector in pending:
                tasks[executor.submit(self._eastmoney_history, code, sector)] = (key, "eastmoney")
                if not sector:
                    tasks[executor.submit(self._tencent_history, code)] = (key, "tencent")
            for future in as_completed(tasks):
                key, source = tasks[future]
                try:
                    rows = [(day, close) for day, close in future.result() if day < quote_date]
                    if rows:
                        grouped[key][source] = rows
                    else:
                        errors[key].append(f"{source}: 无已完成日线")
                except Exception as exc:  # noqa: BLE001 - sources fail independently
                    errors[key].append(f"{source}: {type(exc).__name__}")
        attempted_at = time.time()
        for key, _, _ in pending:
            existing = self._position_history.get(key, {})
            source_rows = {}
            for source, rows in existing.get("source_closes", {}).items():
                parsed = []
                for item in rows:
                    try:
                        parsed.append((date.fromisoformat(item["date"]), float(item["close"])))
                    except (KeyError, TypeError, ValueError):
                        continue
                if parsed:
                    source_rows[source] = parsed
            if not source_rows and len(existing.get("sources", [])) == 1:
                source = existing["sources"][0]
                source_rows[source] = [(date.fromisoformat(item["date"]), float(item["close"]))
                                       for item in existing.get("closes", [])]
            source_rows.update(grouped.get(key, {}))
            by_date = defaultdict(list)
            for rows in source_rows.values():
                for day, close in rows:
                    by_date[day].append(close)
            closes = [{"date": day.isoformat(), "close": round(float(median(by_date[day])), 4)}
                      for day in sorted(by_date)[-25:]]
            self._position_history[key] = {
                "quote_date": quote_date.isoformat(), "closes": closes,
                "sources": sorted(source_rows), "errors": errors.get(key, []),
                "source_closes": {source: [{"date": day.isoformat(), "close": close} for day, close in rows[-25:]]
                                  for source, rows in source_rows.items()},
                "attempted_at": attempted_at,
            }
        if pending:
            self._save_position_history()
        return {
            "stocks": {code: self._position_history.get(f"stock:{code}", {}) for code in selected_codes},
            "sectors": {sector["code"]: self._position_history.get(f"sector:{sector['code']}", {})
                        for sector in strongest},
            "requested_stock_count": len(ordered_codes), "selected_stock_count": len(selected_codes),
            "truncated_count": max(0, len(ordered_codes) - len(selected_codes)),
        }

    def fetch(self, now):
        jobs = {"stocks": self.stocks, "sectors": lambda: self.clist(sector=True)}
        jobs.update({kind: lambda k=kind: self.pool(k, now.strftime("%Y%m%d")) for kind in ("up", "down", "broken")})
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {key: executor.submit(fn) for key, fn in jobs.items()}
            result = {}
            for key, future in futures.items():
                try:
                    result[key] = future.result()
                except Exception:
                    # An independent source must not erase the other successful feeds.
                    result[key] = {"source": "eastmoney", "rows": [], "complete": False,
                                   "ok": False, "count": None, "expected": 0, "received": 0,
                                   "error": "采集失败，请稍后重试"}
        sector_rows = result.get("sectors", {}).get("rows", [])
        strongest = sorted(sector_rows, key=lambda row: (-row["change_pct"], row["code"]))[:5]
        with ThreadPoolExecutor(max_workers=5) as executor:
            result["sector_members"] = {
                row["code"]: feed for row, feed in zip(
                    strongest, executor.map(lambda item: self.sector_constituents(item["code"]), strongest))
            }
        result["position_history"] = self.position_histories(
            strongest, result["sector_members"], result, now.date())
        return result
