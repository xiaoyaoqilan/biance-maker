/**
 * Pro Analysis Script with Fibonacci + Bollinger Bands
 * Old-school trader style with persuasive tone
 */

const fs = require("fs");
const path = require("path");

const APP_DIR = __dirname;
const BASE = (process.env.BINANCE_API_BASE || "https://api.binance.com").replace(/\/+$/, "");

function nowIsoZ() {
  return new Date().toISOString();
}

async function getJson(url) {
  const r = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0" } });
  const raw = await r.text();
  if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}: ${raw.slice(0, 200)}`);
  return JSON.parse(raw);
}

function toNum(x) {
  const n = Number(x);
  return Number.isFinite(n) ? n : null;
}

function normalizeTicker(t) {
  return {
    symbol: String(t.symbol),
    last: toNum(t.lastPrice),
    changePct: toNum(t.priceChangePercent),
    high: toNum(t.highPrice),
    low: toNum(t.lowPrice),
    quoteVol: toNum(t.quoteVolume),
    closeTime: t.closeTime ? Number(t.closeTime) : null,
  };
}

function p(n) {
  const x = Number(n);
  if (!Number.isFinite(x)) return "NA";
  if (Math.abs(x) >= 1000) return x.toLocaleString("en-US", { maximumFractionDigits: 0 });
  if (Math.abs(x) >= 100) return x.toLocaleString("en-US", { maximumFractionDigits: 1 });
  if (Math.abs(x) >= 1) return x.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
  if (Math.abs(x) >= 0.1) return x.toFixed(5).replace(/0+$/, "").replace(/\.$/, "");
  return x.toFixed(8).replace(/0+$/, "").replace(/\.$/, "");
}

function chg(n) {
  const x = Number(n);
  if (!Number.isFinite(x)) return "NA";
  return (x >= 0 ? "+" : "") + x.toFixed(2) + "%";
}

function vol(q) {
  const x = Number(q);
  if (!Number.isFinite(x)) return "NA";
  if (x >= 1e9) return (x / 1e9).toFixed(2).replace(/0+$/, "").replace(/\.$/, "") + "B";
  if (x >= 1e8) return (x / 1e8).toFixed(2).replace(/0+$/, "").replace(/\.$/, "") + "亿";
  if (x >= 1e6) return Math.round(x / 1e4).toLocaleString("en-US") + "万";
  return Math.round(x).toLocaleString("en-US");
}

async function fetchKlines(symbol, interval = "4h", limit = 200) {
  const url = `${BASE}/api/v3/klines?symbol=${encodeURIComponent(symbol)}&interval=${interval}&limit=${limit}`;
  const arr = await getJson(url);
  return arr.map((k) => ({
    open: Number(k[1]),
    high: Number(k[2]),
    low: Number(k[3]),
    close: Number(k[4]),
    volume: Number(k[5]),
  }));
}

function calculateBollingerBands(klines, period = 20, stdDev = 2) {
  const closes = klines.map((k) => k.close);
  const bands = [];
  
  for (let i = period - 1; i < closes.length; i++) {
    const slice = closes.slice(i - period + 1, i + 1);
    const avg = slice.reduce((a, b) => a + b, 0) / period;
    const variance = slice.reduce((sum, val) => sum + Math.pow(val - avg, 2), 0) / period;
    const std = Math.sqrt(variance);
    bands.push({
      middle: avg,
      upper: avg + stdDev * std,
      lower: avg - stdDev * std,
    });
  }
  
  return bands;
}

function calculateFibonacci(high, low) {
  const range = high - low;
  return {
    high: high,
    low: low,
    level0: high,
    level236: high - range * 0.236,
    level382: high - range * 0.382,
    level50: high - range * 0.5,
    level618: high - range * 0.618,
    level786: high - range * 0.786,
    level100: low,
  };
}

function getBollingerLevels(klines) {
  const bands1h = calculateBollingerBands(klines);
  const latest = bands1h[bands1h.length - 1] || { middle: 0, upper: 0, lower: 0 };
  const prev = bands1h[bands1h.length - 2] || latest;
  
  return {
    upper: latest.upper,
    middle: latest.middle,
    lower: latest.lower,
    prevUpper: prev.upper,
    prevLower: prev.lower,
  };
}

function templateOldSchool({ coin, t, fib, bollinger }) {
  const trend = t.changePct > 3 ? "多头" : t.changePct < -3 ? "空头" : "震荡";
  
  return (
    `${coin} | 老炮带你看盘\n` +
    `现价 ${p(t.last)} ｜24h ${chg(t.changePct)} ｜量 ${vol(t.quoteVol)} USDT\n\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    `【布林带 · 4H】\n` +
    `上轨 ${p(bollinger.upper)} ｜中轨 ${p(bollinger.middle)} ｜下轨 ${p(bollinger.lower)}\n\n` +
    `【斐波那契回撤】\n` +
    `顶 ${p(fib.high)} -> 0.236 ${p(fib.level236)} -> 0.382 ${p(fib.level382)}\n` +
    `0.5 ${p(fib.level50)} -> 0.618 ${p(fib.level618)} -> 底 ${p(fib.low)}\n\n` +
    `━━━━━━━━━━━━━━━━━━━━\n` +
    (trend === "多头" 
      ? `老炮观点：这波有量，别在半山腰下车！\n` +
        `支撑看 ${p(fib.level618)}，不破继续拿；\n` +
        `目标先摸布林上轨 ${p(bollinger.upper)}，过了再看 ${p(fib.high)}。\n\n` +
        `听哥一句：回踩确认再动手，别追涨！`
      : trend === "空头"
        ? `老炮观点：别急着抄底，底不是猜出来的！\n` +
          `压力看 ${p(fib.level382)}，不过继续空；\n` +
          `支撑先看布林下轨 ${p(bollinger.lower)}，破了再看 ${p(fib.low)}。\n\n` +
          `听哥一句：反抽不过就别扛，保住本金最重要！`
        : `老炮观点：区间震荡，耐心等方向！\n` +
          `上沿 ${p(bollinger.upper)} 突破站稳跟多；\n` +
          `下沿 ${p(bollinger.lower)} 跌破走弱做空。\n\n` +
          `听哥一句：不提前押注，按规则出牌！`
    ) + `\n\n` +
    `想跟老炮一起抓机会？点个关注，明天继续唠！`
  );
}

