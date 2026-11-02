#!/usr/bin/env python3
import base64
import hmac
import html
import io
import json
import os
import re
import socket
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from admin_ui import enhance_admin_page
from management import handle as management_handle
import traffic_stats
from proxy_admin import section as proxy_admin_section, handle as proxy_handle, save_and_reload, telegram_rule_lines


WIDTH = int(os.getenv("DASHBOARD_WIDTH", "600"))
HEIGHT = int(os.getenv("DASHBOARD_HEIGHT", "800"))
PORT = int(os.getenv("PORT", "8787"))
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "60"))
TZ_NAME = os.getenv("TZ", "Asia/Shanghai")

COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price?"
    + urllib.parse.urlencode(
        {
            "ids": "bitcoin,ethereum",
            "vs_currencies": "eur,usd,cny",
            "include_24hr_change": "true",
            "include_last_updated_at": "true",
        }
    )
)
COINGECKO_CHART_DAYS = os.getenv("COINGECKO_CHART_DAYS", "1")
COINGECKO_COINS = ("bitcoin", "ethereum")
CRYPTOCOMPARE_BASE = "https://min-api.cryptocompare.com/data"
OKX_BASE = "https://www.okx.com/api/v5/market"
STATE_FILE = Path(os.getenv("STATE_FILE", "data/kindle_state.json"))
CONFIG_FILE = Path(os.getenv("CONFIG_FILE", "/data/dashboard_config.json"))
MIHOMO_CONFIG_FILE = Path(os.getenv("MIHOMO_CONFIG_FILE", "/proxy/config.yaml"))
WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


LAST_DATA: dict[str, Any] = {}
LAST_IMAGE: bytes | None = None
LAST_RENDER_TS = 0.0
RENDER_LOCK = threading.Lock()   # 同一时间只允许一个线程去拉取数据并渲染
FILE_LOCK = threading.Lock()     # 状态文件 / 配置文件的写入锁


def load_runtime_config() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_runtime_config(config: dict[str, Any]) -> None:
    with FILE_LOCK:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
        temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(CONFIG_FILE)


def setting(name: str, default: str = "") -> str:
    value = load_runtime_config().get(name)
    if value is not None and value != "":
        return str(value).strip()
    return os.getenv(name, default).strip()


def setting_float(name: str) -> float | None:
    value = setting(name)
    return safe_float(value) if value else None


def http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 12) -> Any:
    request_headers = {"User-Agent": "KindleDashboard/2.0", "Accept": "application/json"}
    request_headers.update(headers or {})
    req = urllib.request.Request(url, headers=request_headers)
    proxy_url = setting("PROXY_URL")
    if proxy_url:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
        response = opener.open(req, timeout=timeout)
    else:
        response = urllib.request.urlopen(req, timeout=timeout)
    with response as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def money(value: Any, digits: int = 0) -> str:
    try:
        n = float(value)
    except Exception:
        return "--"
    if digits == 0:
        return f"{n:,.0f}"
    return f"{n:,.{digits}f}"


def pct(value: Any) -> str:
    try:
        n = float(value)
    except Exception:
        return "--"
    sign = "+" if n >= 0 else ""
    return f"{sign}{n:.2f}%"


def safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def env_float(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return safe_float(value)


def local_now() -> datetime:
    return datetime.now().astimezone()


def chinese_datetime(dt: datetime) -> str:
    return f"{dt.year}年{dt.month}月{dt.day}日{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"


def chinese_date_week_time(dt: datetime) -> str:
    return f"{dt.month}月{dt.day}日 {WEEKDAYS_CN[dt.weekday()]} {dt.hour:02d}:{dt.minute:02d}"


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(updates: dict[str, Any]) -> dict[str, Any]:
    with FILE_LOCK:
        state = load_state()
        state.update({k: v for k, v in updates.items() if v is not None and v != ""})
        state["updated_at"] = local_now().isoformat(timespec="seconds")
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(STATE_FILE)
    return state


def coingecko_market_chart_url(coin_id: str) -> str:
    return (
        f"https://api.coingecko.com/api/v3/coins/{urllib.parse.quote(coin_id)}/market_chart?"
        + urllib.parse.urlencode({"vs_currency": "eur", "days": COINGECKO_CHART_DAYS})
    )


def market_chart_points(coin_id: str) -> list[float]:
    chart = http_json(coingecko_market_chart_url(coin_id), timeout=15)
    points: list[float] = []
    for item in chart.get("prices", []):
        if isinstance(item, list) and len(item) >= 2:
            value = safe_float(item[1])
            if value is not None:
                points.append(value)
    return points[-80:]


def cryptocompare_market_data() -> tuple[dict[str, Any], dict[str, list[float]]]:
    url = f"{CRYPTOCOMPARE_BASE}/pricemultifull?" + urllib.parse.urlencode(
        {"fsyms": "BTC,ETH", "tsyms": "EUR,CNY,USD"}
    )
    payload = http_json(url, timeout=10)
    raw = payload.get("RAW", {})
    prices: dict[str, Any] = {}
    charts: dict[str, list[float]] = {}
    for symbol, coin_id in (("BTC", "bitcoin"), ("ETH", "ethereum")):
        quote = raw.get(symbol, {})
        eur = quote.get("EUR", {})
        cny = quote.get("CNY", {})
        usd = quote.get("USD", {})
        if safe_float(eur.get("PRICE")) is None:
            raise ValueError(f"CryptoCompare missing {symbol}/EUR")
        prices[coin_id] = {
            "eur": eur.get("PRICE"),
            "cny": cny.get("PRICE"),
            "usd": usd.get("PRICE"),
            "eur_24h_change": eur.get("CHANGEPCT24HOUR"),
            "last_updated_at": int(time.time()),
            "source": "CryptoCompare",
        }
        history_url = f"{CRYPTOCOMPARE_BASE}/v2/histohour?" + urllib.parse.urlencode(
            {"fsym": symbol, "tsym": "EUR", "limit": 48}
        )
        history = http_json(history_url, timeout=10)
        rows = history.get("Data", {}).get("Data", [])
        charts[coin_id] = [float(row["close"]) for row in rows if safe_float(row.get("close")) is not None]
    return prices, charts


def public_fx_rates() -> tuple[float, float]:
    providers = (
        "https://open.er-api.com/v6/latest/USD",
        "https://api.exchangerate-api.com/v4/latest/USD",
    )
    errors = []
    for url in providers:
        try:
            data = http_json(url, timeout=8)
            rates = data.get("rates", {})
            eur = safe_float(rates.get("EUR"))
            cny = safe_float(rates.get("CNY"))
            if eur and cny:
                return eur, cny
        except Exception as exc:
            errors.append(type(exc).__name__)
    raise RuntimeError("FX unavailable: " + ",".join(errors))


def okx_market_data() -> tuple[dict[str, Any], dict[str, list[float]]]:
    eur_rate, cny_rate = public_fx_rates()
    prices: dict[str, Any] = {}
    charts: dict[str, list[float]] = {}
    for symbol, coin_id in (("BTC", "bitcoin"), ("ETH", "ethereum")):
        ticker_url = f"{OKX_BASE}/ticker?" + urllib.parse.urlencode({"instId": f"{symbol}-USDT"})
        ticker_data = http_json(ticker_url, timeout=10).get("data", [])
        if not ticker_data:
            raise ValueError(f"OKX missing {symbol} ticker")
        ticker = ticker_data[0]
        last = safe_float(ticker.get("last"))
        open24h = safe_float(ticker.get("open24h"))
        if last is None:
            raise ValueError(f"OKX invalid {symbol} ticker")
        change = ((last / open24h) - 1) * 100 if open24h else None
        prices[coin_id] = {
            "eur": last * eur_rate,
            "cny": last * cny_rate,
            "usd": last,
            "eur_24h_change": change,
            "last_updated_at": int(time.time()),
            "source": "OKX",
        }
        candles_url = f"{OKX_BASE}/history-candles?" + urllib.parse.urlencode(
            {"instId": f"{symbol}-USDT", "bar": "1H", "limit": "48"}
        )
        rows = http_json(candles_url, timeout=10).get("data", [])
        points = [(int(row[0]), float(row[4]) * eur_rate) for row in rows if len(row) > 4]
        charts[coin_id] = [value for _, value in sorted(points)]
    return prices, charts


def coingecko_market_data() -> tuple[dict[str, Any], dict[str, list[float]]]:
    prices = http_json(COINGECKO_URL, timeout=10)
    charts = {coin_id: market_chart_points(coin_id) for coin_id in COINGECKO_COINS}
    for quote in prices.values():
        if isinstance(quote, dict):
            quote["source"] = "CoinGecko"
    return prices, charts


def fetch_market_data() -> tuple[dict[str, Any], dict[str, list[float]], list[str]]:
    errors: list[str] = []
    loaders = {
        "okx": ("OKX", okx_market_data),
        "cryptocompare": ("CryptoCompare", cryptocompare_market_data),
        "coingecko": ("CoinGecko", coingecko_market_data),
    }
    requested = [item.strip().lower() for item in setting("COIN_PROVIDER_ORDER", "okx,cryptocompare,coingecko").split(",")]
    order = [key for key in requested if key in loaders]
    order.extend(key for key in loaders if key not in order)
    for key in order:
        name, loader = loaders[key]
        try:
            prices, charts = loader()
            return prices, charts, errors
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    raise RuntimeError(" | ".join(errors))


def load_text_env(name: str, file_name: str) -> str | None:
    direct = os.getenv(name)
    if direct:
        return direct.strip()
    path = os.getenv(file_name)
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except Exception as exc:
            return f"{file_name} read error: {exc}"
    return None


def load_usage_sources() -> dict[str, Any]:
    """Load optional live quota data without requiring a container restart.

    Each *_USAGE_FILE may contain either plain text (used as the provider
    summary) or JSON in the form documented in README.md.
    """
    result: dict[str, Any] = {}
    for provider, env_name in (("codex", "CODEX_USAGE_FILE"), ("claude", "CLAUDE_USAGE_FILE")):
        path = os.getenv(env_name)
        if not path:
            continue
        try:
            raw = Path(path).read_text(encoding="utf-8").strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"text": raw}
            if isinstance(parsed, dict):
                result[provider] = parsed
        except Exception as exc:
            result[provider] = {"text": f"{env_name} read error: {exc}"}
    return result


