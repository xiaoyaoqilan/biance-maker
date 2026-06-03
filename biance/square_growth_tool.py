#!/usr/bin/env python3
"""
DogDoing + Binance Square monitor and posting helper.

This script can:
- monitor DogDoing pages for changes
- monitor Binance Square pages and generate drafts
- post pure text to Binance Square with the Square OpenAPI key

Set BINANCE_SQUARE_OPENAPI_KEY in your environment before posting.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import random
import re
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "square_monitor_config.json"
STATE_PATH = APP_DIR / ".square_monitor_state.json"
DRAFTS_PATH = APP_DIR / "square_drafts.jsonl"
POST_LOG_PATH = APP_DIR / "square_post_log.jsonl"

POST_ENDPOINT = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
DEFAULT_HOT_URLS = [
    "https://www.binance.com/en/square/hashtag/Hot",
    "https://www.binance.com/en-AE/square/hashtag/%D8%A7%D9%84%D9%81%D8%A6%D8%A9%20%7C%20Hot",
]
DEFAULT_DOGDOING_URLS = [
    "https://dogdoing.ai/",
    "https://dogdoing.ai/alpha",
    "https://dogdoing.ai/market",
    "https://dogdoing.ai/sentiment",
]
BINANCE_API_BASE = os.environ.get("BINANCE_API_BASE", "https://api.binance.com").rstrip("/")
OKX_API_BASE = os.environ.get("OKX_API_BASE", "https://www.okx.com").rstrip("/")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class WatchTarget:
    name: str
    url: str
    kind: str = "html"
    keywords: list[str] | None = None


@dataclass
class HotTopic:
    tag: str
    discussing: int = 0
    views: int = 0
    source_url: str = ""

    @property
    def score(self) -> int:
        return self.discussing * 1_000 + self.views


@dataclass
class OkxTicker:
    inst_id: str
    last: float
    change_pct: float
    vol_ccy24h: float
    source_url: str = ""

    @property
    def score(self) -> float:
        return self.change_pct


@dataclass
class Binance24hTicker:
    symbol: str
    last: float
    change_pct: float
    high: float
    low: float
    quote_volume: float
    source_url: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def default_config() -> dict:
    return {
        "poll_interval_seconds": 300,
        "max_summary_chars": 700,
        "hot_urls": DEFAULT_HOT_URLS,
        "auto_post": {
            "enabled": False,
            "min_discussing": 1000,
            "skip_tags": ["airdrop", "giveaway", "referral"],
            "disclaimer": "仅做市场观察，不构成投资建议。",
        },
        "watch": [
            {
                "name": "DogDoing home",
                "url": "https://dogdoing.ai/",
                "kind": "dogdoing_home",
            },
            {
                "name": "DogDoing alpha",
                "url": "https://dogdoing.ai/alpha",
                "kind": "html",
            },
            {
                "name": "DogDoing market",
                "url": "https://dogdoing.ai/market",
                "kind": "html",
            },
            {
                "name": "DogDoing sentiment",
                "url": "https://dogdoing.ai/sentiment",
                "kind": "html",
            },
            {
                "name": "BTC Square search",
                "url": "https://www.binance.com/en/square/search?keyword=BTC",
                "kind": "square_search",
                "keywords": ["BTC", "Bitcoin", "ETF", "减半", "宏观"],
            },
            {
                "name": "ETH Square search",
                "url": "https://www.binance.com/en/square/search?keyword=ETH",
                "kind": "square_search",
                "keywords": ["ETH", "Ethereum", "ETF", "质押", "L2", "升级"],
            },
            {
                "name": "BNB Square search",
                "url": "https://www.binance.com/en/square/search?keyword=BNB",
                "kind": "square_search",
                "keywords": ["BNB", "Binance", "Launchpool", "生态", "手续费"],
            },
        ],
        "posting": {
            "style": "短句、直给、带关键价位和动作。",
            "disclaimer": "非投资建议，别上杠杆硬追。",
        },
    }


def init_config(force: bool = False) -> None:
    if CONFIG_PATH.exists() and not force:
        print(f"Config already exists: {CONFIG_PATH}")
        return
    save_json(CONFIG_PATH, default_config())
    print(f"Wrote {CONFIG_PATH}")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        init_config()
    config = load_json(CONFIG_PATH, {})
    base = default_config()
    for key, value in base.items():
        config.setdefault(key, value)
    return config


def targets_from_config(config: dict) -> list[WatchTarget]:
    targets: list[WatchTarget] = []
    for item in config.get("watch", []):
        targets.append(
            WatchTarget(
                name=str(item.get("name") or item.get("url") or "unnamed"),
                url=str(item["url"]),
                kind=str(item.get("kind") or "html"),
                keywords=[str(k) for k in item.get("keywords", [])],
            )
        )
    return targets


def request_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def request_json(url: str, timeout: int = 20):
    return json.loads(request_text(url, timeout=timeout))


def visible_text(raw: str) -> str:
    raw = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def parse_count(raw: str) -> int:
    value = raw.replace(",", "").strip()
    match = re.match(r"([0-9]+(?:\.[0-9]+)?)([KkMm]?)", value)
    if not match:
        return 0
    number = float(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    return int(number)


def keyword_hits(text: str, keywords: Iterable[str]) -> list[str]:
    lower = text.lower()
    return [k for k in keywords if k.lower() in lower]


def compact_context(text: str, hits: list[str], max_chars: int) -> str:
    if not hits:
        return text[:max_chars]
    lower = text.lower()
    positions = [lower.find(hit.lower()) for hit in hits if lower.find(hit.lower()) >= 0]
    start = max(0, min(positions) - 180) if positions else 0
    return text[start : start + max_chars].strip()


def optimize_text(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    if len(lines) == 1:
        return lines[0]
    polished: list[str] = []
    for line in lines:
        if len(line) > 120 and "：" in line:
            head, tail = line.split("：", 1)
            polished.append(f"{head}：")
            polished.append(tail.strip())
        else:
            polished.append(line)
    return "\n\n".join(polished).strip()


def okx_api_url(path: str) -> str:
    return f"{OKX_API_BASE}{path}"


def binance_api_url(path: str) -> str:
    return f"{BINANCE_API_BASE}{path}"


def fetch_binance_24h(symbols: Iterable[str]) -> list[Binance24hTicker]:
    tickers: list[Binance24hTicker] = []
    for raw_symbol in symbols:
        symbol = str(raw_symbol).strip().upper()
        if not symbol:
            continue
        url = binance_api_url(f"/api/v3/ticker/24hr?symbol={urllib.parse.quote(symbol)}")
        payload = request_json(url)
        if not isinstance(payload, dict):
            continue
        try:
            tickers.append(
                Binance24hTicker(
                    symbol=str(payload["symbol"]),
                    last=float(payload["lastPrice"]),
                    change_pct=float(payload["priceChangePercent"]),
                    high=float(payload["highPrice"]),
                    low=float(payload["lowPrice"]),
                    quote_volume=float(payload.get("quoteVolume") or 0),
                    source_url=url,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tickers


def _nice_step(price: float) -> float:
    if price >= 50_000:
        return 500
    if price >= 10_000:
        return 200
    if price >= 1_000:
        return 20
    if price >= 100:
        return 2
    if price >= 10:
        return 0.2
    if price >= 1:
        return 0.02
    return 0.002


def _nice_level(price: float) -> float:
    step = _nice_step(price)
    return round(round(price / step) * step, 8)


def make_binance_24h_post(ticker: Binance24hTicker) -> str:
    base = ticker.symbol.removesuffix("USDT").removesuffix("BUSD")
    coin = f"${base}"

    up_break = ticker.high
    down_break = ticker.low
    up_target = _nice_level(up_break * 1.005)
    down_support = _nice_level(down_break * 0.995)

    price_text = f"{ticker.last:,.2f}" if ticker.last >= 1 else f"{ticker.last:.6f}"
    high_text = f"{ticker.high:,.2f}" if ticker.high >= 1 else f"{ticker.high:.6f}"
    low_text = f"{ticker.low:,.2f}" if ticker.low >= 1 else f"{ticker.low:.6f}"
    chg = f"{ticker.change_pct:+.2f}%"

    # Aggressive, high-engagement hooks (including recap style)
    hooks = [
        "兄弟们，这票有妖气，主力要开始表演了。", "BNB 这个位置，狗庄插针插得我头皮发麻。",
        "还在无脑看多？别被卖了还帮人数钱，看清楚点位。", "这行情，散户现在进去就是送外卖，速度撤离。",
        "我盯这票一整晚了，主力意图已经图穷匕见。", "这种走势，摆明了是要收割最后一波韭菜。",
        "别跟我谈什么信仰，在这个点位，保命才是第一位。", "这就是标准的诱多陷阱，谁冲谁接盘。",
        "盘面异动极其明显，大行情可能就在这一两个小时。", "我看这票要变天了，还没清醒的赶紧看过来。",
        "马后炮谁都会，咱们来实打实复盘一下刚才这波针。", "复盘一下昨天的盘面，主力洗盘手法极其残忍。",
        "这波复盘看下来，支撑位卡得死死的，狗庄根本砸不穿。"
    ]
    hook = random.choice(hooks)
    
    # Aggressive trading insights (including recap style)
    insights = [
        f"听我一句，上看 {_nice_level(up_break)} 冲不过去就是骗炮，别硬扛，容易爆仓。",
        f"真要跌破 {_nice_level(down_break)} 直接就是瀑布，别犹豫，止损比命长。",
        "现在这位置，主力在疯狂洗盘，你要是怂了就真被洗出去了。",
        "这种震荡就是在磨耐心，点位不到绝对不入场，懂的都懂。",
        "缩量阴跌最致命，关注这个支撑位，破了就是深渊。",
        "别死盯着K线看，看大户的成交量，主力在悄悄撤退。",
        "这波我看还要往下扎针，没上车的别乱冲，等狗庄表演完。",
        "复盘看量能，主力在悄悄出货，别傻傻以为是回调。",
        "复盘昨天的点位，几乎是一字不差，主力控盘太明显了。"
    ]
    insight = random.choice(insights)
    
    # Urgent/Charismatic CTAs
    ctas = [
        "想看我下波盯谁？评论区打出来，速度点关注，别等爆仓了再来！", 
        "觉得我看得准的，点赞支持一下，下波行情带你们避坑。", 
        "这种干货也就我会说，点个关注不迷路，财富自由看这波。", 
        "关注我，带你拆解狗庄的所有套路，评论区见！",
        "行情不等人，速度点关注，实时盯盘带你飞。"
    ]
    cta = random.choice(ctas)

    # Randomize structure to look less like a template
    templates = [
        f"{coin} {hook}\n\n现在价格 {price_text}，跌了 {chg}。最高 {high_text}，最低 {low_text}。\n\n点位建议：上看 {_nice_level(up_break)} 压力，站稳看 {up_target}；下看 {_nice_level(down_break)} 支撑，破了就撤。\n\n{insight}\n\n{cta}",
        f"{hook} {coin} 现在的局面很尴尬。\n\n目前 {price_text}，24小时波动 {chg}。高位 {high_text}，低位 {low_text}。\n\n点位死盯：压力位 {_nice_level(up_break)}，支撑位 {_nice_level(down_break)}。破位直接走人，别谈格局。\n\n{insight}\n\n{cta}",
        f"又是被狗庄支配的一天？看看 {coin}。\n\n现价 {price_text} ({chg})。今天高低点：{high_text} / {low_text}。\n\n我的建议：冲破 {_nice_level(up_break)} 再看涨到 {up_target}，不然全是骗炮。跌破 {_nice_level(down_break)} 赶紧跑路。\n\n{insight}\n\n{cta}",
        f"来，咱们实打实复盘一下 {coin}。今天最高冲到 {high_text}，最低砸到 {low_text}。主力这波洗盘手法极其残忍。\n\n现价 {price_text} ({chg})。\n\n{insight}\n\n点位死盯：压力位 {_nice_level(up_break)}，支撑位 {_nice_level(down_break)}。\n\n{cta}"
    ]
    
    return optimize_text(random.choice(templates).strip())



def command_binance_24h(args) -> None:

    symbols = [s.strip().upper() for s in (args.symbols.split(",") if args.symbols else []) if s.strip()]
    if not symbols:
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "TRXUSDT"]
    tickers = fetch_binance_24h(symbols)
    for t in tickers:
        print(
            f"{t.symbol} last={t.last} change={t.change_pct:+.2f}% high={t.high} low={t.low} qv={t.quote_volume}"
            f"\nsource={t.source_url}\n"
        )


def command_binance_main_drafts(args) -> None:
    symbols = [s.strip().upper() for s in (args.symbols.split(",") if args.symbols else []) if s.strip()]
    if not symbols:
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    tickers = fetch_binance_24h(symbols)
    created = 0
    for t in tickers:
        body = make_binance_24h_post(t)
        draft_id = append_draft("binance_24h_ticker", t.source_url, [t.symbol], body)
        created += 1
        print(f"[{draft_id}] {t.symbol} change={t.change_pct:+.2f}%")
        print(f"created_drafts={created}")

def fetch_okx_tickers(inst_type: str = "SPOT") -> list[OkxTicker]:
    url = okx_api_url(f"/api/v5/market/tickers?instType={urllib.parse.quote(inst_type)}")
    payload = request_json(url)
    data = payload.get("data", []) if isinstance(payload, dict) else []
    tickers: list[OkxTicker] = []
    for item in data:
        try:
            inst_id = str(item["instId"])
            last = float(item["last"])
            open_24h = float(item.get("open24h") or item.get("sodUtc0") or 0)
            if not open_24h:
                continue
            change_pct = (last - open_24h) / open_24h * 100
            vol_ccy24h = float(item.get("volCcy24h") or 0)
            tickers.append(
                OkxTicker(
                    inst_id=inst_id,
                    last=last,
                    change_pct=change_pct,
                    vol_ccy24h=vol_ccy24h,
                    source_url=url,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tickers


def okx_top_gainers(limit: int = 5, inst_type: str = "SPOT", quote: str = "USDT") -> list[OkxTicker]:
    tickers = fetch_okx_tickers(inst_type=inst_type)
    filtered = [t for t in tickers if t.inst_id.endswith(f"-{quote}") or t.inst_id.endswith(quote)]
    return sorted(filtered, key=lambda item: item.score, reverse=True)[:limit]


def summarize_dogdoing_home(text: str) -> str:
    sections = [
        "市场概览",
        "42.space 预测市场",
        "Binance Alpha 热点追踪与 Crypto 最新资讯",
        "Binance 合约广场热度与持仓量异动监控",
        "BTC 加密货币涨跌幅排行与市场情绪分析",
        "DogDoing — Binance Skills 加密货币导航仪表盘",
    ]
    found = [s for s in sections if s in text]
    summary = "；".join(found) if found else "首页可见模块更新"
    snippet = compact_context(text, found[:2], 420)
    return optimize_text(
        textwrap.dedent(
            f"""
            DogDoing 首页更新。

            关注模块：{summary}
            参考片段：{snippet}
            """
        ).strip()
    )


def summarize_generic_change(target: WatchTarget, text: str, config: dict) -> str:
    hits = keyword_hits(text, target.keywords or [])
    hit_text = "、".join(hits[:6]) if hits else target.name
    context = compact_context(text, hits, int(config.get("max_summary_chars", 700)))
    disclaimer = config.get("posting", {}).get("disclaimer", "仅为个人观察，不构成投资建议。")
    return optimize_text(
        textwrap.dedent(
            f"""
            监测到 {target.name} 有更新，重点标签：{hit_text}。

            参考片段：{context}

            {disclaimer}
            """
        ).strip()
    )


def make_hot_post(topic: HotTopic, config: dict) -> str:
    disclaimer = config.get("auto_post", {}).get("disclaimer", "仅为个人观察，不构成投资建议。")
    tag = topic.tag.lstrip("#")
    discussing = f"{topic.discussing:,}" if topic.discussing else "不少"
    views = f"{topic.views:,}" if topic.views else "较高"
    return optimize_text(
        textwrap.dedent(
            f"""
            Binance Square 当前热度很高的话题之一是 #{tag}。

            讨论量约 {discussing}，浏览量约 {views}。我更关注热度是否能持续，而不是一眼冲进去追消息。

            {disclaimer}
            """
        ).strip()
    )


def make_okx_post(ticker: OkxTicker, config: dict) -> str:
    disclaimer = config.get("auto_post", {}).get("disclaimer", "仅为个人观察，不构成投资建议。")
    base = ticker.inst_id.split("-")[0].strip()
    quote = ticker.inst_id.split("-")[-1].strip() if "-" in ticker.inst_id else ""
    last_text = f"{ticker.last:.6f}" if ticker.last < 1 else f"{ticker.last:.4f}" if ticker.last < 100 else f"{ticker.last:.2f}"
    chg_text = f"{ticker.change_pct:+.2f}%"
    vol_text = f"{ticker.vol_ccy24h:,.0f}"

    return optimize_text(
        textwrap.dedent(
            f"""
            ${base} 今天在 OKX 这边挺抢眼。

            现价 {last_text}（24h {chg_text}），成交额 {vol_text} {quote}。

            这类票最怕的不是涨，是涨完没人接。能不能续上，才是后面真东西。
            我现在先看它回踩能不能站住，站住了再说下一步；站不住就别硬追。

            {disclaimer}
            """
        ).strip()
    )


def append_draft(source: str, url: str, hits: list[str], body: str) -> str:
    draft_id = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
    record = {
        "id": draft_id,
        "created_at": utc_now(),
        "source": source,
        "url": url,
        "keywords": hits,
        "body": body,
        "posted": False,
    }
    with DRAFTS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return draft_id


def iter_drafts() -> list[dict]:
    if not DRAFTS_PATH.exists():
        return []
    drafts: list[dict] = []
    for line in DRAFTS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            drafts.append(json.loads(line))
    return drafts


def rewrite_drafts(drafts: list[dict]) -> None:
    with DRAFTS_PATH.open("w", encoding="utf-8") as fh:
        for item in drafts:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def get_square_key() -> str:
    key = os.environ.get("BINANCE_SQUARE_OPENAPI_KEY", "").strip()
    if not key or key == "your_api_key":
        raise SystemExit("Missing BINANCE_SQUARE_OPENAPI_KEY. Set it first, then retry posting.")
    return key


def post_text(body: str) -> dict:
    payload = json.dumps({"bodyTextOnly": body}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        POST_ENDPOINT,
        data=payload,
        method="POST",
        headers={
            "X-Square-OpenAPI-Key": get_square_key(),
            "Content-Type": "application/json",
            "clienttype": "binanceSkill",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Post failed: HTTP {exc.code} {detail}") from exc


def log_post(body: str, result: dict, source: str = "manual") -> None:
    data = result.get("data") if isinstance(result, dict) else None
    post_id = data.get("id") if isinstance(data, dict) else None
    record = {
        "created_at": utc_now(),
        "source": source,
        "body": body,
        "fingerprint": post_fingerprint(body),
        "tickers": sorted(extract_tickers(body)),
        "result": result,
        "post_id": post_id,
        "post_url": f"https://www.binance.com/square/post/{post_id}" if post_id else None,
        "share_link": data.get("shareLink") if isinstance(data, dict) else None,
    }
    with POST_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def normalize_post_body(body: str) -> str:
    body = body.upper()
    body = re.sub(r"\$([A-Z0-9]{2,12})", r"$\1", body)
    body = re.sub(r"\d+(?:,\d{3})*(?:\.\d+)?", "<NUM>", body)
    body = re.sub(r"\s+", "", body)
    body = re.sub(r"[，。！？；：、,.!?;:()\[\]{}<>《》\"'`~\-_=+|/\\]", "", body)
    return body


def post_fingerprint(body: str) -> str:
    return hashlib.sha256(normalize_post_body(body).encode("utf-8")).hexdigest()


def extract_tickers(body: str) -> set[str]:
    return {m.group(1).upper() for m in re.finditer(r"\$([A-Za-z0-9]{2,12})", body)}


def iter_post_log() -> list[dict]:
    if not POST_LOG_PATH.exists():
        return []
    records: list[dict] = []
    for line in POST_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def jaccard_similarity(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[\$A-Z0-9]+|[\u4e00-\u9fff]{2}", normalize_post_body(left)))
    right_tokens = set(re.findall(r"[\$A-Z0-9]+|[\u4e00-\u9fff]{2}", normalize_post_body(right)))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def find_duplicate_post(body: str, threshold: float = 0.72) -> dict | None:
    fingerprint = post_fingerprint(body)
    tickers = extract_tickers(body)
    for record in reversed(iter_post_log()):
        old_body = str(record.get("body") or "")
        if not old_body:
            continue
        if record.get("fingerprint") == fingerprint or post_fingerprint(old_body) == fingerprint:
            return {"reason": "same_fingerprint", "record": record}
        old_tickers = extract_tickers(old_body)
        if tickers and old_tickers and tickers == old_tickers:
            similarity = jaccard_similarity(body, old_body)
            if similarity >= threshold:
                return {"reason": f"similar_same_tickers:{similarity:.2f}", "record": record}
    return None


def guard_not_duplicate(body: str, allow_duplicate: bool = False) -> None:
    if allow_duplicate:
        return
    duplicate = find_duplicate_post(body)
    if duplicate:
        record = duplicate["record"]
        post_url = record.get("post_url") or record.get("share_link") or "unknown"
        raise SystemExit(f"Duplicate post blocked ({duplicate['reason']}): {post_url}")


def choose_body(text: str, use_original: bool = False) -> tuple[str, str]:
    optimized = optimize_text(text)
    selected = text if use_original else optimized
    return selected, optimized


def parse_hot_topics_from_text(text: str, source_url: str) -> list[HotTopic]:
    topics: dict[str, HotTopic] = {}
    patterns = [
        re.compile(
            r"#?([A-Za-z0-9][A-Za-z0-9_'’.$-]{1,80})\s+([0-9][0-9,]*(?:\.[0-9]+)?[KkMm]?)\s+views?\s*/\s*([0-9][0-9,]*(?:\.[0-9]+)?[KkMm]?)\s+discussing",
            re.I,
        ),
        re.compile(
            r"([0-9][0-9,]*(?:\.[0-9]+)?[KkMm]?)\s+views?\s*/\s*([0-9][0-9,]*(?:\.[0-9]+)?[KkMm]?)\s+discussing\s+#?([A-Za-z0-9][A-Za-z0-9_'’.$-]{1,80})",
            re.I,
        ),
    ]
    for match in patterns[0].finditer(text):
        tag, views, discussing = match.groups()
        topics[tag.lower()] = HotTopic(
            tag=tag,
            views=parse_count(views),
            discussing=parse_count(discussing),
            source_url=source_url,
        )
    for match in patterns[1].finditer(text):
        views, discussing, tag = match.groups()
        topics[tag.lower()] = HotTopic(
            tag=tag,
            views=parse_count(views),
            discussing=parse_count(discussing),
            source_url=source_url,
        )
    return list(topics.values())


def parse_hot_topics_from_jsonish(raw: str, source_url: str) -> list[HotTopic]:
    topics: dict[str, HotTopic] = {}
    tag_keys = r"(?:tagName|hashtag|topic|name|symbol)"
    for match in re.finditer(rf'"{tag_keys}"\s*:\s*"([^"]{{2,100}})"', raw):
        tag = html.unescape(match.group(1)).strip("# ")
        if not re.search(r"[A-Za-z0-9]", tag):
            continue
        window = raw[max(0, match.start() - 600) : match.end() + 1000]
        discussing_match = re.search(r'"(?:discussing|discussCount|discussionCount|talkCount)"\s*:\s*"?([0-9,.KkMm]+)"?', window)
        views_match = re.search(r'"(?:views|viewCount|readCount)"\s*:\s*"?([0-9,.KkMm]+)"?', window)
        discussing = parse_count(discussing_match.group(1)) if discussing_match else 0
        views = parse_count(views_match.group(1)) if views_match else 0
        if discussing or views:
            candidate = HotTopic(tag=tag, discussing=discussing, views=views, source_url=source_url)
            current = topics.get(tag.lower())
            if current is None or candidate.score > current.score:
                topics[tag.lower()] = candidate
    return list(topics.values())


def scan_hot_topics(config: dict) -> list[HotTopic]:
    topics: dict[str, HotTopic] = {}
    for url in config.get("hot_urls", DEFAULT_HOT_URLS):
        try:
            raw = request_text(url)
        except Exception as exc:
            print(f"[hot] fetch failed: {url} ({exc})")
            continue
        text = visible_text(raw)
        parsed = parse_hot_topics_from_text(text, url) + parse_hot_topics_from_jsonish(raw, url)
        for topic in parsed:
            current = topics.get(topic.tag.lower())
            if current is None or topic.score > current.score:
                topics[topic.tag.lower()] = topic
    skip_tags = [str(x).lower() for x in config.get("auto_post", {}).get("skip_tags", [])]
    filtered = [
        topic
        for topic in topics.values()
        if not any(skip in topic.tag.lower() for skip in skip_tags)
    ]
    return sorted(filtered, key=lambda item: item.score, reverse=True)


def monitor_targets_once() -> int:
    config = load_config()
    state = load_json(STATE_PATH, {})
    created = 0
    for target in targets_from_config(config):
        try:
            raw = request_text(target.url)
        except urllib.error.HTTPError as exc:
            print(f"[{target.name}] HTTP {exc.code}: {target.url}")
            continue
        except Exception as exc:
            print(f"[{target.name}] fetch failed: {exc}")
            continue

        text = visible_text(raw)
        current_digest = digest(text[:12000])
        previous_digest = state.get(target.url, {}).get("digest")

        state[target.url] = {
            "name": target.name,
            "kind": target.kind,
            "digest": current_digest,
            "last_checked": utc_now(),
        }

        if previous_digest and previous_digest != current_digest:
            if target.kind == "dogdoing_home":
                body = summarize_dogdoing_home(text)
            else:
                body = summarize_generic_change(target, text, config)
            draft_id = append_draft(target.name, target.url, target.keywords or [], body)
            created += 1
            print(f"[{target.name}] new draft {draft_id}")
        elif not previous_digest:
            print(f"[{target.name}] baseline saved")
        else:
            print(f"[{target.name}] no change")

    save_json(STATE_PATH, state)
    return created


def monitor_loop() -> None:
    config = load_config()
    interval = max(60, int(config.get("poll_interval_seconds", 300)))
    print(f"Monitoring {len(targets_from_config(config))} target(s), interval={interval}s")
    while True:
        monitor_targets_once()
        time.sleep(interval)


def command_post(args: argparse.Namespace) -> None:
    body = args.text.strip()
    if not body:
        raise SystemExit("Post text cannot be empty.")
    selected, optimized = choose_body(body, use_original=args.original)
    if args.preview:
        print("Original:\n")
        print(body)
        print("\nOptimized:\n")
        print(optimized)
        print("\nSelected:\n")
        print(selected)
        return
    if args.dry_run:
        print(selected)
        return
    guard_not_duplicate(selected, allow_duplicate=args.allow_duplicate)
    result = post_text(selected)
    log_post(selected, result, source="post")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    post_id = result.get("data", {}).get("id") if isinstance(result.get("data"), dict) else None
    if post_id:
        print(f"https://www.binance.com/square/post/{post_id}")


def command_list_drafts(_: argparse.Namespace) -> None:
    drafts = [d for d in iter_drafts() if not d.get("posted")]
    if not drafts:
        print("No unposted drafts.")
        return
    for item in drafts:
        body = item.get("body", "")
        preview = re.sub(r"\s+", " ", body)[:160]
        print(f"{item['id']} | {item.get('source')} | {preview}")


def command_show_draft(args: argparse.Namespace) -> None:
    for item in iter_drafts():
        if item.get("id") == args.id:
            print(item.get("body", ""))
            return
    raise SystemExit(f"Draft not found: {args.id}")


def command_post_draft(args: argparse.Namespace) -> None:
    drafts = iter_drafts()
    for item in drafts:
        if item.get("id") == args.id:
            body = str(item.get("body", "")).strip()
            selected, optimized = choose_body(body, use_original=args.original)
            if args.preview:
                print("Original:\n")
                print(body)
                print("\nOptimized:\n")
                print(optimized)
                print("\nSelected:\n")
                print(selected)
                return
            if args.dry_run:
                print(selected)
                return
            guard_not_duplicate(selected, allow_duplicate=args.allow_duplicate)
            result = post_text(selected)
            log_post(selected, result, source="post-draft")
            item["posted"] = True
            item["posted_at"] = utc_now()
            item["post_result"] = result
            rewrite_drafts(drafts)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            post_id = result.get("data", {}).get("id") if isinstance(result.get("data"), dict) else None
            if post_id:
                print(f"https://www.binance.com/square/post/{post_id}")
            return
    raise SystemExit(f"Draft not found: {args.id}")


def command_post_sequence(args: argparse.Namespace) -> None:
    texts = [t.strip() for t in args.text if t.strip()]
    for path in getattr(args, "text_file", []) or []:
        p = Path(path)
        if not p.exists():
            raise SystemExit(f"Missing text file: {p}")
        content = p.read_text(encoding="utf-8").strip()
        if content:
            texts.append(content)
    if not texts:
        raise SystemExit("No texts provided.")
    if args.min_delay > args.max_delay:
        raise SystemExit("min-delay must be <= max-delay.")
    for idx, text in enumerate(texts, start=1):
        selected, optimized = choose_body(text, use_original=args.original)
        body = selected
        if args.preview:
            print(f"[{idx}/{len(texts)}] Preview:\n")
            print("Original:\n")
            print(text)
            print("\nOptimized:\n")
            print(optimized)
            print("\nSelected:\n")
            print(body)
            print()
        if args.dry_run:
            print(body)
        else:
            guard_not_duplicate(body, allow_duplicate=args.allow_duplicate)
            result = post_text(body)
            log_post(body, result, source="post-sequence")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            post_id = result.get("data", {}).get("id") if isinstance(result.get("data"), dict) else None
            if post_id:
                print(f"https://www.binance.com/square/post/{post_id}")
        if idx < len(texts):
            wait_seconds = random.randint(args.min_delay, args.max_delay)
            print(f"sleep={wait_seconds}s")
            time.sleep(wait_seconds)


def command_hot(args: argparse.Namespace) -> None:
    config = load_config()
    topics = scan_hot_topics(config)
    if not topics:
        raise SystemExit("No hot topics parsed. Binance page structure may have changed.")
    for topic in topics[: args.limit]:
        print(f"#{topic.tag} | discussing={topic.discussing:,} | views={topic.views:,} | {topic.source_url}")


def command_okx_hot(args: argparse.Namespace) -> None:
    gainers = okx_top_gainers(limit=args.limit, inst_type=args.inst_type, quote=args.quote)
    if not gainers:
        raise SystemExit("No OKX gainers parsed.")
    for ticker in gainers:
        print(f"${ticker.inst_id.split('-')[0]} | {ticker.change_pct:.2f}% | last={ticker.last:.8f} | vol={ticker.vol_ccy24h:,.2f}")


def command_okx_draft_batch(args: argparse.Namespace) -> None:
    config = load_config()
    gainers = okx_top_gainers(limit=args.topics, inst_type=args.inst_type, quote=args.quote)
    if not gainers:
        raise SystemExit("No OKX gainers parsed.")
    if args.count <= 0:
        raise SystemExit("Count must be positive.")
    generated = 0
    for idx, ticker in enumerate(gainers):
        per_topic = max(1, args.count // len(gainers))
        if idx < args.count % len(gainers):
            per_topic += 1
        for _ in range(per_topic):
            body = make_okx_post(ticker, config)
            draft_id = append_draft("okx-gainers", ticker.source_url, [ticker.inst_id], body)
            generated += 1
            if args.verbose:
                print(f"[{draft_id}] ${ticker.inst_id.split('-')[0]} change={ticker.change_pct:.2f}%")
                print(body)
                print()
    print(f"generated_drafts={generated}")
    for ticker in gainers[:5]:
        print(f"${ticker.inst_id.split('-')[0]} | {ticker.change_pct:.2f}% | last={ticker.last:.8f}")


def command_hot_draft_batch(args: argparse.Namespace) -> None:
    config = load_config()
    topics = scan_hot_topics(config)
    if not topics:
        raise SystemExit("No hot topics parsed. Binance page structure may have changed.")
    if args.count <= 0:
        raise SystemExit("Count must be positive.")
    selected = topics[: max(1, min(len(topics), args.topics))]
    generated = 0
    for idx, topic in enumerate(selected):
        per_topic = max(1, args.count // len(selected))
        if idx < args.count % len(selected):
            per_topic += 1
        for n in range(per_topic):
            body = optimize_text(
                textwrap.dedent(
                    f"""
                    #{topic.tag} 的讨论度正在升，我更看重它后续是否还能延续。

                    观察角度：热度持续性 / 资金跟随 / 价格确认。
                    这条草稿是围绕热度变化拆出来的第 {n + 1} 条。

                    仅为个人观察，不构成投资建议。
                    """
                ).strip()
            )
            draft_id = append_draft("hot-topic-batch", topic.source_url, [topic.tag], body)
            generated += 1
            if args.verbose:
                print(f"[{draft_id}] #{topic.tag} discussing={topic.discussing:,} views={topic.views:,}")
                print(body)
                print()
    print(f"generated_drafts={generated}")
    for topic in selected[:5]:
        print(f"#{topic.tag} | discussing={topic.discussing:,} | views={topic.views:,}")


def command_dogdoing_scan(_: argparse.Namespace) -> None:
    for url in DEFAULT_DOGDOING_URLS:
        raw = request_text(url)
        text = visible_text(raw)
        if url.endswith("/"):
            summary = summarize_dogdoing_home(text)
        else:
            summary = optimize_text(text[:1000])
        print(f"[{url}]")
        print(summary)
        print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DogDoing + Binance Square monitor and posting helper")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create sample config")
    init.add_argument("--force", action="store_true", help="Overwrite existing config")
    init.set_defaults(func=lambda args: init_config(force=args.force))

    once = sub.add_parser("monitor-once", help="Poll configured targets once")
    once.set_defaults(func=lambda _: print(f"created_drafts={monitor_targets_once()}"))

    loop = sub.add_parser("monitor", help="Poll configured targets forever")
    loop.set_defaults(func=lambda _: monitor_loop())

    dogdoing = sub.add_parser("dogdoing-scan", help="Print DogDoing page summaries")
    dogdoing.set_defaults(func=command_dogdoing_scan)

    hot = sub.add_parser("hot", help="List parsed hot Binance Square topics")
    hot.add_argument("--limit", type=int, default=10)
    hot.set_defaults(func=command_hot)

    okx_hot = sub.add_parser("okx-hot", help="List OKX top gainers")
    okx_hot.add_argument("--limit", type=int, default=5)
    okx_hot.add_argument("--inst-type", default="SPOT", help="OKX instrument type, e.g. SPOT or SWAP")
    okx_hot.add_argument("--quote", default="USDT", help="Quote currency filter")
    okx_hot.set_defaults(func=command_okx_hot)

    binance_24h = sub.add_parser("binance-24h", help="Fetch Binance 24h ticker(s) for given symbols")
    binance_24h.add_argument(
        "--symbols",
        help='Comma-separated symbols, e.g. "BTCUSDT,ETHUSDT". Default: BTC/ETH/SOL/BNB/TRX',
    )
    binance_24h.set_defaults(func=command_binance_24h)

    binance_main = sub.add_parser("binance-main-drafts", help="Generate drafts from Binance 24h tickers")
    binance_main.add_argument(
        "--symbols",
        help='Comma-separated symbols, e.g. "BTCUSDT,ETHUSDT,SOLUSDT". Default: BTC/ETH/SOL',
    )
    binance_main.set_defaults(func=command_binance_main_drafts)

    hot_batch = sub.add_parser("hot-draft-batch", help="Generate many drafts from hot Binance Square topics")
    hot_batch.add_argument("--count", type=int, default=30, help="How many drafts to generate")
    hot_batch.add_argument("--topics", type=int, default=5, help="How many hot topics to use")
    hot_batch.add_argument("--verbose", action="store_true", help="Print every draft")
    hot_batch.set_defaults(func=command_hot_draft_batch)

    okx_batch = sub.add_parser("okx-draft-batch", help="Generate many drafts from OKX gainers")
    okx_batch.add_argument("--count", type=int, default=5, help="How many drafts to generate")
    okx_batch.add_argument("--topics", type=int, default=5, help="How many gainers to use")
    okx_batch.add_argument("--inst-type", default="SPOT", help="OKX instrument type, e.g. SPOT or SWAP")
    okx_batch.add_argument("--quote", default="USDT", help="Quote currency filter")
    okx_batch.add_argument("--verbose", action="store_true", help="Print every draft")
    okx_batch.set_defaults(func=command_okx_draft_batch)

    post = sub.add_parser("post", help="Post text to Binance Square")
    post.add_argument("text", help="Text to publish")
    post.add_argument("--dry-run", action="store_true", help="Print without posting")
    post.add_argument("--preview", action="store_true", help="Show original and optimized text")
    post.add_argument("--original", action="store_true", help="Post original text instead of optimized")
    post.add_argument("--allow-duplicate", action="store_true", help="Bypass duplicate post protection")
    post.set_defaults(func=command_post)

    drafts = sub.add_parser("drafts", help="List unposted drafts")
    drafts.set_defaults(func=command_list_drafts)

    show = sub.add_parser("show-draft", help="Show one draft")
    show.add_argument("id")
    show.set_defaults(func=command_show_draft)

    post_draft = sub.add_parser("post-draft", help="Post a draft by id")
    post_draft.add_argument("id")
    post_draft.add_argument("--dry-run", action="store_true", help="Print without posting")
    post_draft.add_argument("--preview", action="store_true", help="Show original and optimized text")
    post_draft.add_argument("--original", action="store_true", help="Post original text instead of optimized")
    post_draft.add_argument("--allow-duplicate", action="store_true", help="Bypass duplicate post protection")
    post_draft.set_defaults(func=command_post_draft)

    post_sequence = sub.add_parser("post-sequence", help="Post multiple texts with random delay between them")
    post_sequence.add_argument("--text", action="append", default=[], help="Text to post; repeat for each post")
    post_sequence.add_argument("--text-file", action="append", default=[], help="UTF-8 text file to post; repeatable")
    post_sequence.add_argument("--min-delay", type=int, default=60, help="Minimum delay in seconds")
    post_sequence.add_argument("--max-delay", type=int, default=300, help="Maximum delay in seconds")
    post_sequence.add_argument("--dry-run", action="store_true", help="Print without posting")
    post_sequence.add_argument("--preview", action="store_true", help="Show original and optimized text")
    post_sequence.add_argument("--original", action="store_true", help="Post original text instead of optimized")
    post_sequence.add_argument("--allow-duplicate", action="store_true", help="Bypass duplicate post protection")
    post_sequence.set_defaults(func=command_post_sequence)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
