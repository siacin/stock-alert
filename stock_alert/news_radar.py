from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests


LOGGER = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")
API_TEMPLATE = "https://newsnow.busiyi.world/api/s?id={platform_id}&latest"
THS_HOT_URL = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
THS_HOT_PAGE = "https://eq.10jqka.com.cn/webpage/ths-hot-list/index.html?showStatusBar=true"
EASTMONEY_RANK_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
EASTMONEY_QUOTE_URLS = (
    "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
    "https://push2.eastmoney.com/api/qt/ulist.np/get",
)

PLATFORMS: tuple[dict[str, str], ...] = (
    {"id": "ths-hot", "name": "同花顺热榜", "category": "热股"},
    {"id": "eastmoney-hot", "name": "东方财富人气榜", "category": "热股"},
    {"id": "cls-hot", "name": "财联社热门", "category": "财经"},
    {"id": "wallstreetcn-hot", "name": "华尔街见闻·最热", "category": "财经"},
    {"id": "wallstreetcn-news", "name": "华尔街见闻·最新", "category": "财经"},
    {"id": "xueqiu", "name": "雪球热股", "category": "热股"},
    {"id": "jin10", "name": "金十数据", "category": "快讯"},
    {"id": "gelonghui", "name": "格隆汇", "category": "财经"},
    {"id": "mktnews", "name": "MKT 新闻", "category": "快讯"},
    {"id": "fastbull", "name": "快讯通", "category": "快讯"},
    {"id": "baidu", "name": "百度热搜", "category": "热榜"},
    {"id": "toutiao", "name": "今日头条", "category": "热榜"},
    {"id": "weibo", "name": "微博", "category": "热榜"},
    {"id": "thepaper", "name": "澎湃新闻", "category": "新闻"},
    {"id": "ifeng", "name": "凤凰网", "category": "新闻"},
    {"id": "cankaoxiaoxi", "name": "参考消息", "category": "新闻"},
    {"id": "zaobao", "name": "联合早报", "category": "新闻"},
    {"id": "ithome", "name": "IT之家", "category": "科技"},
    {"id": "juejin", "name": "掘金", "category": "科技"},
    {"id": "github", "name": "GitHub", "category": "科技"},
    {"id": "hackernews", "name": "Hacker News", "category": "科技"},
    {"id": "solidot", "name": "Solidot", "category": "科技"},
    {"id": "v2ex", "name": "V2EX", "category": "科技"},
    {"id": "zhihu", "name": "知乎", "category": "社区"},
    {"id": "bilibili-hot-search", "name": "哔哩哔哩热搜", "category": "社区"},
    {"id": "douyin", "name": "抖音", "category": "热榜"},
)
PLATFORM_MAP = {item["id"]: item for item in PLATFORMS}
DEFAULT_PLATFORM_IDS = (
    "ths-hot",
    "eastmoney-hot",
    "cls-hot",
    "wallstreetcn-hot",
    "wallstreetcn-news",
    "xueqiu",
    "jin10",
    "gelonghui",
    "mktnews",
    "fastbull",
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "platforms": list(DEFAULT_PLATFORM_IDS),
    "keywords": ["A股", "上证", "深证", "创业板", "科创板", "北交所"],
    "refresh_interval_seconds": 60,
    "request_timeout_seconds": 8,
    "max_items_per_source": 30,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}
THS_HEADERS = {
    **HEADERS,
    "Referer": THS_HOT_PAGE,
}
EASTMONEY_RANK_HEADERS = {
    **HEADERS,
    "Origin": "https://emappdata.eastmoney.com",
    "Referer": "https://emappdata.eastmoney.com/",
}
EASTMONEY_QUOTE_HEADERS = {
    **HEADERS,
    "Referer": "https://quote.eastmoney.com/",
}


class NewsRadarError(RuntimeError):
    pass


