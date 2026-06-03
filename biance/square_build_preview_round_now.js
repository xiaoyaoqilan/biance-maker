/**
 * Build a Binance Square preview (JSONL) from a fresh 24h snapshot.
 *
 * - Avoids recent tickers (posts + drafts) to reduce duplicates.
 * - Uses only data inside the provided snapshot (realtime fetched this run).
 *
 * Usage:
 *   node .\square_build_preview_round_now.js --snapshot E:\Antigravity\_binance24h_snapshot_xxx.json
 *
 * Output:
 *   prints preview path, and writes a run record JSON.
 */

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const APP_DIR = __dirname;
const DEFAULT_OUT_DIR = APP_DIR;

const POST_LOG_PATH = path.join(APP_DIR, "square_post_log.jsonl");
const DRAFTS_PATH = path.join(APP_DIR, "square_drafts.jsonl");

function now() {
  return new Date();
}
function nowIsoZ() {
  return new Date().toISOString();
}

function randInt(min, maxInclusive) {
  return min + Math.floor(Math.random() * (maxInclusive - min + 1));
}

function parseArgs(argv) {
  const out = { snapshot: "", outDir: DEFAULT_OUT_DIR, min: 3, max: 10, avoidDays: 7 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--snapshot") out.snapshot = argv[++i] || "";
    else if (a === "--out-dir") out.outDir = argv[++i] || out.outDir;
    else if (a === "--min") out.min = Number(argv[++i] || out.min);
    else if (a === "--max") out.max = Number(argv[++i] || out.max);
    else if (a === "--avoid-days") out.avoidDays = Number(argv[++i] || out.avoidDays);
  }
  if (!out.snapshot) throw new Error("Missing --snapshot <path>");
  return out;
}

function readJsonlSafe(p) {
  if (!fs.existsSync(p)) return [];
  const text = fs.readFileSync(p, "utf8");
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
}

function daysAgo(d) {
  return new Date(now().getTime() - d * 24 * 3600 * 1000);
}