async function main() {
  const at = nowIsoZ();
  const url = `${BASE}/api/v3/ticker/24hr`;
  const raw = await getJson(url);
  
  const all = raw.map(normalizeTicker).filter((t) => 
    t.symbol && t.symbol.endsWith("USDT") && 
    t.quoteVol != null && t.last != null && 
    Math.abs(t.changePct ?? 0) <= 300 &&
    t.quoteVol >= 5_000_000
  );

  const majorCoins = ["BTCUSDT", "SOLUSDT", "ETHUSDT"];
  
  const topGainers = [...all]
    .filter(t => t.changePct > 0)
    .sort((a, b) => (b.changePct ?? -Infinity) - (a.changePct ?? -Infinity))
    .slice(0, 5);

  const topLosers = [...all]
    .filter(t => t.changePct < 0)
    .sort((a, b) => (a.changePct ?? Infinity) - (b.changePct ?? Infinity))
    .slice(0, 5);

  const highVolume = [...all]
    .sort((a, b) => (b.quoteVol ?? 0) - (a.quoteVol ?? 0))
    .slice(0, 10);

  const seen = new Set();
  const selected = [];

  for (const sym of majorCoins) {
    const t = all.find(x => x.symbol === sym);
    if (t && !seen.has(sym)) {
      selected.push(t);
      seen.add(sym);
    }
  }

  for (const t of topGainers) {
    if (!seen.has(t.symbol)) {
      selected.push(t);
      seen.add(t.symbol);
    }
  }

  for (const t of topLosers) {
    if (!seen.has(t.symbol)) {
      selected.push(t);
      seen.add(t.symbol);
    }
  }

  for (const t of highVolume) {
    if (!seen.has(t.symbol) && selected.length < 18) {
      selected.push(t);
      seen.add(t.symbol);
    }
  }

  const items = [];
  for (const t of selected) {
    const klines = await fetchKlines(t.symbol, "4h", 50);
    const bollinger = getBollingerLevels(klines);
    const fib = calculateFibonacci(t.high, t.low);
    const coin = "$" + String(t.symbol).replace(/USDT$/, "");
    const body = templateOldSchool({ coin, t, fib, bollinger });
    
    items.push({
      id: at.replace(/[-:TZ.]/g, "").slice(0, 14) + "_" + String(t.symbol),
      created_at: at,
      source: "pro_analysis",
      url: `${BASE}/api/v3/ticker/24hr?symbol=${String(t.symbol)}`,
      keywords: [String(t.symbol)],
      body,
      posted: false,
      realtime: { ticker: t, fib, bollinger },
    });
  }

  const previewPath = path.join(APP_DIR, `square_posts_preview_${at.replace(/[:.]/g, "-")}.jsonl`);
  fs.writeFileSync(previewPath, Buffer.from(items.map((x) => JSON.stringify(x)).join("\n") + "\n", "utf8"));

  console.log(`=== 老炮分析 · 共 ${items.length} 篇 ===`);
  console.log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
  items.forEach((item, idx) => {
    console.log(`\n【#${idx + 1}】${item.keywords[0]}`);
    console.log("────────────────────────────────");
    console.log(item.body);
    console.log("────────────────────────────────");
  });
  console.log(`\n预览文件：${previewPath}`);
}

main().catch((e) => {
  console.error(e && e.stack ? e.stack : e);
  process.exitCode = 1;
});
