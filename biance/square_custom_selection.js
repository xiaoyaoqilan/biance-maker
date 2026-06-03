/**
 * Custom selection script for specific coins:
 * - Major coins: BTC, SOL, ETH
 * - Top 5 gainers
 * - Top 5 losers  
 * - Top 5 high-volume/potential coins
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
  if (x >= 1e9) return (x / 1e9).toFixed(2).replace(/0+$/, "").replace(/\.$/, "") + "B USDT";
  if (x >= 1e8) return (x / 1e8).toFixed(2).replace(/0+$/, "").replace(/\.$/, "") + "亿 USDT";
  if (x >= 1e6) return Math.round(x / 1e4).toLocaleString("en-US") + "万 USDT";
  return Math.round(x).toLocaleString("en-US") + " USDT";
}

function niceStep(price) {
  const p0 = Math.abs(price);
  if (p0 >= 50000) return 500;
  if (p0 >= 10000) return 200;
  if (p0 >= 1000) return 50;
  if (p0 >= 100) return 5;
  if (p0 >= 10) return 0.5;
  if (p0 >= 1) return 0.05;
  if (p0 >= 0.1) return 0.005;
  if (p0 >= 0.01) return 0.0005;
  if (p0 >= 0.001) return 0.00005;
  if (p0 > 0) {
    const raw = p0 * 0.08;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const scaled = raw / mag;
    const nice = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
    return nice * mag;
  }
  return 0.00000001;
}

function ceilTo(n, step) {
  return Math.ceil(n / step) * step;
}

function floorTo(n, step) {
  return Math.floor(n / step) * step;
}

function buildLevels(t) {
  const step = niceStep(t.last);
  const res1 = t.high;
  const res2 = ceilTo(res1 + step, step);
  const res3 = ceilTo(res2 + step, step);
  const sup1 = t.low;
  const sup2 = floorTo(sup1 - step, step);
  const sup3 = floorTo(sup2 - step, step);
  return { res1, res2, res3, sup1, sup2: Math.max(0, sup2), sup3: Math.max(0, sup3) };
}

function templateAnalysis({ coin, t, lv }) {
  return (
    `${coin} 今日区间分析\n` +
    `现价 ${p(t.last)}（24h ${chg(t.changePct)}），高/低 ${p(t.high)} / ${p(t.low)}，量 ${vol(t.quoteVol)}\n\n` +
    `上方阻力：${p(lv.res1)} -> ${p(lv.res2)} -> ${p(lv.res3)}\n` +
    `下方支撑：${p(lv.sup1)} -> ${p(lv.sup2)} -> ${p(lv.sup3)}\n\n` +
    `操作建议：突破阻力位可考虑跟进，跌破支撑位注意风险控制。关注获取更多分析。`
  );
}

async function main() {
  const at = nowIsoZ();
  const url = `${BASE}/api/v3/ticker/24hr`;
  const raw = await getJson(url);
  
  const all = raw.map(normalizeTicker).filter((t) => 
    t.symbol && t.symbol.endsWith("USDT") && 
    t.quoteVol != null && t.last != null && 
    Math.abs(t.changePct ?? 0) <= 300
  );

  const majorCoins = ["BTCUSDT", "SOLUSDT", "ETHUSDT"];
  
  const topGainers = [...all]
    .filter(t => t.changePct > 0 && t.quoteVol >= 5_000_000)
    .sort((a, b) => (b.changePct ?? -Infinity) - (a.changePct ?? -Infinity))
    .slice(0, 5);

  const topLosers = [...all]
    .filter(t => t.changePct < 0 && t.quoteVol >= 5_000_000)
    .sort((a, b) => (a.changePct ?? Infinity) - (b.changePct ?? Infinity))
    .slice(0, 5);

  const highVolume = [...all]
    .filter(t => t.quoteVol >= 50_000_000)
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
    const coin = "$" + String(t.symbol).replace(/USDT$/, "");
    const lv = buildLevels(t);
    const body = templateAnalysis({ coin, t, lv });
    items.push({
      id: at.replace(/[-:TZ.]/g, "").slice(0, 14) + "_" + String(t.symbol),
      created_at: at,
      source: "custom_selection",
      url: `${BASE}/api/v3/ticker/24hr?symbol=${String(t.symbol)}`,
      keywords: [String(t.symbol)],
      body,
      posted: false,
      realtime: { ticker: t, levels: lv },
    });
  }

  const previewPath = path.join(APP_DIR, `square_posts_preview_${at.replace(/[:.]/g, "-")}.jsonl`);
  fs.writeFileSync(previewPath, Buffer.from(items.map((x) => JSON.stringify(x)).join("\n") + "\n", "utf8"));

  console.log(`Generated ${items.length} posts:`);
  console.log("==================");
  items.forEach((item, idx) => {
    console.log(`\n----- #${idx + 1} ${item.keywords[0]} -----\n${item.body}`);
  });
  console.log(`\nPreview saved to: ${previewPath}`);
}

main().catch((e) => {
  console.error(e && e.stack ? e.stack : e);
  process.exitCode = 1;
});