def source_value(source: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = source.get(name)
        if value is not None and value != "":
            return value
    return None


def source_quota(source: dict[str, Any], key: str, fallback_percent: float, fallback_text: str) -> dict[str, Any]:
    raw = source.get("quotas", {}).get(key) if isinstance(source.get("quotas"), dict) else None
    if raw is None:
        raw = source.get(key)
    if isinstance(raw, dict):
        percent = safe_float(raw.get("percent"))
        text = str(raw.get("text") or fallback_text)
        percent_value: float | None = (
            clamp(percent, 0, 100) if percent is not None else (None if "percent" in raw else fallback_percent)
        )
    else:
        percent = safe_float(raw)
        text = fallback_text
        percent_value = clamp(percent, 0, 100) if percent is not None else fallback_percent
    return {
        "percent": percent_value,
        "text": text,
    }


def openai_today_cost_text() -> str | None:
    key = os.getenv("OPENAI_ADMIN_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        return None

    now = int(time.time())
    start = now - (now % 86400)
    params = urllib.parse.urlencode({"start_time": start, "end_time": now, "bucket_width": "1d"})
    url = f"https://api.openai.com/v1/organization/costs?{params}"
    headers = {"Authorization": f"Bearer {key}"}
    try:
        data = http_json(url, headers=headers, timeout=15)
    except Exception as exc:
        return f"OpenAI cost: API error ({type(exc).__name__})"

    # The API has used bucket/result structures over time; parse defensively.
    total = 0.0
    try:
        buckets = data.get("data", [])
        for bucket in buckets:
            for result in bucket.get("results", []):
                amount = result.get("amount", {})
                if isinstance(amount, dict):
                    total += float(amount.get("value", 0) or 0)
                elif isinstance(amount, (int, float)):
                    total += float(amount)
    except Exception:
        return "OpenAI cost: parse error"

    return f"OpenAI API today: ${total:.3f}"


def compact_tokens(value: float) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.2f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return f"{value:.0f}"


def parse_bucket_time(value: Any) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), timezone.utc)
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        pass
    return None


def budget_percent(prefix: str, period: str, tokens: float) -> float | None:
    budget = setting_float(f"{prefix}_{period.upper()}_TOKEN_BUDGET")
    if budget and budget > 0:
        return clamp(tokens / budget * 100, 0, 100)
    return None


def build_api_usage_source(prefix: str, buckets: list[tuple[datetime, float]], provider: str) -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    local = local_now()
    start_today_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_today = start_today_local.astimezone(timezone.utc)
    periods = (
        ("3h", "3H", now_utc - timedelta(hours=3)),
        ("5h", "5H", now_utc - timedelta(hours=5)),
        ("today", "TODAY", start_today),
        ("week", "WEEK", now_utc - timedelta(days=7)),
    )
    quotas: dict[str, Any] = {}
    for key, budget_key, cutoff in periods:
        total = sum(tokens for timestamp, tokens in buckets if timestamp >= cutoff)
        quotas[key] = {
            "percent": budget_percent(prefix, budget_key, total),
            "text": f"已用{compact_tokens(total)} tokens",
        }
    return {"text": f"{provider} 管理员接口实时数据", "quotas": quotas}


def api_error_source(provider: str, exc: Exception) -> dict[str, Any]:
    detail = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
    label = f"{provider}读取失败:{detail}"
    return {
        "text": label,
        "quotas": {
            key: {"percent": None, "text": label}
            for key in ("3h", "5h", "today", "week")
        },
    }


