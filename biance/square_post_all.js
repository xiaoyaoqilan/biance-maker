/**
 * Post all without visibility check
 */

const fs = require("fs");
const path = require("path");

const APP_DIR = __dirname;
const POST_ENDPOINT = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add";
const POST_LOG_PATH = path.join(APP_DIR, "square_post_log.jsonl");

function nowIsoZ() {
  return new Date().toISOString();
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function randInt(min, maxInclusive) {
  return min + Math.floor(Math.random() * (maxInclusive - min + 1));
}

function readJsonl(filePath) {
  const text = fs.readFileSync(filePath, "utf8");
  return text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean).map((line) => JSON.parse(line));
}

async function postText(body, key) {
  const payload = JSON.stringify({ bodyTextOnly: body });
  const r = await fetch(POST_ENDPOINT, {
    method: "POST",
    headers: {
      "X-Square-OpenAPI-Key": key,
      "Content-Type": "application/json; charset=utf-8",
      clienttype: "binanceSkill",
    },
    body: payload,
  });
  const raw = await r.text();
  let json = null;
  try { json = JSON.parse(raw); } 
  catch { json = { success: false, code: String(r.status), message: "Non-JSON", raw }; }
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${raw.slice(0, 500)}`);
  return json;
}

function appendLog(record) {
  fs.appendFileSync(POST_LOG_PATH, JSON.stringify(record) + "\n", { encoding: "utf8" });
}

function parseArgs(argv) {
  const out = { preview: "", minDelay: 20, maxDelay: 40 };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--preview") out.preview = argv[++i] || "";
    else if (argv[i] === "--min-delay") out.minDelay = Number(argv[++i] || out.minDelay);
    else if (argv[i] === "--max-delay") out.maxDelay = Number(argv[++i] || out.maxDelay);
  }
  if (!out.preview) throw new Error("Missing --preview <jsonl path>");
  return out;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const key = (process.env.BINANCE_SQUARE_OPENAPI_KEY || "").trim();
  if (!key) throw new Error("Missing env BINANCE_SQUARE_OPENAPI_KEY");

  const items = readJsonl(args.preview);
  const posts = items.map((x) => String(x.body || "").trim()).filter(Boolean);
  if (!posts.length) throw new Error("No posts found");

  console.log(`Total: ${posts.length} posts\n`);

  for (let i = 0; i < posts.length; i++) {
    const body = posts[i];
    const created_at = nowIsoZ();
    let result = null, post_id = null, share_link = null;

    try {
      result = await postText(body, key);
      const data = result && result.data ? result.data : null;
      post_id = data && data.id ? data.id : null;
      share_link = data && data.shareLink ? data.shareLink : null;
    } catch (e) {
      result = { success: false, error: String(e && e.message ? e.message : e) };
    }

    appendLog({ created_at, body, result, post_id, share_link, preview: args.preview });
    console.log(`Posted #${i + 1}/${posts.length}: id=${post_id || "FAILED"}`);

    if (i < posts.length - 1) {
      const delaySec = randInt(args.minDelay, args.maxDelay);
      console.log(`Waiting ${delaySec}s...\n`);
      await sleep(delaySec * 1000);
    }
  }
  console.log("\nAll posts completed!");
}

main().catch((e) => { console.error(e.message || e); process.exitCode = 1; });
