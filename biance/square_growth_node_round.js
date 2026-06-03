/**
 * Binance Square 24h Growth Loop (Node fallback)
 *
 * Why: some environments don't have a Python runtime, and PowerShell TLS can fail.
 * This script fetches realtime Binance 24h ticker + 1h klines, then appends up to 3
 * high-signal drafts into ./square_drafts.jsonl (UTF-8, no BOM).
 *
 * Usage:
 *   node .\square_growth_node_round.js
 *
 * Notes:
 * - Drafts are only generated; posting still requires BINANCE_SQUARE_OPENAPI_KEY and
 *   the Python helper (square_growth_tool.py post-sequence) per workspace rules.
 */

const fs = require("fs");
const path = require("path");

const APP_DIR = __dirname;
const DEFAULT_DRAFTS_PATH = path.join(APP_DIR, "square_drafts.jsonl");
const DEFAULT_ROUND_OUT_PATH = path.join(APP_DIR, "_round_out.json");
const DEFAULT_POST_LOG_PATH = path.join(APP_DIR, "square_post_log.jsonl");

const BINANCE_API_BASE = (process.env.BINANCE_API_BASE || "https://api.binance.com").replace(
  /\/+$/,
  "",
);

function nowIsoZ() {
  return new Date().toISOString();
}

