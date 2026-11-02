#!/usr/bin/env python3
import base64
import hmac
import html
import io
import json
import os
import socket
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from proxy_admin import section as proxy_admin_section, handle as proxy_handle, save_and_reload, managed_config


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


def load_runtime_config() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_runtime_config(config: dict[str, Any]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(CONFIG_FILE)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


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
    state = load_state()
    state.update({k: v for k, v in updates.items() if v is not None and v != ""})
    state["updated_at"] = local_now().isoformat(timespec="seconds")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
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


def draw_box(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], title: str, lines: list[str]) -> None:
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=12, outline=0, width=3, fill=255)
    title_font = find_font(26, bold=True)
    body_font = find_font(21)
    draw.text((x1 + 18, y1 + 12), title, font=title_font, fill=0)
    y = y1 + 54
    max_width = x2 - x1 - 36
    line_height = 28
    for line in lines:
        for wrapped in wrap_text(draw, line, body_font, max_width):
            if y + line_height > y2 - 10:
                return
            draw.text((x1 + 18, y), wrapped, font=body_font, fill=0)
            y += line_height


def draw_card(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], title: str) -> None:
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=14, outline=0, width=3, fill=255)
    draw.text((x1 + 18, y1 + 12), title, font=find_font(26, bold=True), fill=0)


def draw_dashed_line(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[int, int]],
    width: int = 3,
    dash_every: int = 2,
) -> None:
    for idx in range(len(points) - 1):
        if idx % dash_every == 0:
            draw.line((points[idx], points[idx + 1]), fill=0, width=width)