def openai_admin_usage_source() -> dict[str, Any] | None:
    key = setting("OPENAI_ADMIN_KEY")
    if not key:
        return None
    now_utc = datetime.now(timezone.utc)
    params = urllib.parse.urlencode(
        {
            "start_time": int((now_utc - timedelta(days=7)).timestamp()),
            "end_time": int(now_utc.timestamp()),
            "bucket_width": "1h",
            "limit": 168,
        }
    )
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        data = http_json(f"https://api.openai.com/v1/organization/usage/completions?{params}", headers, 15)
        buckets: list[tuple[datetime, float]] = []
        for bucket in data.get("data", []):
            timestamp = parse_bucket_time(bucket.get("start_time"))
            if timestamp is None:
                continue
            tokens = 0.0
            for result in bucket.get("results", []):
                tokens += float(result.get("input_tokens", 0) or 0)
                tokens += float(result.get("output_tokens", 0) or 0)
            buckets.append((timestamp, tokens))
        return build_api_usage_source("OPENAI", buckets, "OpenAI")
    except Exception as exc:
        return api_error_source("OpenAI", exc)


def anthropic_admin_usage_source() -> dict[str, Any] | None:
    key = setting("ANTHROPIC_ADMIN_KEY")
    if not key:
        return None
    now_utc = datetime.now(timezone.utc)
    start = (now_utc - timedelta(days=7)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    end = now_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    params = urllib.parse.urlencode(
        {"starting_at": start, "ending_at": end, "bucket_width": "1h", "limit": 168}
    )
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    try:
        data = http_json(
            f"https://api.anthropic.com/v1/organizations/usage_report/messages?{params}", headers, 15
        )
        buckets: list[tuple[datetime, float]] = []
        for bucket in data.get("data", []):
            timestamp = parse_bucket_time(bucket.get("starting_at"))
            if timestamp is None:
                continue
            tokens = 0.0
            for result in bucket.get("results", []):
                tokens += float(result.get("uncached_input_tokens", result.get("input_tokens", 0)) or 0)
                tokens += float(result.get("cache_read_input_tokens", 0) or 0)
                tokens += float(result.get("output_tokens", 0) or 0)
                cache_creation = result.get("cache_creation", 0)
                if isinstance(cache_creation, dict):
                    tokens += sum(float(v or 0) for v in cache_creation.values())
                else:
                    tokens += float(cache_creation or 0)
            buckets.append((timestamp, tokens))
        return build_api_usage_source("ANTHROPIC", buckets, "Anthropic")
    except Exception as exc:
        return api_error_source("Anthropic", exc)


def usage_block(prefix: str, fallback_name: str, fallback_text: str) -> dict[str, Any]:
    percent = env_float(f"{prefix}_USAGE_PERCENT")
    if percent is None:
        percent = env_float(f"{prefix}_QUOTA_PERCENT")
    used = env_float(f"{prefix}_USAGE_USED")
    limit = env_float(f"{prefix}_USAGE_LIMIT")
    if percent is None and used is not None and limit and limit > 0:
        percent = used / limit * 100

    label = os.getenv(f"{prefix}_USAGE_LABEL", fallback_name).strip() or fallback_name
    text = fallback_text
    if used is not None and limit:
        text = f"{used:g}/{limit:g}"
    elif percent is not None:
        text = f"{percent:.0f}% used"

    return {
        "label": label,
        "percent": None if percent is None else clamp(percent, 0, 100),
        "text": text,
    }


def quota_percent(*names: str, default: float = 0.0) -> float:
    for name in names:
        value = env_float(name)
        if value is not None:
            return clamp(value, 0, 100)
    return default


def quota_text(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def quota_blocks(sources: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    sources = sources or {}
    codex_source = sources.get("codex", {}) if isinstance(sources.get("codex", {}), dict) else {}
    claude_source = sources.get("claude", {}) if isinstance(sources.get("claude", {}), dict) else {}
    codex_q = {
        key: source_quota(codex_source, key, quota_percent(env_name, default=0), text)
        for key, env_name, text in (
            ("3h", "CODEX_3H_PERCENT", quota_text("CODEX_3H_TEXT", "短周期")),
            ("5h", "CODEX_5H_PERCENT", quota_text("CODEX_5H_TEXT", "滚动窗口")),
            ("today", "CODEX_TODAY_PERCENT", quota_text("CODEX_TODAY_TEXT", "今日累计")),
            ("week", "CODEX_WEEK_PERCENT", quota_text("CODEX_WEEK_TEXT", "周累计")),
        )
    }
    claude_q = {
        key: source_quota(claude_source, key, quota_percent(env_name, default=0), text)
        for key, env_name, text in (
            ("3h", "CLAUDE_3H_PERCENT", quota_text("CLAUDE_3H_TEXT", "短周期")),
            ("5h", "CLAUDE_5H_PERCENT", quota_text("CLAUDE_5H_TEXT", "滚动窗口")),
            ("today", "CLAUDE_TODAY_PERCENT", quota_text("CLAUDE_TODAY_TEXT", "今日累计")),
            ("week", "CLAUDE_WEEK_PERCENT", quota_text("CLAUDE_WEEK_TEXT", "周累计")),
        )
    }
    return {
        "codex": [
            {
                "label": "3小时",
                "percent": codex_q["3h"]["percent"],
                "text": codex_q["3h"]["text"],
            },
            {
                "label": "5小时",
                "percent": codex_q["5h"]["percent"],
                "text": codex_q["5h"]["text"],
            },
            {
                "label": "今日",
                "percent": codex_q["today"]["percent"],
                "text": codex_q["today"]["text"],
            },
            {
                "label": "本周",
                "percent": codex_q["week"]["percent"],
                "text": codex_q["week"]["text"],
            },
        ],
        "claude": [
            {
                "label": "3小时",
                "percent": claude_q["3h"]["percent"],
                "text": claude_q["3h"]["text"],
            },
            {
                "label": "5小时",
                "percent": claude_q["5h"]["percent"],
                "text": claude_q["5h"]["text"],
            },
            {
                "label": "今日",
                "percent": claude_q["today"]["percent"],
                "text": claude_q["today"]["text"],
            },
            {
                "label": "本周",
                "percent": claude_q["week"]["percent"],
                "text": claude_q["week"]["text"],
            },
        ],
    }


def collect_data() -> dict[str, Any]:
    now = local_now()
    data: dict[str, Any] = {
        "ok": True,
        "errors": [],
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_cn": chinese_datetime(now),
        "top_time_cn": chinese_date_week_time(now),
        "hostname": socket.gethostname(),
        "kindle": load_state(),
    }

    try:
        prices, charts, market_errors = fetch_market_data()
        data["prices"] = prices
        data["charts"] = charts
        data["errors"].extend(market_errors)
    except Exception as exc:
        data["prices"] = LAST_DATA.get("prices", {})
        data["charts"] = LAST_DATA.get("charts", {})
        data["errors"].append(f"Market data: {type(exc).__name__}: {exc}")

    usage_sources = load_usage_sources()
    openai_source = openai_admin_usage_source()
    anthropic_source = anthropic_admin_usage_source()
    if openai_source:
        usage_sources["codex"] = openai_source
    if anthropic_source:
        usage_sources["claude"] = anthropic_source
    codex_source = usage_sources.get("codex", {})
    claude_source = usage_sources.get("claude", {})
    codex_text = str(codex_source.get("text", "")).strip() if isinstance(codex_source, dict) else ""
    codex_text = codex_text or load_text_env("CODEX_USAGE_TEXT", "CODEX_USAGE_FILE")
    if codex_text:
        data["codex"] = codex_text
    else:
        data["codex"] = "OpenAI API: waiting for admin usage source"

    claude_text = str(claude_source.get("text", "")).strip() if isinstance(claude_source, dict) else ""
    claude_text = claude_text or load_text_env("CLAUDE_USAGE_TEXT", "CLAUDE_USAGE_FILE")
    data["claude"] = claude_text or "Claude Code: waiting for usage source"

    data["usage"] = {
        "codex": usage_block("CODEX", "Codex", str(data["codex"])),
        "claude": usage_block("CLAUDE", "Claude Code", str(data["claude"])),
    }
    data["quotas"] = quota_blocks(usage_sources)

    custom_note = setting("DASHBOARD_NOTE")
    if custom_note:
        data["note"] = custom_note

    return data


def find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = []
    if bold:
        candidates.extend(
            [
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/noto/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
                "C:/Windows/Fonts/NotoSansSC-VF.ttf",
                "C:/Windows/Fonts/simhei.ttf",
                "C:/Windows/Fonts/msyhbd.ttc",
                "C:/Windows/Fonts/arialbd.ttf",
            ]
        )
    candidates.extend(
        [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/NotoSansSC-VF.ttf",
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
    )
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    text = str(text).replace("\n", " ").strip()
    if not text:
        return [""]

    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = word if not current else f"{current} {word}"
        if text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)
            current = word
            continue

        clipped = ""
        for ch in word:
            if text_width(draw, clipped + ch + "...", font) > max_width:
                break
            clipped += ch
        lines.append((clipped or word[:1]) + "...")
        current = ""

    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Kindle 数据屏渲染（600x800 灰度 PNG）
# 以 3 倍分辨率绘制再缩小，得到抗锯齿的线条和文字；最终量化为 16 级灰度，
# 这正是电子墨水屏的原生色阶。DASHBOARD_MONOCHROME=1 可退回纯黑白。
# ---------------------------------------------------------------------------
DASH_SS = 3
GRAY_TRACK = 224   # 进度条底色
GRAY_SOFT = 238    # 走势图面积
GRAY_LINE = 200    # 分隔线
GRAY_MUTED = 96    # 次要文字
DESIGN_W, DESIGN_H = 600, 800


@lru_cache(maxsize=64)
def _font(px: int, bold: bool) -> Any:
    return find_font(px, bold=bold)


class _Canvas:
    """按逻辑像素作画、内部放大 DASH_SS 倍的小画布。"""

    def __init__(self, width: int, height: int) -> None:
        self.img = Image.new("L", (width * DASH_SS, height * DASH_SS), 255)
        self.d = ImageDraw.Draw(self.img)

    @staticmethod
    def _pt(p: tuple[float, float]) -> tuple[float, float]:
        return (p[0] * DASH_SS, p[1] * DASH_SS)

    def rect(self, xy, radius=0, fill=None, outline=None, width=1) -> None:
        x1, y1, x2, y2 = (v * DASH_SS for v in xy)
        self.d.rounded_rectangle(
            (x1, y1, x2 - 1, y2 - 1), radius=radius * DASH_SS, fill=fill,
            outline=outline, width=max(1, round(width * DASH_SS)),
        )

    def line(self, pts, fill=0, width=1, round_caps=False) -> None:
        scaled = [self._pt(p) for p in pts]
        w = max(1, round(width * DASH_SS))
        self.d.line(scaled, fill=fill, width=w, joint="curve")
        if round_caps:
            r = w / 2
            for x, y in (scaled[0], scaled[-1]):
                self.d.ellipse((x - r, y - r, x + r, y + r), fill=fill)

    def dashed(self, x1, x2, y, fill=150, width=1, dash=5, gap=4) -> None:
        x = x1
        while x < x2:
            self.line([(x, y), (min(x + dash, x2), y)], fill=fill, width=width)
            x += dash + gap

    def poly(self, pts, fill) -> None:
        self.d.polygon([self._pt(p) for p in pts], fill=fill)

    def circle(self, cx, cy, r, fill=None, outline=None, width=1) -> None:
        s = DASH_SS
        self.d.ellipse(
            ((cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s),
            fill=fill, outline=outline, width=max(1, round(width * s)),
        )

    def text(self, x, y, s, size, bold=False, fill=0, anchor="ls") -> None:
        self.d.text((x * DASH_SS, y * DASH_SS), str(s), font=_font(round(size * DASH_SS), bold),
                    fill=fill, anchor=anchor)

    def tw(self, s, size, bold=False) -> float:
        return self.d.textlength(str(s), font=_font(round(size * DASH_SS), bold)) / DASH_SS

    def fit(self, s, size, max_w, bold=False) -> str:
        s = str(s).replace("\n", " ").strip()
        if self.tw(s, size, bold) <= max_w:
            return s
        while s and self.tw(s + "…", size, bold) > max_w:
            s = s[:-1]
        return s + "…"

    def finish(self, width: int, height: int) -> Image.Image:
        return self.img.resize((width, height), Image.LANCZOS)


def _data_time(data: dict[str, Any]) -> datetime:
    try:
        return datetime.fromisoformat(str(data.get("generated_at")))
    except Exception:
        return local_now()


def _battery_level(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return clamp(float(str(value).strip().rstrip("%")), 0, 100)
    except ValueError:
        return None


def _draw_header(c: _Canvas, data: dict[str, Any], now: datetime) -> None:
    clock = now.strftime("%H:%M")
    c.text(24, 58, clock, 54, bold=True)
    x = 24 + c.tw(clock, 54, True) + 18
    c.line([(x, 22), (x, 60)], fill=GRAY_LINE, width=2)
    c.text(x + 16, 40, f"{now.month}月{now.day}日", 22, bold=True)
    c.text(x + 16, 60, WEEKDAYS_CN[now.weekday()].replace("周", "星期"), 17, fill=GRAY_MUTED)

    kindle_state = data.get("kindle", {}) if isinstance(data.get("kindle"), dict) else {}
    level = _battery_level(
        os.getenv("KINDLE_BATTERY_TEXT") or kindle_state.get("kindle_battery") or kindle_state.get("battery")
    )
    # 电池图标：右对齐，文字在图标左侧
    bx2 = 576
    bx1 = bx2 - 40
    by1, by2 = 31, 51
    c.rect((bx1, by1, bx2 - 4, by2), radius=4, fill=255, outline=0, width=2)
    c.rect((bx2 - 4, by1 + 6, bx2, by2 - 6), radius=1, fill=0)
    if level is not None and level > 0:
        inner = (bx2 - 4 - 4) - (bx1 + 4)
        c.rect((bx1 + 4, by1 + 4, bx1 + 4 + max(3, inner * level / 100), by2 - 4), radius=1, fill=0)
    label = "--" if level is None else f"{level:.0f}%"
    c.text(bx1 - 10, 48, label, 21, bold=True, anchor="rs")
    c.line([(24, 78), (576, 78)], fill=0, width=2.5)


def _change_pill(c: _Canvas, right: float, cy: float, change: float | None) -> None:
    h = 26
    top, bottom = cy - h / 2, cy + h / 2
    if change is None:
        label = "--"
        w = c.tw(label, 15, True) + 24
        c.rect((right - w, top, right, bottom), radius=h / 2, outline=0, width=2)
        c.text(right - w / 2, cy + 5.5, label, 15, bold=True, anchor="ms")
        return
    up = change >= 0
    label = f"{abs(change):.2f}%"
    tri = 10
    w = 14 + tri + 6 + c.tw(label, 15, True) + 14
    fg = 255 if up else 0
    if up:
        c.rect((right - w, top, right, bottom), radius=h / 2, fill=0)
    else:
        c.rect((right - w, top, right, bottom), radius=h / 2, fill=255, outline=0, width=2)
    tx = right - w + 14
    if up:
        c.poly([(tx + tri / 2, cy - 4.5), (tx, cy + 4), (tx + tri, cy + 4)], fill=fg)
    else:
        c.poly([(tx, cy - 4), (tx + tri, cy - 4), (tx + tri / 2, cy + 4.5)], fill=fg)
    c.text(tx + tri + 6, cy + 5.5, label, 15, bold=True, fill=fg)


def _smooth(values: list[float], limit: int = 96) -> list[float]:
    if len(values) <= limit:
        return values
    step = len(values) / limit
    out = []
    for i in range(limit):
        chunk = values[int(i * step): max(int(i * step) + 1, int((i + 1) * step))]
        out.append(sum(chunk) / len(chunk))
    out[-1] = values[-1]
    return out


def _draw_sparkline(c: _Canvas, box, values: list[float]) -> None:
    x1, y1, x2, y2 = box
    clean = [float(v) for v in values if isinstance(v, (int, float))]
    if len(clean) < 2:
        c.text((x1 + x2) / 2, (y1 + y2) / 2 + 5, "等待走势数据", 14, fill=GRAY_MUTED, anchor="ms")
        return
    clean = _smooth(clean)
    lo, hi = min(clean), max(clean)
    span = (hi - lo) or max(abs(hi), 1.0) * 0.01
    pad = 5
    top, bottom = y1 + pad, y2 - pad

    def y_of(v: float) -> float:
        return bottom - (v - lo) / span * (bottom - top)

    pts = [(x1 + i * (x2 - x1 - 6) / (len(clean) - 1), y_of(v)) for i, v in enumerate(clean)]
    c.poly(pts + [(pts[-1][0], y2), (pts[0][0], y2)], fill=GRAY_SOFT)
    c.dashed(x1, x2, y_of(clean[0]), fill=150)          # 24h 前的起点价位
    c.line(pts, fill=0, width=2.6, round_caps=True)
    px, py = pts[-1]
    c.circle(px, py, 6, fill=255)
    c.circle(px, py, 4.2, fill=0)


def _draw_coin(c: _Canvas, box, symbol: str, name: str, quote: dict[str, Any], values: list[float]) -> None:
    x1, y1, x2, y2 = box
    c.rect(box, radius=16, fill=255, outline=0, width=2)
    c.text(x1 + 16, y1 + 36, symbol, 22, bold=True)
    c.text(x1 + 16 + c.tw(symbol, 22, True) + 8, y1 + 36, name, 14, fill=GRAY_MUTED)
    _change_pill(c, x2 - 14, y1 + 28, safe_float(quote.get("eur_24h_change")))

    price = money(quote.get("eur"))
    c.text(x1 + 16, y1 + 82, price, 34, bold=True)
    c.text(x1 + 16 + c.tw(price, 34, True) + 7, y1 + 82, "EUR", 15, fill=GRAY_MUTED)

    _draw_sparkline(c, (x1 + 16, y1 + 94, x2 - 16, y2 - 34), values)

    clean = [float(v) for v in values if isinstance(v, (int, float))]
    if len(clean) >= 2:
        rng = f"24h  {money(min(clean))} – {money(max(clean))}"
        c.text(x1 + 16, y2 - 13, rng, 13, fill=GRAY_MUTED)


def _draw_quota_row(c: _Canvas, x1: float, x2: float, top: float, item: dict[str, Any]) -> None:
    label = str(item.get("label", "--"))
    note = str(item.get("text", ""))
    raw = item.get("percent")
    percent = clamp(float(raw or 0), 0, 100)
    pct_text = f"{percent:.0f}%" if raw is not None else "实时"

    left_w = 100
    c.text(x1, top + 17, label, 18, bold=True)
    if note:
        c.text(x1, top + 34, c.fit(note, 13, left_w), 13, fill=GRAY_MUTED)

    pct_w = 72   # 为 "100%" 反白胶囊预留
    bx1, bx2 = x1 + left_w + 6, x2 - pct_w - 8
    by1, by2 = top + 13, top + 27
    c.rect((bx1, by1, bx2, by2), radius=7, fill=GRAY_TRACK)
    if percent > 0:
        c.rect((bx1, by1, bx1 + max(14, (bx2 - bx1) * percent / 100), by2), radius=7, fill=0)

    if raw is not None and percent >= 80:       # 高用量：反白胶囊提醒
        w = c.tw(pct_text, 22, True) + 14
        c.rect((x2 - w, top + 4, x2, top + 30), radius=13, fill=0)
        c.text(x2 - w / 2, top + 26, pct_text, 22, bold=True, fill=255, anchor="ms")
    else:
        c.text(x2, top + 26, pct_text, 22, bold=True, anchor="rs")


def _draw_quota_card(c: _Canvas, box, chip: str, suffix: str, items: list[dict[str, Any]]) -> None:
    x1, y1, x2, y2 = box
    c.rect(box, radius=16, fill=255, outline=0, width=2)
    chip_w = c.tw(chip, 17, True) + 22
    c.rect((x1 + 18, y1 + 14, x1 + 18 + chip_w, y1 + 40), radius=13, fill=0)
    c.text(x1 + 18 + chip_w / 2, y1 + 33, chip, 17, bold=True, fill=255, anchor="ms")
    c.text(x1 + 18 + chip_w + 10, y1 + 34, suffix, 18, bold=True)
    c.line([(x1 + 18, y1 + 50), (x2 - 18, y1 + 50)], fill=GRAY_LINE, width=1.5)

    items = list(items)[:4]
    while len(items) < 4:
        items.append({"label": "--", "percent": 0, "text": "未配置"})
    for i, item in enumerate(items):
        _draw_quota_row(c, x1 + 20, x2 - 20, y1 + 58 + i * 40, item)


def render_dashboard(data: dict[str, Any]) -> bytes:
    c = _Canvas(DESIGN_W, DESIGN_H)
    now = _data_time(data)
    _draw_header(c, data, now)

    prices = data.get("prices", {}) or {}
    charts = data.get("charts", {}) or {}
    _draw_coin(c, (24, 94, 288, 268), "BTC", "Bitcoin", prices.get("bitcoin", {}) or {}, list(charts.get("bitcoin", [])))
    _draw_coin(c, (312, 94, 576, 268), "ETH", "Ethereum", prices.get("ethereum", {}) or {}, list(charts.get("ethereum", [])))

    quotas = data.get("quotas", {}) if isinstance(data.get("quotas"), dict) else quota_blocks()
    _draw_quota_card(c, (24, 282, 576, 508), "OpenAI", "API 用量", list(quotas.get("codex", [])))
    _draw_quota_card(c, (24, 520, 576, 746), "Claude", "API 用量", list(quotas.get("claude", [])))

    c.line([(24, 759), (576, 759)], fill=0, width=2)
    stamp = f"更新于 {now.strftime('%m-%d %H:%M')}"
    if data.get("errors"):
        stamp += "  ·  部分数据获取失败"
    c.text(24, 785, stamp, 15, fill=GRAY_MUTED)
    eth_cny = safe_float(((prices.get("ethereum") or {}) if isinstance(prices, dict) else {}).get("cny"))
    c.text(576, 785, "ETH ¥--" if eth_cny is None else f"ETH ¥{money(eth_cny)}", 15, bold=True, anchor="rs")

    img = c.finish(DESIGN_W, DESIGN_H)
    if (WIDTH, HEIGHT) != (DESIGN_W, DESIGN_H):
        img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    if os.getenv("DASHBOARD_MONOCHROME", "").strip().lower() in ("1", "true", "yes"):
        img = img.point(lambda p: 255 if p > 180 else 0)
    else:
        img = img.point(lambda p: min(255, round(p / 17) * 17))   # 量化到 Kindle 的 16 级灰度
    # 始终保存为 8 位灰度 PNG：旧版 Kindle 的 eips 不接受 1 位 PNG（"8bit only"）。
    out = io.BytesIO()
    img.convert("L").save(out, format="PNG", optimize=True)
    return out.getvalue()


def get_cached_image() -> bytes:
    """返回缓存的数据屏图片。

    - 数据（行情、用量）按 CACHE_SECONDS 过期后才重新拉取；
    - Kindle 上报电量（/update）只会让图片重绘，不会重新请求上游接口；
    - 加锁，避免多个请求同时过期时重复请求上游。
    """
    global LAST_DATA, LAST_IMAGE, LAST_RENDER_TS
    with RENDER_LOCK:
        now = time.time()
        cache_seconds = setting_float("CACHE_SECONDS") or CACHE_SECONDS
        # After boot the proxy may need several seconds to load its rule database.
        # Retry failed/empty market renders quickly instead of caching them for 5 minutes.
        effective_cache = cache_seconds if LAST_DATA.get("prices") else min(cache_seconds, 15)
        data_expired = not LAST_DATA or now - LAST_RENDER_TS > effective_cache
        if LAST_IMAGE is None or data_expired:
            try:
                if data_expired:
                    LAST_DATA = collect_data()
                    LAST_RENDER_TS = now
                else:
                    LAST_DATA["kindle"] = load_state()
                LAST_IMAGE = render_dashboard(LAST_DATA)
            except Exception as exc:
                LAST_DATA = {"ok": False, "errors": [traceback.format_exc()], "generated_at": datetime.now().isoformat()}
                LAST_IMAGE = render_dashboard({"errors": [str(exc)], "generated_at": datetime.now().isoformat()})
                LAST_RENDER_TS = now
        return LAST_IMAGE


ADMIN_NON_SECRET_FIELDS = (
    "PROXY_URL",
    "COIN_PROVIDER_ORDER",
    "CACHE_SECONDS",
    "DASHBOARD_NOTE",
    "OPENAI_3H_TOKEN_BUDGET",
    "OPENAI_5H_TOKEN_BUDGET",
    "OPENAI_TODAY_TOKEN_BUDGET",
    "OPENAI_WEEK_TOKEN_BUDGET",
    "ANTHROPIC_3H_TOKEN_BUDGET",
    "ANTHROPIC_5H_TOKEN_BUDGET",
    "ANTHROPIC_TODAY_TOKEN_BUDGET",
    "ANTHROPIC_WEEK_TOKEN_BUDGET",
)
ADMIN_SECRET_FIELDS = ("OPENAI_ADMIN_KEY", "ANTHROPIC_ADMIN_KEY", "MIHOMO_SUBSCRIPTION_URL")


def current_proxy_mode() -> str:
    """读取代理内核当前的运行模式，重新生成配置时保持不变。"""
    try:
        found = re.search(r"^mode:\s*(rule|global|direct)\s*$", MIHOMO_CONFIG_FILE.read_text(encoding="utf-8"), re.M)
        return found.group(1) if found else "rule"
    except Exception:
        return "rule"


def mihomo_subscription_config(subscription_url: str, mode: str = "rule") -> str:
    # JSON quoting is valid YAML quoting and safely preserves URL query parameters.
    escaped_url = json.dumps(subscription_url, ensure_ascii=False)
    telegram_rules = "\n".join("  - " + rule for rule in telegram_rule_lines())
    return f"""mixed-port: 7890
allow-lan: true
bind-address: "*"
mode: {mode}
log-level: warning
proxy-providers:
  airport:
    type: http
    url: {escaped_url}
    path: ./providers/airport.yaml
    interval: 86400
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 3600
      lazy: false
      timeout: 5000
      expected-status: 204
    header:
      User-Agent:
        - ClashMetaForAndroid/2.0
proxy-groups:
  - name: 数据屏代理
    type: select
    proxies:
      - 自动选择
    use:
      - airport
    exclude-filter: "一元机场|请立即|剩余流量|套餐到期"
  - name: 自动选择
    type: url-test
    url: https://www.gstatic.com/generate_204
    interval: 3600
    lazy: false
    timeout: 5000
    expected-status: 204
    tolerance: 0
    use:
      - airport
    exclude-filter: "一元机场|请立即|剩余流量|套餐到期"
  - name: WhatsApp代理
    type: select
    proxies:
      - 自动选择
    use:
      - airport
    exclude-filter: "一元机场|请立即|剩余流量|套餐到期"
  - name: Telegram代理
    type: select
    proxies:
      - 自动选择
    use:
      - airport
    exclude-filter: "一元机场|请立即|剩余流量|套餐到期"
rules:
{telegram_rules}
  - DOMAIN-SUFFIX,whatsapp.net,WhatsApp代理
  - MATCH,数据屏代理
"""


def admin_password_configured() -> bool:
    """未设置，或仍是 .env.example 里的占位值 change-me，都视为未配置。"""
    return os.getenv("ADMIN_PASSWORD", "") not in ("", "change-me")


def admin_authorized(headers: Any) -> bool:
    expected_user = os.getenv("ADMIN_USERNAME", "admin")
    expected_password = os.getenv("ADMIN_PASSWORD", "")
    if not admin_password_configured():
        return False
    auth = headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return False
    # compare_digest 对含非 ASCII 的 str 会抛 TypeError，所以统一按字节比较。
    return hmac.compare_digest(username.encode("utf-8"), expected_user.encode("utf-8")) and hmac.compare_digest(
        password.encode("utf-8"), expected_password.encode("utf-8")
    )


def html_input(name: str, label: str, value: str = "", input_type: str = "text", placeholder: str = "") -> str:
    return (
        '<label class="field"><span>' + html.escape(label) + "</span>"
        f'<input type="{html.escape(input_type)}" name="{html.escape(name)}" '
        f'value="{html.escape(value, quote=True)}" placeholder="{html.escape(placeholder, quote=True)}"></label>'
    )


def admin_page(saved: bool = False) -> bytes:
    config = load_runtime_config()
    prices = LAST_DATA.get("prices", {}) if isinstance(LAST_DATA.get("prices"), dict) else {}
    btc = prices.get("bitcoin", {}) if isinstance(prices.get("bitcoin"), dict) else {}
    market_source = str(btc.get("source") or "尚未刷新")
    errors = LAST_DATA.get("errors", []) if isinstance(LAST_DATA.get("errors"), list) else []
    error_text = "\n".join(str(item) for item in errors[-8:]) or "暂无错误记录"
    proxy_file_status = (
        f"已保存（{MIHOMO_CONFIG_FILE.stat().st_size} 字节）" if MIHOMO_CONFIG_FILE.exists() else "尚未保存"
    )

    budget_fields = []
    for prefix, title in (("OPENAI", "OpenAI"), ("ANTHROPIC", "Anthropic")):
        budget_fields.append(f'<h3>{title} token 预算（可选）</h3><div class="grid four">')
        for period, label in (("3H", "3小时"), ("5H", "5小时"), ("TODAY", "今日"), ("WEEK", "7天")):
            name = f"{prefix}_{period}_TOKEN_BUDGET"
            budget_fields.append(html_input(name, label, str(config.get(name, "")), "number", "留空则显示实时数值"))
        budget_fields.append("</div>")

    openai_saved = bool(setting("OPENAI_ADMIN_KEY"))
    anthropic_saved = bool(setting("ANTHROPIC_ADMIN_KEY"))
    subscription_saved = bool(setting("MIHOMO_SUBSCRIPTION_URL"))
    notice = '<div class="notice ok">配置已保存，下一次刷新立即生效。</div>' if saved else ""
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kindle 数据屏管理</title><style>
:root{{--bg:#f4f1e8;--card:#fffdf8;--ink:#24231f;--muted:#6d6a61;--line:#d7d1c3;--accent:#24231f}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif}}
.wrap{{max-width:1040px;margin:0 auto;padding:28px 18px 60px}} header{{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:22px}}
h1{{font-size:30px;margin:0}} .sub{{color:var(--muted);margin-top:6px}} .links a{{color:var(--ink);margin-left:14px}}
.status{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}} .pill,.card{{background:var(--card);border:1px solid var(--line);border-radius:15px}}
.pill{{padding:14px 16px}} .pill b{{display:block;font-size:13px;color:var(--muted);margin-bottom:5px}} .card{{padding:22px;margin:14px 0}}
h2{{font-size:21px;margin:0 0 16px}} h3{{font-size:15px;margin:20px 0 10px}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}} .grid.four{{grid-template-columns:repeat(4,1fr)}}
.field span{{display:block;font-size:13px;font-weight:650;margin-bottom:6px}} input,textarea{{width:100%;border:1px solid var(--line);background:white;border-radius:10px;padding:11px 12px;font:inherit;color:var(--ink)}}
textarea{{min-height:170px;resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:13px}} .hint{{font-size:13px;color:var(--muted);line-height:1.6}}
.secret{{display:flex;gap:10px;align-items:end}} .secret .field{{flex:1}} .check{{white-space:nowrap;padding-bottom:10px;font-size:13px}} .check input{{width:auto}}
button{{background:var(--accent);color:white;border:0;border-radius:11px;padding:13px 22px;font-size:15px;font-weight:700;cursor:pointer}} pre{{white-space:pre-wrap;word-break:break-word;background:#efede6;padding:13px;border-radius:10px;font-size:12px}}
.notice{{padding:12px 15px;border-radius:10px;margin:12px 0}} .ok{{background:#e5f1e6;color:#174a21}} @media(max-width:760px){{.status,.grid,.grid.four{{grid-template-columns:1fr}} header{{display:block}}.links{{margin-top:12px}}}}
</style></head><body><div class="wrap">
<header><div><h1>Kindle 数据屏管理</h1><div class="sub">飞牛 NAS · 局域网后台</div></div><div class="links"><a href="/dashboard.png">查看面板</a><a href="/data.json">原始数据</a></div></header>
{notice}
{proxy_admin_section()}
<div class="status"><div class="pill"><b>当前行情节点</b>{html.escape(market_source)}</div><div class="pill"><b>代理地址</b>{html.escape(setting('PROXY_URL') or '未配置')}</div><div class="pill"><b>机场配置</b>{html.escape(proxy_file_status)}</div></div>
<form method="post" action="/admin">
<section class="card"><h2>账号用量接口</h2>
<div class="secret">{html_input('OPENAI_ADMIN_KEY','OpenAI Admin Key','','password','已保存' if openai_saved else '粘贴管理员 Key')}<label class="check"><input type="checkbox" name="CLEAR_OPENAI_ADMIN_KEY" value="1"> 清除</label></div>
<div class="secret">{html_input('ANTHROPIC_ADMIN_KEY','Anthropic Admin Key','','password','已保存' if anthropic_saved else '粘贴管理员 Key')}<label class="check"><input type="checkbox" name="CLEAR_ANTHROPIC_ADMIN_KEY" value="1"> 清除</label></div>
<p class="hint">Key 只保存在 NAS 的 /data/dashboard_config.json；页面不会回显完整内容。必须使用组织 Admin Key。</p>
{''.join(budget_fields)}</section>
<section class="card"><h2>网络与行情</h2><div class="grid">
{html_input('PROXY_URL','HTTP 代理地址',str(config.get('PROXY_URL','')),'text','例如 http://mihomo:7890')}
{html_input('COIN_PROVIDER_ORDER','行情节点顺序',str(config.get('COIN_PROVIDER_ORDER','okx,cryptocompare,coingecko')),'text','okx,cryptocompare,coingecko')}
{html_input('CACHE_SECONDS','刷新缓存（秒）',str(config.get('CACHE_SECONDS','300')),'number','300')}
{html_input('DASHBOARD_NOTE','面板备注',str(config.get('DASHBOARD_NOTE','')),'text','可留空')}
</div><p class="hint">使用机场时建议代理地址填 http://mihomo:7890；该地址供数据屏容器使用；局域网其他设备请使用上方 NAS 地址。</p></section>
<section class="card"><h2>机场配置</h2>
<div class="secret">{html_input('MIHOMO_SUBSCRIPTION_URL','订阅链接','','password','已保存' if subscription_saved else '粘贴一元机场订阅链接')}<label class="check"><input type="checkbox" name="CLEAR_MIHOMO_SUBSCRIPTION_URL" value="1"> 清除</label></div>
<p class="hint">直接粘贴一元机场的“Clash/Mihomo 订阅链接”即可。后台会自动生成代理配置；订阅链接优先于下方 YAML。</p>
<textarea name="MIHOMO_YAML" placeholder="高级选项：粘贴完整 Clash/Mihomo YAML；留空表示保持现有配置"></textarea>
<p class="hint">订阅链接或 YAML 都含敏感凭据，请只在局域网后台粘贴，不要发送到聊天中。保存时自动校验并重载内核，无需手动重启；旧配置会自动备份。</p></section>
<section class="card"><h2>最近错误</h2><pre>{html.escape(error_text)}</pre></section>
<button type="submit">保存配置并刷新缓存</button></form></div></body></html>"""
    return enhance_admin_page(page).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    timeout = 60  # 慢速/挂起的连接不会无限占用线程

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), fmt % args), flush=True)

    def do_GET(self) -> None:
        global LAST_IMAGE
        if management_handle(self, admin_authorized):
            return
        if proxy_handle(self, admin_authorized):
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/admin":
            if not admin_password_configured():
                body = "请先在 .env 设置 ADMIN_PASSWORD（不能为空，也不能是 change-me），再重启容器。".encode("utf-8")
                self.send_response(503)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not admin_authorized(self.headers):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Kindle Dashboard Admin", charset="UTF-8"')
                self.end_headers()
                return
            body = admin_page(saved=urllib.parse.parse_qs(parsed.query).get("saved") == ["1"])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ("/", "/index.html"):
            body = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>NAS 数据屏与代理</title><style>body{font-family:system-ui,Microsoft YaHei;margin:0;background:#f4f1e8;color:#24231f}.wrap{max-width:950px;margin:40px auto;padding:20px}.card{padding:24px;background:#fffdf8;border:1px solid #d7d1c3;border-radius:16px;margin:18px 0}a{color:#237847}img{max-width:100%;max-height:600px;border-radius:12px}code{overflow-wrap:anywhere}.button{display:inline-block;background:#24231f;color:white;padding:14px 22px;border-radius:10px;text-decoration:none}</style>'
                '<div class="wrap"><h1>NAS 数据屏与局域网代理</h1><p>行情数据屏 · 节点管理 · Telegram MTProto · WhatsApp</p><p><a href="https://github.com/Viper-Boss/kindle-dashboard-nas">项目源码（AGPL-3.0）</a></p>'
                '<div class="card"><h2>代理管理</h2><p>查看节点、切换线路、测试延迟、更新订阅和管理活动连接。</p><a class="button" href="/admin">打开管理后台</a>'
                '<p>混合代理 <code>192.168.120.200:7890</code> · SOCKS5 <code>192.168.120.200:7891</code> · HTTP <code>192.168.120.200:7892</code></p></div>'
                '<div class="card"><h2>数据屏预览</h2><p><a href="/dashboard.png">查看原图</a> · <a href="/data.json">原始数据</a></p><img src="/dashboard.png" alt="Kindle 数据屏"></div></div></html>').encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/update") or path.startswith("/kindle-state"):
            query = urllib.parse.parse_qs(parsed.query)
            updates = {
                "kindle_battery": (query.get("kindle_battery") or query.get("battery") or [""])[0],
                "kindle_ip": self.client_address[0],
            }
            state = save_state(updates)
            LAST_IMAGE = None
            body = json.dumps({"ok": True, "state": state}, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/dashboard.png"):
            image = get_cached_image()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(image)))
            self.end_headers()
            self.wfile.write(image)
            return

        if path.startswith("/healthz"):
            # 健康检查只确认进程存活，不触发行情/用量接口请求。
            body = json.dumps({"ok": True, "last_render_age": round(time.time() - LAST_RENDER_TS) if LAST_RENDER_TS else None}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/data.json"):
            get_cached_image()
            body = json.dumps(LAST_DATA, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        global LAST_IMAGE, LAST_RENDER_TS
        if management_handle(self, admin_authorized):
            return
        if proxy_handle(self, admin_authorized):
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/admin":
            self.send_response(404)
            self.end_headers()
            return
        if not admin_authorized(self.headers):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Kindle Dashboard Admin", charset="UTF-8"')
            self.end_headers()
            return
        origin = self.headers.get("Origin", "")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host", ""):
            self.send_response(403)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 2_000_000:
            self.send_response(413)
            self.end_headers()
            return
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
        previous_config = load_runtime_config()
        config = dict(previous_config)
        for name in ADMIN_NON_SECRET_FIELDS:
            value = (form.get(name) or [""])[0].strip()
            if value:
                config[name] = value
            else:
                config.pop(name, None)
        for name in ADMIN_SECRET_FIELDS:
            if (form.get(f"CLEAR_{name}") or [""])[0] == "1":
                config.pop(name, None)
                continue
            value = (form.get(name) or [""])[0].strip()
            if value:
                config[name] = value
        # 只有这次确实粘贴了新的订阅链接或 YAML 才改动代理配置；
        # 仅修改预算等其他设置时不再重写配置，避免把界面里切换的运行模式重置掉。
        cleared_subscription = (form.get("CLEAR_MIHOMO_SUBSCRIPTION_URL") or [""])[0] == "1"
        new_subscription = "" if cleared_subscription else (form.get("MIHOMO_SUBSCRIPTION_URL") or [""])[0].strip()
        yaml_text = (form.get("MIHOMO_YAML") or [""])[0].strip()
        if new_subscription:
            generated_proxy_config = mihomo_subscription_config(new_subscription, current_proxy_mode())
        else:
            generated_proxy_config = yaml_text
        if generated_proxy_config:
            try:
                save_and_reload(generated_proxy_config)
            except Exception:
                # 代理配置失败时，其余设置照常保存，只放弃这次的订阅链接。
                if new_subscription:
                    if previous_config.get("MIHOMO_SUBSCRIPTION_URL"):
                        config["MIHOMO_SUBSCRIPTION_URL"] = previous_config["MIHOMO_SUBSCRIPTION_URL"]
                    else:
                        config.pop("MIHOMO_SUBSCRIPTION_URL", None)
                save_runtime_config(config)
                LAST_IMAGE = None
                LAST_RENDER_TS = 0
                body = "代理配置校验或重载失败，已恢复旧配置；其余设置已保存。请检查订阅链接或 YAML 后重试。".encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        save_runtime_config(config)
        LAST_IMAGE = None
        LAST_RENDER_TS = 0
        self.send_response(303)
        self.send_header("Location", "/admin?saved=1")
        self.end_headers()


def main() -> None:
    import signal
    traffic_stats.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    print(f"Kindle dashboard server listening on 0.0.0.0:{PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        traffic_stats.stop()
        server.server_close()


if __name__ == "__main__":
    main()