def normalize_settings(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    platforms = data.get("platforms", DEFAULT_SETTINGS["platforms"])
    if not isinstance(platforms, list):
        raise NewsRadarError("资讯平台必须是列表")
    normalized_platforms: list[str] = []
    for value in platforms:
        platform_id = str(value).strip()
        if platform_id not in PLATFORM_MAP:
            raise NewsRadarError(f"不支持的资讯平台：{platform_id}")
        if platform_id not in normalized_platforms:
            normalized_platforms.append(platform_id)
    if not normalized_platforms:
        raise NewsRadarError("至少选择一个资讯平台")
    if len(normalized_platforms) > 16:
        raise NewsRadarError("资讯平台最多选择 16 个")

    keywords = data.get("keywords", DEFAULT_SETTINGS["keywords"])
    if isinstance(keywords, str):
        keywords = re.split(r"[,，\n]", keywords)
    if not isinstance(keywords, list):
        raise NewsRadarError("资讯关键词必须是列表")
    normalized_keywords: list[str] = []
    for value in keywords:
        keyword = str(value).strip()
        if not keyword or keyword in normalized_keywords:
            continue
        if len(keyword) > 30:
            raise NewsRadarError("单个资讯关键词不能超过 30 个字符")
        normalized_keywords.append(keyword)
    if len(normalized_keywords) > 40:
        raise NewsRadarError("资讯关键词最多设置 40 个")

    try:
        refresh_interval = int(data.get("refresh_interval_seconds", 60))
        request_timeout = float(data.get("request_timeout_seconds", 8))
        max_items = int(data.get("max_items_per_source", 30))
    except (TypeError, ValueError) as exc:
        raise NewsRadarError("资讯雷达的数值设置无效") from exc
    if not 30 <= refresh_interval <= 1800:
        raise NewsRadarError("资讯刷新间隔需在 30–1800 秒之间")
    if not 2 <= request_timeout <= 20:
        raise NewsRadarError("资讯请求超时需在 2–20 秒之间")
    if not 5 <= max_items <= 50:
        raise NewsRadarError("每个平台展示数量需在 5–50 条之间")
    return {
        "platforms": normalized_platforms,
        "keywords": normalized_keywords,
        "refresh_interval_seconds": refresh_interval,
        "request_timeout_seconds": request_timeout,
        "max_items_per_source": max_items,
    }


def read_settings(config_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NewsRadarError(f"配置读取失败：{exc}") from exc
    return normalize_settings(payload.get("news_radar"))


class NewsRadarService:
    def __init__(
        self,
        config_path: str | Path,
        request_get: Callable[..., Any] | None = None,
        request_post: Callable[..., Any] | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self._request_get = request_get or requests.get
        self._request_post = request_post or requests.post
        self._lock = threading.RLock()
        self._cached_at = 0.0
        self._cached_payload: dict[str, Any] | None = None

    def catalog(self) -> list[dict[str, str]]:
        return [dict(item) for item in PLATFORMS]

    def settings(self) -> dict[str, Any]:
        return read_settings(self.config_path)

    def invalidate(self) -> None:
        with self._lock:
            self._cached_at = 0.0
            self._cached_payload = None

    def cached_hot_stocks(self) -> list[dict[str, Any]]:
        """Return cached direct stock ranks only; never trigger network traffic."""
        with self._lock:
            items = (self._cached_payload or {}).get("items", [])
            return [dict(item) for item in items if item.get("stock_code")]

    def fetch(
        self,
        force: bool = False,
        watch_quotes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        settings = self.settings()
        now = time.monotonic()
        with self._lock:
            cached = self._cached_payload
            cache_age = now - self._cached_at
            if cached and not force and cache_age < settings["refresh_interval_seconds"]:
                result = json.loads(json.dumps(cached, ensure_ascii=False))
                result["cache_hit"] = True
                result["cache_age_seconds"] = round(cache_age, 1)
                return result

        config_payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        watch_terms = self._watch_terms(config_payload)
        for quote in watch_quotes or []:
            code = re.sub(r"\D", "", str(quote.get("code", "")))
            name = str(quote.get("name", "")).strip()
            if code and len(name) >= 2:
                watch_terms.append({"term": name, "label": name, "code": code})
        watch_terms = list(
            {
                (item["term"].lower(), item["code"]): item
                for item in watch_terms
            }.values()
        )
        selected = [PLATFORM_MAP[item] for item in settings["platforms"]]
        source_results: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        workers = min(8, len(selected))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="news-radar") as executor:
            futures = {
                executor.submit(self._fetch_platform, platform, settings): platform
                for platform in selected
            }
            for future in as_completed(futures):
                platform = futures[future]
                try:
                    source_results[platform["id"]] = future.result()
                except Exception as exc:  # noqa: BLE001 - one platform must not stop the radar
                    LOGGER.warning("资讯平台 %s 获取失败：%s", platform["id"], exc)
                    source_results[platform["id"]] = (
                        {
                            **platform,
                            "ok": False,
                            "status": "failed",
                            "latency_ms": None,
                            "item_count": 0,
                            "error": self._short_error(exc),
                        },
                        [],
                    )

        sources: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        for platform in selected:
            source, platform_items = source_results[platform["id"]]
            sources.append(source)
            for item in platform_items:
                self._apply_relevance(item, watch_terms, settings["keywords"])
                items.append(item)

        items.sort(key=lambda item: (-item["relevance_score"], item["rank"], item["source_name"]))
        related_count = sum(bool(item["matched_stocks"] or item["matched_keywords"]) for item in items)
        fetched_at = datetime.now(TZ).isoformat()
        payload = {
            "ok": any(source["ok"] for source in sources),
            "fetched_at": fetched_at,
            "cache_hit": False,
            "cache_age_seconds": 0,
            "sources": sources,
            "items": items,
            "item_count": len(items),
            "related_count": related_count,
            "settings": settings,
            "catalog": self.catalog(),
            "watch_terms": watch_terms,
            "message": (
                f"已获取 {len(items)} 条资讯，其中 {related_count} 条命中关注词"
                if items
                else "本轮没有获取到资讯，请稍后重试"
            ),
        }
        if payload["ok"]:
            with self._lock:
                self._cached_at = time.monotonic()
                self._cached_payload = payload
        return payload

    def _fetch_platform(
        self,
        platform: dict[str, str],
        settings: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if platform["id"] == "ths-hot":
            return self._fetch_ths_hot(platform, settings)
        if platform["id"] == "eastmoney-hot":
            return self._fetch_eastmoney_hot(platform, settings)

        started = time.monotonic()
        response = self._request_get(
            API_TEMPLATE.format(platform_id=platform["id"]),
            headers=HEADERS,
            timeout=settings["request_timeout_seconds"],
        )
        response.raise_for_status()
        try:
            data = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NewsRadarError("响应不是有效的 UTF-8 JSON") from exc
        status = str(data.get("status", "unknown"))
        if status not in {"success", "cache"}:
            raise NewsRadarError(f"接口状态异常：{status}")
        raw_items = data.get("items", [])
        if not isinstance(raw_items, list):
            raise NewsRadarError("接口没有返回资讯列表")
        items: list[dict[str, Any]] = []
        seen_titles: set[str] = set()
        for rank, raw in enumerate(raw_items[: settings["max_items_per_source"]], 1):
            if not isinstance(raw, dict):
                continue
            title = re.sub(r"\s+", " ", str(raw.get("title", ""))).strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            url = self._safe_url(raw.get("url") or raw.get("mobileUrl"))
            fingerprint = hashlib.sha1(
                f"{platform['id']}|{title}".encode("utf-8"), usedforsecurity=False
            ).hexdigest()[:16]
            items.append(
                {
                    "id": fingerprint,
                    "title": title,
                    "url": url,
                    "source_id": platform["id"],
                    "source_name": platform["name"],
                    "category": platform["category"],
                    "rank": rank,
                    "updated_at": self._timestamp(data.get("updatedTime")),
                }
            )
        latency = int((time.monotonic() - started) * 1000)
        return (
            {
                **platform,
                "ok": True,
                "status": status,
                "latency_ms": latency,
                "item_count": len(items),
                "error": None,
            },
            items,
        )

    def _fetch_ths_hot(
        self,
        platform: dict[str, str],
        settings: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        started = time.monotonic()
        response = self._request_get(
            THS_HOT_URL,
            params={"stock_type": "a", "type": "day", "list_type": "normal"},
            headers=THS_HEADERS,
            timeout=settings["request_timeout_seconds"],
        )
        data = self._response_json(response)
        if str(data.get("status_code")) != "0":
            raise NewsRadarError(f"同花顺接口状态异常：{data.get('status_code', 'unknown')}")
        raw_items = (data.get("data") or {}).get("stock_list", [])
        if not isinstance(raw_items, list):
            raise NewsRadarError("同花顺接口没有返回热股列表")

        updated_at = datetime.now(TZ).isoformat()
        items: list[dict[str, Any]] = []
        for fallback_rank, raw in enumerate(raw_items[: settings["max_items_per_source"]], 1):
            if not isinstance(raw, dict):
                continue
            code = re.sub(r"\D", "", str(raw.get("code", "")))
            name = re.sub(r"\s+", " ", str(raw.get("name", ""))).strip()
            if not code or not name:
                continue
            tag = raw.get("tag") if isinstance(raw.get("tag"), dict) else {}
            rank = self._positive_int(raw.get("order"), fallback_rank)
            items.append(
                self._hot_stock_item(
                    platform=platform,
                    rank=rank,
                    name=name,
                    code=code,
                    url=f"https://stockpage.10jqka.com.cn/{code}/",
                    updated_at=updated_at,
                    change_pct=self._finite_number(raw.get("rise_and_fall")),
                    heat=self._positive_int(raw.get("rate"), None),
                    rank_change=self._finite_number(raw.get("hot_rank_chg")),
                    hot_tag=str(tag.get("popularity_tag", "")).strip(),
                    concept_tags=[
                        str(value).strip()
                        for value in tag.get("concept_tag", [])
                        if str(value).strip()
                    ] if isinstance(tag.get("concept_tag"), list) else [],
                )
            )
        return self._source_result(platform, started, items, "success")

    def _fetch_eastmoney_hot(
        self,
        platform: dict[str, str],
        settings: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        started = time.monotonic()
        rank_response = self._request_post(
            EASTMONEY_RANK_URL,
            json={
                "appId": "appId01",
                "globalId": "786e4c21-70dc-435a-93bb-38",
                "marketType": "",
                "pageNo": 1,
                "pageSize": settings["max_items_per_source"],
            },
            headers=EASTMONEY_RANK_HEADERS,
            timeout=settings["request_timeout_seconds"],
        )
        rank_data = self._response_json(rank_response)
        if str(rank_data.get("code")) != "0":
            raise NewsRadarError(f"东方财富人气榜状态异常：{rank_data.get('message', 'unknown')}")
        rank_items = rank_data.get("data", [])
        if not isinstance(rank_items, list):
            raise NewsRadarError("东方财富接口没有返回人气榜")
        rank_items = [item for item in rank_items if isinstance(item, dict)][
            : settings["max_items_per_source"]
        ]

        securities: list[tuple[str, str, str]] = []
        for raw in rank_items:
            symbol = str(raw.get("sc", "")).strip().upper()
            match = re.fullmatch(r"([A-Z]{2})(\d{6})", symbol)
            if not match:
                continue
            prefix, code = match.groups()
            securities.append((symbol, code, f"{'1' if prefix == 'SH' else '0'}.{code}"))

        quote_map: dict[str, dict[str, Any]] = {}
        status = "success"
        source_error: str | None = None
        if securities:
            try:
                quote_params = {
                    "ut": "f057cbcbce2a86e2866ab8877db1d059",
                    "fltt": "2",
                    "invt": "2",
                    "fields": "f14,f3,f12,f2",
                    "secids": ",".join(item[2] for item in securities),
                }
                raw_quotes: list[dict[str, Any]] | None = None
                last_quote_error: Exception | None = None
                for endpoint in EASTMONEY_QUOTE_URLS:
                    try:
                        quote_response = self._request_get(
                            endpoint,
                            params=quote_params,
                            headers=EASTMONEY_QUOTE_HEADERS,
                            timeout=settings["request_timeout_seconds"],
                        )
                        quote_data = self._response_json(quote_response)
                        candidate = (quote_data.get("data") or {}).get("diff", [])
                        if not isinstance(candidate, list) or not candidate:
                            raise NewsRadarError("东方财富行情补充接口没有返回列表")
                        raw_quotes = candidate
                        break
                    except Exception as exc:  # noqa: BLE001 - try the alternate official host
                        last_quote_error = exc
                if raw_quotes is None:
                    raise last_quote_error or NewsRadarError("东方财富行情补充失败")
                quote_map = {
                    str(item.get("f12", "")): item
                    for item in raw_quotes
                    if isinstance(item, dict) and item.get("f12")
                }
            except Exception as exc:  # noqa: BLE001 - rankings remain useful without quote fields
                status = "partial"
                source_error = f"排名正常，行情补充失败：{self._short_error(exc)}"
                LOGGER.warning("东方财富人气榜行情补充失败：%s", exc)

        symbol_map = {symbol: code for symbol, code, _ in securities}
        updated_at = datetime.now(TZ).isoformat()
        items: list[dict[str, Any]] = []
        for fallback_rank, raw in enumerate(rank_items, 1):
            symbol = str(raw.get("sc", "")).strip().upper()
            code = symbol_map.get(symbol)
            if not code:
                continue
            quote = quote_map.get(code, {})
            name = re.sub(r"\s+", " ", str(quote.get("f14") or code)).strip()
            rank = self._positive_int(raw.get("rk"), fallback_rank)
            items.append(
                self._hot_stock_item(
                    platform=platform,
                    rank=rank,
                    name=name,
                    code=code,
                    url=f"https://quote.eastmoney.com/{symbol.lower()}.html",
                    updated_at=updated_at,
                    price=self._finite_number(quote.get("f2")),
                    change_pct=self._finite_number(quote.get("f3")),
                    rank_change=self._finite_number(raw.get("hisRc")),
                )
            )
        return self._source_result(platform, started, items, status, source_error)

    @staticmethod
    def _hot_stock_item(
        platform: dict[str, str],
        rank: int,
        name: str,
        code: str,
        url: str,
        updated_at: str,
        price: float | None = None,
        change_pct: float | None = None,
        heat: int | None = None,
        rank_change: float | None = None,
        hot_tag: str = "",
        concept_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        fingerprint = hashlib.sha1(
            f"{platform['id']}|{code}|{name}".encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:16]
        return {
            "id": fingerprint,
            "title": name,
            "url": url,
            "source_id": platform["id"],
            "source_name": platform["name"],
            "category": platform["category"],
            "rank": rank,
            "updated_at": updated_at,
            "stock_code": code,
            "price": price,
            "change_pct": change_pct,
            "heat": heat,
            "rank_change": rank_change,
            "hot_tag": hot_tag,
            "concept_tags": concept_tags or [],
        }

    @staticmethod
    def _source_result(
        platform: dict[str, str],
        started: float,
        items: list[dict[str, Any]],
        status: str,
        error: str | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return (
            {
                **platform,
                "ok": True,
                "status": status,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "item_count": len(items),
                "error": error,
            },
            items,
        )

    @staticmethod
    def _response_json(response: Any) -> dict[str, Any]:
        response.raise_for_status()
        try:
            data = json.loads(response.content.decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NewsRadarError("响应不是有效的 UTF-8 JSON") from exc
        if not isinstance(data, dict):
            raise NewsRadarError("接口响应不是 JSON 对象")
        return data

    @staticmethod
    def _finite_number(value: Any) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) else None

    @staticmethod
    def _positive_int(value: Any, default: int | None) -> int | None:
        try:
            result = int(float(value))
        except (TypeError, ValueError):
            return default
        return result if result >= 0 else default

    @staticmethod
    def _watch_terms(config_payload: dict[str, Any]) -> list[dict[str, str]]:
        terms: list[dict[str, str]] = []
        for stock in config_payload.get("stocks", []):
            if stock.get("enabled", True) is False:
                continue
            code = re.sub(r"\D", "", str(stock.get("code", "")))
            name = str(stock.get("name", "")).strip()
            if code:
                terms.append({"term": code, "label": name or code, "code": code})
            if len(name) >= 2:
                terms.append({"term": name, "label": name, "code": code})
        unique: dict[tuple[str, str], dict[str, str]] = {}
        for item in terms:
            unique[(item["term"].lower(), item["code"])] = item
        return list(unique.values())

    @staticmethod
    def _apply_relevance(
        item: dict[str, Any],
        watch_terms: list[dict[str, str]],
        keywords: list[str],
    ) -> None:
        title_lower = f"{item['title']} {item.get('stock_code', '')}".lower()
        stock_matches: dict[str, str] = {}
        for candidate in watch_terms:
            if candidate["term"].lower() in title_lower:
                stock_matches[candidate["code"] or candidate["label"]] = candidate["label"]
        keyword_matches = [keyword for keyword in keywords if keyword.lower() in title_lower]
        rank_score = max(1, 31 - int(item["rank"]))
        item["matched_stocks"] = list(stock_matches.values())
        item["matched_keywords"] = keyword_matches
        item["relevance_score"] = rank_score + len(stock_matches) * 100 + len(keyword_matches) * 35
        item["related"] = bool(stock_matches or keyword_matches)

    @staticmethod
    def _safe_url(value: Any) -> str:
        url = str(value or "").strip()
        parsed = urlparse(url)
        return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""

    @staticmethod
    def _timestamp(value: Any) -> str | None:
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return None
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, TZ).isoformat()
        except (OSError, OverflowError, ValueError):
            return None

    @staticmethod
    def _short_error(exc: Exception) -> str:
        text = re.sub(r"\s+", " ", str(exc)).strip()
        return text[:180] or exc.__class__.__name__