def draw_sparkline(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    values: list[float],
    name: str,
    price_text: str,
    change_text: str,
    dashed: bool = False,
) -> None:
    x1, y1, x2, y2 = xy
    label_font = find_font(22, bold=True)
    body_font = find_font(20)
    tiny_font = find_font(15)

    draw.text((x1, y1), name, font=label_font, fill=0)
    right = f"{price_text}  {change_text}"
    draw.text((x2 - text_width(draw, right, body_font), y1 + 2), right, font=body_font, fill=0)

    chart_top = y1 + 32
    chart_bottom = y2
    draw.line((x1, chart_bottom, x2, chart_bottom), fill=0, width=2)
    draw.line((x1, chart_top, x1, chart_bottom), fill=0, width=2)
    draw.line((x1, (chart_top + chart_bottom) // 2, x2, (chart_top + chart_bottom) // 2), fill=180, width=1)

    clean = [v for v in values if isinstance(v, (int, float))]
    if len(clean) < 2:
        draw.text((x1 + 12, chart_top + 12), "waiting for chart data", font=tiny_font, fill=0)
        return

    lo = min(clean)
    hi = max(clean)
    span = hi - lo or max(abs(hi), 1.0) * 0.01
    points: list[tuple[int, int]] = []
    for idx, value in enumerate(clean):
        x = int(x1 + idx * (x2 - x1) / max(1, len(clean) - 1))
        y = int(chart_bottom - (value - lo) / span * (chart_bottom - chart_top))
        points.append((x, y))

    if dashed:
        draw_dashed_line(draw, points, width=3)
    else:
        draw.line(points, fill=0, width=3)

    draw.ellipse((points[-1][0] - 4, points[-1][1] - 4, points[-1][0] + 4, points[-1][1] + 4), fill=0)
    range_text = f"{money(lo)}-{money(hi)}"
    draw.text((x2 - text_width(draw, range_text, tiny_font), chart_bottom - 18), range_text, font=tiny_font, fill=0)


def draw_usage_bar(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], usage: dict[str, Any]) -> None:
    x1, y1, x2, y2 = xy
    label_font = find_font(22, bold=True)
    body_font = find_font(18)
    label = str(usage.get("label", "--"))
    percent = usage.get("percent")
    text = str(usage.get("text", ""))

    draw.text((x1, y1), label, font=label_font, fill=0)
    if percent is None:
        pct_text = "--"
    else:
        pct_text = f"{percent:.0f}%"
    draw.text((x2 - text_width(draw, pct_text, label_font), y1), pct_text, font=label_font, fill=0)

    if text:
        line = wrap_text(draw, text, body_font, x2 - x1)[0]
        draw.text((x1, y1 + 28), line, font=body_font, fill=0)

    bar_top = y1 + 54
    bar_bottom = y1 + 80
    draw.rounded_rectangle((x1, bar_top, x2, bar_bottom), radius=8, outline=0, width=2, fill=255)
    for tick in (25, 50, 75):
        tx = int(x1 + (x2 - x1) * tick / 100)
        draw.line((tx, bar_top, tx, bar_bottom), fill=0, width=1)
    if percent is not None:
        fill_right = int(x1 + (x2 - x1) * clamp(percent, 0, 100) / 100)
        if fill_right > x1 + 4:
            draw.rounded_rectangle((x1 + 3, bar_top + 3, fill_right, bar_bottom - 3), radius=6, fill=0)
    else:
        draw.line((x1 + 8, (bar_top + bar_bottom) // 2, x2 - 8, (bar_top + bar_bottom) // 2), fill=0, width=2)


def draw_mini_sparkline(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    values: list[float],
    dashed: bool = False,
) -> None:
    x1, y1, x2, y2 = xy
    clean = [float(v) for v in values if isinstance(v, (int, float))]
    if len(clean) < 2:
        draw.text((x1 + 4, y1 + 8), "等待数据", font=find_font(14), fill=0)
        return

    lo = min(clean)
    hi = max(clean)
    span = hi - lo or 1
    points: list[tuple[int, int]] = []
    for idx, value in enumerate(clean):
        x = int(x1 + idx * (x2 - x1) / max(1, len(clean) - 1))
        y = int(y2 - (value - lo) / span * (y2 - y1))
        points.append((x, y))

    draw.line((x1, (y1 + y2) // 2, x2, (y1 + y2) // 2), fill=160, width=1)
    if dashed:
        draw_dashed_line(draw, points, width=3)
    else:
        draw.line(points, fill=0, width=3)
    px, py = points[-1]
    draw.ellipse((px - 4, py - 4, px + 4, py + 4), fill=0)


def draw_coin_tile(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    name: str,
    price: str,
    change: str,
    values: list[float],
    dashed: bool = False,
) -> None:
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=13, outline=0, width=3, fill=255)
    draw.text((x1 + 16, y1 + 12), name, font=find_font(28, bold=True), fill=0)
    draw.text((x1 + 16, y1 + 50), price, font=find_font(23, bold=True), fill=0)
    draw.text((x1 + 16, y1 + 81), change, font=find_font(17), fill=0)
    draw_mini_sparkline(draw, (x1 + 16, y1 + 112, x2 - 16, y2 - 18), values, dashed=dashed)


def draw_quota_bar(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    item: dict[str, Any],
    compact: bool = False,
) -> None:
    label_font = find_font(18 if compact else 19, bold=True)
    text_font = find_font(13 if compact else 14)
    label = str(item.get("label", "--"))
    raw_percent = item.get("percent")
    percent = clamp(float(raw_percent or 0), 0, 100)
    note = str(item.get("text", ""))
    pct_text = f"{percent:.0f}%" if raw_percent is not None else "实时"

    draw.text((x, y), label, font=label_font, fill=0)
    draw.text((x + width - text_width(draw, pct_text, label_font), y), pct_text, font=label_font, fill=0)
    if note:
        draw.text((x, y + 23), note[:16], font=text_font, fill=0)

    bar_top = y + 45
    bar_bottom = y + 69
    draw.rounded_rectangle((x, bar_top, x + width, bar_bottom), radius=7, outline=0, width=2, fill=255)
    for tick in (25, 50, 75):
        tx = x + int(width * tick / 100)
        draw.line((tx, bar_top, tx, bar_bottom), fill=0, width=1)
    if percent > 0:
        fill_right = x + int(width * percent / 100)
        draw.rounded_rectangle((x + 3, bar_top + 3, max(x + 5, fill_right), bar_bottom - 3), radius=5, fill=0)


def draw_quota_section(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    title: str,
    items: list[dict[str, Any]],
) -> None:
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=13, outline=0, width=3, fill=255)
    draw.text((x1 + 16, y1 + 10), title, font=find_font(22, bold=True), fill=0)
    while len(items) < 4:
        items.append({"label": "--", "percent": 0, "text": "未配置"})
    if len(items) >= 4:
        draw_quota_bar(draw, x1 + 22, y1 + 48, 226, items[0], compact=True)
        draw_quota_bar(draw, x1 + 282, y1 + 48, 226, items[1], compact=True)
        draw_quota_bar(draw, x1 + 22, y1 + 128, 226, items[2], compact=True)
        draw_quota_bar(draw, x1 + 282, y1 + 128, 226, items[3], compact=True)
    else:
        draw_quota_bar(draw, x1 + 22, y1 + 48, 226, items[0], compact=True)
        draw_quota_bar(draw, x1 + 282, y1 + 48, 226, items[1], compact=True)
        draw_quota_bar(draw, x1 + 22, y1 + 128, 486, items[2], compact=True)


def render_dashboard(data: dict[str, Any]) -> bytes:
    img = Image.new("L", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(img)
    title_font = find_font(31, bold=True)
    top_font = find_font(17)
    bottom_font = find_font(17)

    top_time = str(data.get("top_time_cn") or chinese_date_week_time(local_now()))
    kindle_state = data.get("kindle", {}) if isinstance(data.get("kindle"), dict) else {}
    battery = os.getenv("KINDLE_BATTERY_TEXT") or kindle_state.get("kindle_battery") or kindle_state.get("battery")
    if battery is None or battery == "":
        battery_text = "电量：未获取"
    else:
        battery_text = f"电量：{battery}" if str(battery).endswith("%") else f"电量：{battery}%"

    draw.text((24, 16), "Kindle数据屏", font=title_font, fill=0)
    draw.text(((WIDTH - text_width(draw, top_time, top_font)) // 2, 29), top_time, font=top_font, fill=0)
    draw.text((WIDTH - 24 - text_width(draw, battery_text, top_font), 29), battery_text, font=top_font, fill=0)
    draw.line((24, 66, WIDTH - 24, 66), fill=0, width=3)

    prices = data.get("prices", {}) or {}
    btc = prices.get("bitcoin", {})
    eth = prices.get("ethereum", {})
    charts = data.get("charts", {}) or {}

    draw_coin_tile(
        draw,
        (24, 84, 288, 256),
        "BTC",
        f"{money(btc.get('eur'))} EUR",
        f"{pct(btc.get('eur_24h_change'))} / 24h",
        list(charts.get("bitcoin", [])),
        dashed=False,
    )
    draw_coin_tile(
        draw,
        (312, 84, 576, 256),
        "ETH",
        f"{money(eth.get('eur'))} EUR",
        f"{pct(eth.get('eur_24h_change'))} / 24h",
        list(charts.get("ethereum", [])),
        dashed=True,
    )

    quotas = data.get("quotas", {}) if isinstance(data.get("quotas"), dict) else quota_blocks()
    draw_quota_section(draw, (24, 276, 576, 502), "OpenAI API用量", list(quotas.get("codex", [])))
    draw_quota_section(draw, (24, 518, 576, 744), "Claude API用量", list(quotas.get("claude", [])))

    draw.line((24, 756, WIDTH - 24, 756), fill=0, width=2)
    info_time = str(data.get("generated_at_cn") or chinese_datetime(local_now()))
    footer = f"信息获取时间：{info_time}"
    draw.text((24, 770), footer, font=bottom_font, fill=0)
    eth_cny = "ETH人民币：--"
    if eth.get("cny") is not None:
        eth_cny = f"ETH人民币：{money(eth.get('cny'))}元"
    draw.text((WIDTH - 24 - text_width(draw, eth_cny, bottom_font), 770), eth_cny, font=bottom_font, fill=0)

    # Keep pure black/white pixels but encode the PNG as 8-bit grayscale.
    # Older Kindle eips rejects 1-bit PNG files with "8bit only".
    img = img.point(lambda p: 255 if p > 180 else 0).convert("L")
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def get_cached_image() -> bytes:
    global LAST_DATA, LAST_IMAGE, LAST_RENDER_TS
    now = time.time()
    cache_seconds = setting_float("CACHE_SECONDS") or CACHE_SECONDS
    # After boot the proxy may need several seconds to load its rule database.
    # Retry failed/empty market renders quickly instead of caching them for 5 minutes.
    effective_cache = cache_seconds if LAST_DATA.get("prices") else min(cache_seconds, 15)
    if LAST_IMAGE is None or now - LAST_RENDER_TS > effective_cache:
        try:
            LAST_DATA = collect_data()
            LAST_IMAGE = render_dashboard(LAST_DATA)
            LAST_RENDER_TS = now
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


def mihomo_subscription_config(subscription_url: str) -> str:
    # JSON quoting is valid YAML quoting and safely preserves URL query parameters.
    escaped_url = json.dumps(subscription_url, ensure_ascii=False)
    return f"""mixed-port: 7890
allow-lan: true
bind-address: "*"
mode: rule
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
      interval: 600
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
    interval: 300
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
rules:
  - DOMAIN-SUFFIX,whatsapp.net,WhatsApp代理
  - MATCH,数据屏代理
"""


def admin_authorized(headers: Any) -> bool:
    expected_user = os.getenv("ADMIN_USERNAME", "admin")
    expected_password = os.getenv("ADMIN_PASSWORD", "")
    if not expected_password:
        return False
    auth = headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return False
    return hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_password)


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
    return page.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), fmt % args), flush=True)

    def do_GET(self) -> None:
        global LAST_IMAGE
        if proxy_handle(self, admin_authorized):
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/admin":
            if not os.getenv("ADMIN_PASSWORD", ""):
                body = "请先在 .env 设置 ADMIN_PASSWORD，再重启容器。".encode("utf-8")
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
                '<div class="wrap"><h1>NAS 数据屏与局域网代理</h1><p>行情数据屏 · 节点管理 · Telegram MTProto</p>'
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

        if path.startswith("/data.json") or path.startswith("/healthz"):
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
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        config = load_runtime_config()
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
        subscription_url = str(config.get("MIHOMO_SUBSCRIPTION_URL") or "").strip()
        yaml_text = (form.get("MIHOMO_YAML") or [""])[0].strip()
        generated_proxy_config = mihomo_subscription_config(subscription_url) if subscription_url else yaml_text
        if generated_proxy_config:
            try:
                save_and_reload(generated_proxy_config)
            except Exception:
                body = '代理配置校验或重载失败，已恢复旧配置；请检查 YAML 后重试。'.encode('utf-8')
                self.send_response(400)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
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
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Kindle dashboard server listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
