/**
 * Fetch Binance spot 24h tickers (realtime) and write a compact snapshot.
 *
 * Output JSON:
 *   { at, base, topVol, topGainers, topLosers }
 *
 * Usage:
 *   node .\square_fetch_binance_24h_snapshot_now.js
 *   node .\square_fetch_binance_24h_snapshot_now.js --out E:\Antigravity\_binance24h_snapshot_xxx.json
 */

const fs = require("fs");
const path = require("path");

const BASE = (process.env.BINANCE_API_BASE || "https://api.binance.com").replace(/\/+$/, "");
const APP_DIR = __dirname;

function nowIsoZ() {
  return new Date().toISOString();
}

function parseArgs(argv) {
  const out = { out: "" };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--out") out.out = argv[++i] || "";
  }
  return out;
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

function pickTop(arr, n, keyFn, filterFn = () => true) {
  return arr
    .filter(filterFn)
    .sort((a, b) => (keyFn(b) ?? -Infinity) - (keyFn(a) ?? -Infinity))
    .slice(0, n);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const at = nowIsoZ();
  const url = `${BASE}/api/v3/ticker/24hr`;
  const raw = await getJson(url);
  if (!Array.isArray(raw)) throw new Error("Unexpected response: not an array");

  const all = raw.map(normalizeTicker).filter((t) => t.symbol && t.symbol.endsWith("USDT"));

  const liquid = (t) =>
    t.quoteVol != null &&
    t.last != null &&
    t.high != null &&
    t.low != null &&
    Math.abs(t.changePct ?? 0) <= 300 &&
    t.quoteVol >= 5_000_000;

  const topVol = pickTop(all, 80, (t) => t.quoteVol, liquid);
  const topGainers = pickTop(all, 80, (t) => t.changePct, liquid);
  const topLosers = pickTop(all, 80, (t) => -t.changePct, liquid);

  const snapshot = { at, base: BASE, topVol, topGainers, topLosers, sourceUrl: url };

  const outPath =
    args.out ||
    path.join(APP_DIR, `_binance24h_snapshot_${at.replace(/[:.]/g, "-")}.json`);

  fs.writeFileSync(outPath, Buffer.from(JSON.stringify(snapshot, null, 2), "utf8"));
  process.stdout.write(outPath + "\n");
}

main().catch((e) => {
  console.error(String(e && e.stack ? e.stack : e));
  process.exitCode = 1;
});