function pickRecentTickers({ days = 7 } = {}) {
  const cutoff = daysAgo(days).getTime();
  const out = new Set();

  for (const j of readJsonlSafe(POST_LOG_PATH)) {
    const t = j.created_at ? Date.parse(j.created_at) : null;
    if (t != null && t < cutoff) continue;
    const arr = Array.isArray(j.tickers) ? j.tickers : [];
    for (const x of arr) out.add(String(x).replace(/USDT$/, ""));
    const body = typeof j.body === "string" ? j.body : "";
    for (const m of body.match(/\$([A-Z0-9]{2,12})/g) || []) out.add(m.slice(1));
  }

  for (const j of readJsonlSafe(DRAFTS_PATH)) {
    const t = j.created_at ? Date.parse(j.created_at) : null;
    if (t != null && t < cutoff) continue;
    const arr = Array.isArray(j.keywords) ? j.keywords : [];
    for (const x of arr) out.add(String(x).replace(/USDT$/, ""));
    const body = typeof j.body === "string" ? j.body : "";
    for (const m of body.match(/\$([A-Z0-9]{2,12})/g) || []) out.add(m.slice(1));
  }

  // Include preview files (draft history) to avoid rerunning the same coin/angle.
  for (const p of fs.readdirSync(APP_DIR)) {
    if (!/^square_posts_preview_.*\.jsonl$/i.test(p)) continue;
    const full = path.join(APP_DIR, p);
    const st = fs.statSync(full);
    if (st.mtimeMs < cutoff) continue;
    for (const j of readJsonlSafe(full)) {
      const t = j.created_at ? Date.parse(j.created_at) : null;
      if (t != null && t < cutoff) continue;
      const arr = Array.isArray(j.keywords) ? j.keywords : [];
      for (const x of arr) out.add(String(x).replace(/USDT$/, ""));
      const body = typeof j.body === "string" ? j.body : "";
      for (const m of body.match(/\$([A-Z0-9]{2,12})/g) || []) out.add(m.slice(1));
    }
  }

  return out;
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

  // Micro-priced coins: use a relative step to avoid giant jumps (e.g. 0.000006 -> 0.0001).
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

function hashText(s) {
  return crypto.createHash("sha256").update(String(s), "utf8").digest("hex");
}

function templateBull({ coin, t, lv }) {
  return (
    `${coin} 这波是量拉的，别在中间横着猜。\n` +
    `现价 ${p(t.last)}（24h ${chg(t.changePct)}），高/低 ${p(t.high)} / ${p(t.low)}，量 ${vol(t.quoteVol)}\n\n` +
    `上方：先看 ${p(lv.res1)}，过了再摸 ${p(lv.res2)} -> ${p(lv.res3)}\n` +
    `下方：${p(lv.sup1)} 守住继续扛；失守就看 ${p(lv.sup2)} -> ${p(lv.sup3)}\n\n` +
    `做多只考虑回踩确认，不追绿不追尖。想看更多这种盘面，点个关注。`
  );
}

function templateBear({ coin, t, lv }) {
  return (
    `${coin} 反弹别急着追，先把区间认清。\n` +
    `现价 ${p(t.last)}（24h ${chg(t.changePct)}），高/低 ${p(t.high)} / ${p(t.low)}，量 ${vol(t.quoteVol)}\n\n` +
    `上压：${p(lv.res1)} -> ${p(lv.res2)} -> ${p(lv.res3)}（这里不突破都当反抽）\n` +
    `下沿：${p(lv.sup1)}，再下去就是 ${p(lv.sup2)} -> ${p(lv.sup3)}\n\n` +
    `空头思路就盯“反抽不过 + 跌回区间”。路过点个关注，后面我继续跟。`
  );
}

function templateSqueeze({ coin, t, lv }) {
  return (
    `${coin} 24h 区间卡得很紧，适合等方向。\n` +
    `现价 ${p(t.last)}（24h ${chg(t.changePct)}），高/低 ${p(t.high)} / ${p(t.low)}，量 ${vol(t.quoteVol)}\n\n` +
    `向上：${p(lv.res1)} 突破站稳 -> ${p(lv.res2)} -> ${p(lv.res3)}\n` +
    `向下：${p(lv.sup1)} 跌破走弱 -> ${p(lv.sup2)} -> ${p(lv.sup3)}\n\n` +
    `我就按触发走，不提前押。想要这种“触发点位”就关注一下。`
  );
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

function score(t) {
  const change = Math.abs(Number(t.changePct) || 0);
  const v = Number(t.quoteVol) || 0;
  const vScore = Math.log10(1 + Math.max(0, v) / 1_000_000) * 2;
  return change + vScore;
}

function selectCandidates(snapshot, avoidTickers, targetCount) {
  const pools = []
    .concat(snapshot.topVol || [])
    .concat(snapshot.topGainers || [])
    .concat(snapshot.topLosers || []);

  const uniq = new Map();
  for (const t of pools) {
    if (!t || !t.symbol) continue;
    if (!String(t.symbol).endsWith("USDT")) continue;
    if (t.quoteVol == null || t.last == null || t.high == null || t.low == null) continue;
    const coin = String(t.symbol).replace(/USDT$/, "");
    if (avoidTickers.has(coin)) continue;
    if (t.quoteVol < 6_000_000) continue; // liquidity floor
    if (Math.abs(t.changePct || 0) < 3.0) continue;
    if (Math.abs(t.changePct || 0) > 220) continue;
    const prev = uniq.get(coin);
    if (!prev || score(t) > score(prev)) uniq.set(coin, t);
  }

  const list = [...uniq.values()].sort((a, b) => score(b) - score(a));

  // Prefer a mix: mostly gainers, a couple losers if they are liquid.
  const gainers = list.filter((t) => (t.changePct || 0) > 0);
  const losers = list.filter((t) => (t.changePct || 0) < 0);

  const picks = [];
  for (const t of gainers) {
    picks.push(t);
    if (picks.length >= Math.max(2, Math.floor(targetCount * 0.7))) break;
  }
  for (const t of losers) {
    if (picks.length >= targetCount) break;
    picks.push(t);
  }
  // If still short, fill from remaining by score.
  for (const t of list) {
    if (picks.length >= targetCount) break;
    if (picks.some((x) => x.symbol === t.symbol)) continue;
    picks.push(t);
  }

  return picks.slice(0, targetCount);
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const snapshot = JSON.parse(fs.readFileSync(args.snapshot, "utf8"));

  const target = randInt(Math.max(3, args.min), Math.min(10, args.max));
  const avoid = pickRecentTickers({ days: Math.max(1, Number(args.avoidDays) || 7) });

  const picks = selectCandidates(snapshot, avoid, target);
  if (picks.length < 3) {
    throw new Error(`Not enough fresh signals after de-dup. Picks=${picks.length}`);
  }

  const templates = [templateBull, templateBear, templateSqueeze];
  const at = nowIsoZ();

  const items = [];
  for (let i = 0; i < picks.length; i++) {
    const t = picks[i];
    const coin = "$" + String(t.symbol).replace(/USDT$/, "");
    const lv = buildLevels(t);
    const tpl = templates[randInt(0, templates.length - 1)];
    const body = tpl({ coin, t, lv });
    items.push({
      id: at.replace(/[-:TZ.]/g, "").slice(0, 14) + "_" + String(t.symbol),
      created_at: at,
      source: "binance_24h_snapshot_now",
      snapshot: args.snapshot,
      url: `${snapshot.base || "https://api.binance.com"}/api/v3/ticker/24hr?symbol=${String(t.symbol)}`,
      keywords: [String(t.symbol)],
      body,
      body_fingerprint: hashText(body),
      posted: false,
      realtime: { ticker: t, levels: lv },
    });
  }

  const previewPath = path.join(args.outDir, `square_posts_preview_${at.replace(/[:.]/g, "-")}.jsonl`);
  fs.writeFileSync(previewPath, Buffer.from(items.map((x) => JSON.stringify(x)).join("\n") + "\n", "utf8"));

  const runOut = {
    at,
    snapshot: args.snapshot,
    preview: previewPath,
    target,
    createdCount: items.length,
    created: items.map((x) => ({ id: x.id, symbol: x.keywords[0], fp: x.body_fingerprint })),
    avoidCount: avoid.size,
  };
  const runPath = path.join(args.outDir, `square_round_${at.replace(/[-:TZ.]/g, "").slice(0, 14)}.json`);
  fs.writeFileSync(runPath, Buffer.from(JSON.stringify(runOut, null, 2), "utf8"));

  process.stdout.write(previewPath + "\n");
  process.stdout.write(JSON.stringify(runOut, null, 2) + "\n");
}

main();
