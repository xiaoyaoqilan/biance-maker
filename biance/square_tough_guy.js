/**
 * Tough Guy Style - No fluff, just signals
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
  };
}

function p(n) {
  const x = Number(n);
  if (!Number.isFinite(x)) return "NA";
  if (Math.abs(x) >= 1000) return x.toLocaleString("en-US", { maximumFractionDigits: 0 });
  if (Math.abs(x) >= 100) return x.toLocaleString("en-US", { maximumFractionDigits: 1 });
  if (Math.abs(x) >= 1) return x.toFixed(2).replace(/\.0+$/, "");
  if (Math.abs(x) >= 0.01) return x.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  return x.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
}

function chg(n) {
  const x = Number(n);
  if (!Number.isFinite(x)) return "NA";
  return (x >= 0 ? "+" : "") + x.toFixed(2) + "%";
}

function vol(q) {
  const x = Number(q);
  if (!Number.isFinite(x)) return "NA";
  if (x >= 1e9) return (x / 1e9).toFixed(2) + "B";
  if (x >= 1e8) return (x / 1e8).toFixed(2) + "亿";
  if (x >= 1e6) return Math.round(x / 1e4).toLocaleString("en-US") + "万";
  return Math.round(x).toLocaleString("en-US");
}

async function fetchKlines(symbol, interval = "4h", limit = 50) {
  const url = `${BASE}/api/v3/klines?symbol=${encodeURIComponent(symbol)}&interval=${interval}&limit=${limit}`;
  const arr = await getJson(url);
  return arr.map((k) => ({ close: Number(k[4]) }));
}

function calculateBollingerBands(klines, period = 20, stdDev = 2) {
  const closes = klines.map((k) => k.close);
  const bands = [];
  for (let i = period - 1; i < closes.length; i++) {
    const slice = closes.slice(i - period + 1, i + 1);
    const avg = slice.reduce((a, b) => a + b, 0) / period;
    const variance = slice.reduce((sum, val) => sum + Math.pow(val - avg, 2), 0) / period;
    const std = Math.sqrt(variance);
    bands.push({ middle: avg, upper: avg + stdDev * std, lower: avg - stdDev * std });
  }
  return bands;
}

function calculateFibonacci(high, low) {
  const range = high - low;
  return { level618: high - range * 0.618, level50: high - range * 0.5 };
}

function getBollingerLevels(klines) {
  const bands = calculateBollingerBands(klines);
  return bands[bands.length - 1] || { middle: 0, upper: 0, lower: 0 };
}

function templateToughGuy({ coin, t, fib, bollinger }) {
  return `${coin} 现价${p(t.last)} | 24h ${chg(t.changePct)} | 量${vol(t.quoteVol)}U\n` +
    `${p(bollinger.upper)} ${p(bollinger.middle)} ${p(bollinger.lower)} ${p(fib.level618)} ${p(fib.level50)}\n` +
    `参考技术指标：回踩${p(fib.level618)}不破可以买，突破${p(bollinger.upper)}站稳可以追，跌破${p(bollinger.lower)}先出来`;
}

async function main() {
  const at = nowIsoZ();
  const raw = await getJson(`${BASE}/api/v3/ticker/24hr`);
  
  const all = raw.map(normalizeTicker).filter((t) => 
    t.symbol && t.symbol.endsWith("USDT") && 
    t.quoteVol != null && t.last != null && 
    Math.abs(t.changePct ?? 0) <= 300 &&
    t.quoteVol >= 5_000_000
  );

  const majorCoins = ["BTCUSDT", "SOLUSDT", "ETHUSDT"];
  const topGainers = [...all].filter(t => t.changePct > 3).sort((a, b) => (b.changePct ?? 0) - (a.changePct ?? 0)).slice(0, 5);
  const topLosers = [...all].filter(t => t.changePct < -3).sort((a, b) => (a.changePct ?? 0) - (b.changePct ?? 0)).slice(0, 5);
  const highVolume = [...all].sort((a, b) => (b.quoteVol ?? 0) - (a.quoteVol ?? 0)).slice(0, 10);

  const seen = new Set();
  const selected = [];

  for (const sym of majorCoins) {
    const t = all.find(x => x.symbol === sym);
    if (t && !seen.has(sym)) { selected.push(t); seen.add(sym); }
  }
  for (const t of topGainers) { if (!seen.has(t.symbol)) { selected.push(t); seen.add(t.symbol); } }
  for (const t of topLosers) { if (!seen.has(t.symbol)) { selected.push(t); seen.add(t.symbol); } }
  for (const t of highVolume) { if (!seen.has(t.symbol) && selected.length < 15) { selected.push(t); seen.add(t.symbol); } }

  const items = [];
  for (const t of selected) {
    const klines = await fetchKlines(t.symbol, "4h", 50);
    const bollinger = getBollingerLevels(klines);
    const fib = calculateFibonacci(t.high, t.low);
    const coin = "$" + String(t.symbol).replace(/USDT$/, "");
    const body = templateToughGuy({ coin, t, fib, bollinger });
    
    items.push({
      id: at.replace(/[-:TZ.]/g, "").slice(0, 14) + "_" + String(t.symbol),
      created_at: at,
      source: "tough_guy",
      url: `${BASE}/api/v3/ticker/24hr?symbol=${String(t.symbol)}`,
      keywords: [String(t.symbol)],
      body,
      posted: false,
      realtime: { ticker: t, fib, bollinger },
    });
  }

  const previewPath = path.join(APP_DIR, `square_posts_preview_${at.replace(/[:.]/g, "-")}.jsonl`);
  fs.writeFileSync(previewPath, Buffer.from(items.map((x) => JSON.stringify(x)).join("\n") + "\n", "utf8"));

  console.log(`=== 老炮看盘 · 共 ${items.length} 篇 ===`);
  items.forEach((item, idx) => console.log(`\n${item.body}`));
  console.log(`\n预览文件：${previewPath}`);
}

main().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exitCode = 1; });