async function getJson(url) {
  const r = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0" } });
  if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}`);
  return r.json();
}

async function fetchBinance24h(symbol) {
  const url = `${BINANCE_API_BASE}/api/v3/ticker/24hr?symbol=${encodeURIComponent(symbol)}`;
  const j = await getJson(url);
  return {
    symbol: j.symbol,
    last: Number(j.lastPrice),
    changePct: Number(j.priceChangePercent),
    high: Number(j.highPrice),
    low: Number(j.lowPrice),
    quoteVol: Number(j.quoteVolume),
    url,
  };
}

async function fetchKlines(symbol, interval = "1h", limit = 72) {
  const url = `${BINANCE_API_BASE}/api/v3/klines?symbol=${encodeURIComponent(
    symbol,
  )}&interval=${interval}&limit=${limit}`;
  const arr = await getJson(url);
  const klines = arr.map((k) => ({
    openTime: k[0],
    high: Number(k[2]),
    low: Number(k[3]),
    close: Number(k[4]),
  }));
  return { url, klines };
}

function fmtUsd(n) {
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e12) return (n / 1e12).toFixed(2).replace(/\.0+$/, "") + "T";
  if (abs >= 1e9) return (n / 1e9).toFixed(2).replace(/\.0+$/, "") + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(2).replace(/\.0+$/, "") + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(2).replace(/\.0+$/, "") + "K";
  return String(Math.round(n));
}

function fmtPrice(p) {
  if (!Number.isFinite(p)) return "—";
  const s =
    p >= 1000 ? p.toFixed(0) : p >= 100 ? p.toFixed(1) : p >= 1 ? p.toFixed(3) : p >= 0.1 ? p.toFixed(4) : p.toFixed(6);
  return s.replace(/(\.\d*?[1-9])0+$/, "$1").replace(/\.0+$/, "");
}

function roundStepForPrice(p) {
  if (p >= 50_000) return 500;
  if (p >= 10_000) return 200;
  if (p >= 1_000) return 50;
  if (p >= 100) return 5;
  if (p >= 10) return 0.5;
  if (p >= 1) return 0.05;
  if (p >= 0.1) return 0.005;
  // Micro prices: proportional step to avoid unrealistic jumps.
  if (p > 0) return Math.max(p * 0.05, 0.00000001);
  return 0.00000001;
}

function ceilTo(n, step) {
  return Math.ceil(n / step) * step;
}

function floorTo(n, step) {
  return Math.floor(n / step) * step;
}

function distinctHighs(values, minGap) {
  const sorted = [...values].filter(Number.isFinite).sort((a, b) => b - a);
  const levels = [];
  for (const v of sorted) {
    if (levels.every((x) => Math.abs(x - v) >= minGap)) levels.push(v);
    if (levels.length >= 5) break;
  }
  return levels;
}

function distinctLows(values, minGap) {
  const sorted = [...values].filter(Number.isFinite).sort((a, b) => a - b);
  const levels = [];
  for (const v of sorted) {
    if (levels.every((x) => Math.abs(x - v) >= minGap)) levels.push(v);
    if (levels.length >= 5) break;
  }
  return levels;
}

function deriveLevels(ticker, k) {
  const highs = k.klines.map((x) => x.high);
  const lows = k.klines.map((x) => x.low);
  const gap = Math.max(1e-12, ticker.last * 0.001); // 0.1%

  const res1 = ticker.high; // required: 24h high
  const sup1 = ticker.low; // required: 24h low

  const hiLevels = distinctHighs(highs, gap);
  const loLevels = distinctLows(lows, gap);

  // For the 2nd level, prefer distinct prior levels away from the 24h high/low,
  // otherwise fall back to a rounded step to keep the path concrete.
  const step = roundStepForPrice(ticker.last);
  const res2Fallback = ceilTo(res1, step) > res1 ? ceilTo(res1, step) : res1 + step;
  const sup2Fallback = floorTo(sup1, step) < sup1 ? floorTo(sup1, step) : sup1 - step;

  const res2 = hiLevels.find((x) => x > res1 + gap) ?? res2Fallback;
  const sup2 = loLevels.find((x) => x < sup1 - gap) ?? sup2Fallback;

  const range = Math.max(1e-9, ticker.high - ticker.low);
  const pos = (ticker.last - ticker.low) / range;
  let bias = "flat";
  if (ticker.changePct > 1.0 && pos > 0.7) bias = "bull";
  else if (ticker.changePct < -1.0 && pos < 0.3) bias = "bear";

  return { res1, res2, sup1, sup2, bias, klineUrl: k.url };
}

function randPick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

function makePost(ticker, lv) {
  const coin = "$" + ticker.symbol.replace(/USDT$/, "");
  const chg = (ticker.changePct >= 0 ? "+" : "") + ticker.changePct.toFixed(2) + "%";
  const vol = `成交额 ${fmtUsd(ticker.quoteVol)} USDT`;

  const headline = randPick([
    `${coin} 现价 ${fmtPrice(ticker.last)}｜24h ${chg}`,
    `${coin} 24h ${chg}，价 ${fmtPrice(ticker.last)}`,
    `${coin} 盯住：${fmtPrice(ticker.last)}（24h ${chg}）`,
  ]);
  const stats = `高 ${fmtPrice(ticker.high)}｜低 ${fmtPrice(ticker.low)}｜${vol}`;

  const step = roundStepForPrice(ticker.last);
  const res3 = lv.res2 + step;
  const sup3 = Math.max(0, lv.sup2 - step);
  const upPath = `${fmtPrice(lv.res1)} -> ${fmtPrice(lv.res2)} -> ${fmtPrice(res3)}`;
  const dnPath = `${fmtPrice(lv.sup1)} -> ${fmtPrice(lv.sup2)} -> ${fmtPrice(sup3)}`;

  const action = (() => {
    if (lv.bias === "bull") {
      return randPick([
        `计划：不追；回踩 ${fmtPrice(lv.res1)} 不破再考虑，跌破 ${fmtPrice(lv.sup1)} 先撤。`,
        `计划：等回踩；${fmtPrice(lv.res1)} 守住再上，失守 ${fmtPrice(lv.sup1)} 认错。`,
      ]);
    }
    if (lv.bias === "bear") {
      return randPick([
        `计划：反抽不过 ${fmtPrice(lv.res1)} 就别扛；破 ${fmtPrice(lv.sup1)} 只减不抄。`,
        `计划：先防守；${fmtPrice(lv.res1)} 站不回就当弱，跌破 ${fmtPrice(lv.sup1)} 不硬拗。`,
      ]);
    }
    return randPick([
      `计划：先等确认；上破 ${fmtPrice(lv.res1)} 再跟，失守 ${fmtPrice(lv.sup1)} 就撤。`,
      `计划：别急；有效上破 ${fmtPrice(lv.res1)} 再动，跌回 ${fmtPrice(lv.sup1)} 下方先休息。`,
    ]);
  })();

  const hook = randPick([
    "别追涨杀跌，给我一个你在盯的币。",
    "你现在最想我盯哪一个？评论区丢 $币名。",
    "想看下一轮热点，我就按评论区点名来。",
  ]);

  return (
    `${headline}\n${stats}\n\n` +
    `上破路径：${upPath}\n` +
    `跌破路径：${dnPath}\n\n` +
    `${action}\n` +
    `${hook}`
  );
}

function safeReadJsonl(filePath) {
  try {
    if (!fs.existsSync(filePath)) return [];
    const text = fs.readFileSync(filePath, "utf8");
    return text
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean)
      .map((line) => {
        try {
          return JSON.parse(line);
        } catch {
          return null;
        }
      })
      .filter(Boolean);
  } catch {
    return [];
  }
}

function extractTickersFromBody(body) {
  const s = String(body || "");
  const out = new Set();
  const re = /\$([A-Z0-9]{2,15})/g;
  let m = null;
  while ((m = re.exec(s))) out.add(m[1]);
  return [...out];
}

function getAvoidSet({ postLogPath, avoidDays }) {
  const out = new Set();
  const cut = Date.now() - avoidDays * 24 * 60 * 60 * 1000;
  const items = safeReadJsonl(postLogPath);
  for (const it of items) {
    const created = it && it.created_at ? Date.parse(it.created_at) : NaN;
    if (!Number.isFinite(created) || created < cut) continue;
    const tickers = Array.isArray(it.tickers) && it.tickers.length ? it.tickers : extractTickersFromBody(it.body);
    for (const t of tickers) out.add(String(t).replace(/^\$/, "").toUpperCase());
  }
  return out;
}

function getAvoidSetFromJsonlFiles({ files, avoidDays }) {
  const out = new Set();
  const cut = Date.now() - avoidDays * 24 * 60 * 60 * 1000;

  for (const f of files) {
    if (!f) continue;
    let stat = null;
    try {
      if (!fs.existsSync(f)) continue;
      stat = fs.statSync(f);
    } catch {
      continue;
    }

    // For generic previews/drafts without timestamps per line, rely on file mtime.
    const fileRecent = stat && stat.mtimeMs ? stat.mtimeMs >= cut : true;
    const lines = safeReadJsonl(f);
    for (const it of lines) {
      const created = it && it.created_at ? Date.parse(it.created_at) : NaN;
      const recent = Number.isFinite(created) ? created >= cut : fileRecent;
      if (!recent) continue;

      let syms = [];
      if (Array.isArray(it.keywords) && it.keywords.length) syms = it.keywords;
      else if (Array.isArray(it.tickers) && it.tickers.length) syms = it.tickers.map((x) => `${x}USDT`);
      else if (it.realtime && it.realtime.ticker && it.realtime.ticker.symbol) syms = [it.realtime.ticker.symbol];
      else syms = [];

      for (const s of syms) {
        const sym = String(s || "").toUpperCase();
        if (!sym) continue;
        const coin = sym.replace(/USDT$/, "").replace(/^\$/, "");
        if (coin) out.add(coin);
      }
    }
  }

  return out;
}

async function fetchAll24h() {
  const url = `${BINANCE_API_BASE}/api/v3/ticker/24hr`;
  const all = await getJson(url);

  const filtered = all
    .filter((x) => typeof x.symbol === "string" && /^[A-Z0-9]{2,20}USDT$/.test(x.symbol))
    .filter((x) => !/(UP|DOWN|BULL|BEAR)USDT$/.test(x.symbol))
    .map((x) => ({
      symbol: x.symbol,
      last: Number(x.lastPrice),
      changePct: Number(x.priceChangePercent),
      high: Number(x.highPrice),
      low: Number(x.lowPrice),
      quoteVol: Number(x.quoteVolume),
      url: `${BINANCE_API_BASE}/api/v3/ticker/24hr?symbol=${encodeURIComponent(x.symbol)}`,
    }))
    .filter((x) => Number.isFinite(x.changePct))
    .filter((x) => Number.isFinite(x.quoteVol) && x.quoteVol > 0);

  return { url, all: filtered };
}

function appendDraft(filePath, item) {
  fs.appendFileSync(filePath, JSON.stringify(item) + "\n", { encoding: "utf8" });
}

function parseArgs(argv) {
  const out = {
    countMin: 3,
    countMax: 10,
    avoidDays: 7,
    postLogPath: DEFAULT_POST_LOG_PATH,
    previewOut: "",
    snapshotOut: "",
    draftsPath: DEFAULT_DRAFTS_PATH,
    minQuoteVol: 30_000_000,
  };

  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--count-min") out.countMin = Number(argv[++i] || out.countMin);
    else if (a === "--count-max") out.countMax = Number(argv[++i] || out.countMax);
    else if (a === "--avoid-days") out.avoidDays = Number(argv[++i] || out.avoidDays);
    else if (a === "--post-log") out.postLogPath = argv[++i] || out.postLogPath;
    else if (a === "--preview-out") out.previewOut = argv[++i] || out.previewOut;
    else if (a === "--snapshot-out") out.snapshotOut = argv[++i] || out.snapshotOut;
    else if (a === "--drafts") out.draftsPath = argv[++i] || out.draftsPath;
    else if (a === "--min-quote-vol") out.minQuoteVol = Number(argv[++i] || out.minQuoteVol);
  }

  out.countMin = Math.max(1, Math.floor(out.countMin));
  out.countMax = Math.max(out.countMin, Math.floor(out.countMax));
  out.avoidDays = Math.max(0, Number.isFinite(out.avoidDays) ? out.avoidDays : 0);
  return out;
}

function scoreTicker(t) {
  const range = Math.max(1e-9, t.high - t.low);
  const edge = Math.min((t.last - t.low) / range, (t.high - t.last) / range);
  return Math.abs(t.changePct) * 1.5 + (edge < 0.25 ? 0.8 : 0) + Math.log10(1 + t.quoteVol / 10_000_000);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));

  const logDir = path.dirname(args.postLogPath);
  const extraAvoidFiles = [];
  try {
    const files = fs.readdirSync(logDir);
    for (const name of files) {
      if (!/^square_posts_preview_.*\.jsonl$/i.test(name)) continue;
      extraAvoidFiles.push(path.join(logDir, name));
    }
  } catch {
    // ignore
  }

  const avoid = getAvoidSetFromJsonlFiles({
    files: [args.postLogPath, args.draftsPath, ...extraAvoidFiles],
    avoidDays: args.avoidDays,
  });

  const allInfo = await fetchAll24h();
  const all = allInfo.all;

  const stableCoins = new Set(["USDC", "FDUSD", "TUSD", "USDP", "BUSD", "DAI", "USD1", "USDE", "USDS"]);
  const base = all
    .filter((x) => Number.isFinite(x.quoteVol) && x.quoteVol > 0)
    .filter((x) => Number.isFinite(x.changePct))
    .filter((x) => {
      const coin = x.symbol.replace(/USDT$/, "");
      if (stableCoins.has(coin)) return false;
      return !avoid.has(coin);
    });

  const byScore = [...base]
    .filter((x) => x.quoteVol >= args.minQuoteVol)
    .sort((a, b) => scoreTicker(b) - scoreTicker(a));

  const gainers = [...base]
    .filter((x) => x.changePct >= 5 && x.quoteVol >= Math.min(args.minQuoteVol, 10_000_000))
    .sort((a, b) => b.changePct - a.changePct)
    .slice(0, 40);

  const losers = [...base]
    .filter((x) => x.changePct <= -5 && x.quoteVol >= Math.min(args.minQuoteVol, 10_000_000))
    .sort((a, b) => a.changePct - b.changePct)
    .slice(0, 40);

  const byVol = [...base]
    .sort((a, b) => b.quoteVol - a.quoteVol)
    .filter((x) => Math.abs(x.changePct) >= 1.0)
    .slice(0, 40);

  const pool = [...gainers, ...losers, ...byVol, ...byScore].filter(Boolean);

  const candidates = [];
  const seen = new Set();
  for (const c of pool) {
    if (seen.has(c.symbol)) continue;
    seen.add(c.symbol);
    candidates.push(c);
    if (candidates.length >= 200) break;
  }

  const targetCount =
    args.countMin === args.countMax
      ? args.countMin
      : args.countMin + Math.floor(Math.random() * (args.countMax - args.countMin + 1));

  const pickedSymbols = [];
  for (const c of candidates) {
    if (pickedSymbols.length >= targetCount) break;
    if (pickedSymbols.includes(c.symbol)) continue;
    // Skip very quiet movers unless it's extreme volume.
    const quiet = Math.abs(c.changePct) < 1.2 && c.quoteVol < 1_200_000_000;
    if (quiet) continue;
    pickedSymbols.push(c.symbol);
  }

  const picked = [];
  for (const sym of pickedSymbols) {
    const ticker = await fetchBinance24h(sym);
    const k = await fetchKlines(sym, "1h", 72);
    const lv = deriveLevels(ticker, k);
    picked.push({ ticker, lv, from: "binance_24h_ranked", rankUrl: allInfo.url });
  }

  const created = [];
  const previewLines = [];
  for (const p of picked) {
    const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
    const id = `${stamp}_${p.ticker.symbol}`;
    const body = makePost(p.ticker, p.lv);
    const draft = {
      id,
      created_at: nowIsoZ(),
      source: p.from,
      url: p.rankUrl || p.ticker.url,
      keywords: [p.ticker.symbol],
      body,
      posted: false,
      realtime: {
        ticker: p.ticker,
        kline_url: p.lv.klineUrl,
        levels: { res1: p.lv.res1, res2: p.lv.res2, sup1: p.lv.sup1, sup2: p.lv.sup2, bias: p.lv.bias },
      },
    };
    appendDraft(args.draftsPath, draft);
    previewLines.push(draft);
    created.push({ id, symbol: p.ticker.symbol, source: draft.source });
  }

  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const previewOut =
    args.previewOut || path.join(APP_DIR, `square_posts_preview_${stamp.replace("T", "_").replace("Z", "Z")}.jsonl`);
  const snapshotOut = args.snapshotOut || path.join(APP_DIR, `_binance24h_snapshot_${stamp}.json`);

  fs.writeFileSync(previewOut, previewLines.map((x) => JSON.stringify(x)).join("\n") + "\n", { encoding: "utf8" });

  const out = {
    at: nowIsoZ(),
    created,
    keyPresent: Boolean(process.env.BINANCE_SQUARE_OPENAPI_KEY),
    pythonPresent: false,
    avoidDays: args.avoidDays,
    avoidCount: avoid.size,
    avoidSample: [...avoid].slice(0, 30),
    targetCount,
    pickedCount: picked.length,
    pickSymbols: picked.map((x) => x.ticker.symbol),
    previewOut,
    snapshotOut,
    topCandidates: candidates.slice(0, 30),
    url: allInfo.url,
  };

  fs.writeFileSync(snapshotOut, JSON.stringify(out, null, 2), { encoding: "utf8" });
  fs.writeFileSync(DEFAULT_ROUND_OUT_PATH, JSON.stringify(out, null, 2), { encoding: "utf8" });

  process.stdout.write(`Preview: ${previewOut}\nPicked: ${picked.length}/${targetCount}\n\n`);
  previewLines.forEach((x, idx) => {
    process.stdout.write(`----- #${idx + 1} ${x.keywords[0]} -----\n${x.body}\n\n`);
  });
  process.stdout.write(JSON.stringify(out, null, 2));
}

main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
