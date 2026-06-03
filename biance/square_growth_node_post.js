/**
 * Post Binance Square texts (Node).
 *
 * Why: some environments don't have Python available on PATH.
 *
 * Requires:
 *   - env BINANCE_SQUARE_OPENAPI_KEY
 *
 * Usage:
 *   node .\square_growth_node_post.js --preview E:\Antigravity\square_posts_preview_xxx.jsonl --dry-run
 *   node .\square_growth_node_post.js --preview E:\Antigravity\square_posts_preview_xxx.jsonl --yes
 *
 * Notes:
 * - Enforces 20–60s random delay between posts.
 * - If API returns success but the public post page isn't visible, stops the run.
 * - Logs every attempt to ./square_post_log.jsonl (UTF-8, JSONL).
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
  return text
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((line) => JSON.parse(line));
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
  try {
    json = JSON.parse(raw);
  } catch {
    json = { success: false, code: String(r.status), message: "Non-JSON response", raw };
  }
  if (!r.ok) {
    throw new Error(`HTTP ${r.status}: ${raw.slice(0, 500)}`);
  }
  return json;
}

async function checkPublicVisible(postId) {
  if (!postId) return { ok: false, status: null };
  const url = `https://www.binance.com/square/post/${postId}`;
  try {
    const r = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0" } });
    const html = await r.text();
    const ok = Boolean(r.ok && html && !/not found|404/i.test(html) && html.includes(String(postId)));
    return { ok, status: r.status, url };
  } catch (e) {
    return { ok: false, status: null, url, error: String(e && e.message ? e.message : e) };
  }
}

function appendLog(record) {
  fs.appendFileSync(POST_LOG_PATH, JSON.stringify(record) + "\n", { encoding: "utf8" });
}

function parseArgs(argv) {
  const out = { preview: "", dryRun: false, yes: false, minDelay: 20, maxDelay: 60, source: "node-post-sequence" };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--preview") out.preview = argv[++i] || "";
    else if (a === "--dry-run") out.dryRun = true;
    else if (a === "--yes") out.yes = true;
    else if (a === "--min-delay") out.minDelay = Number(argv[++i] || out.minDelay);
    else if (a === "--max-delay") out.maxDelay = Number(argv[++i] || out.maxDelay);
    else if (a === "--source") out.source = argv[++i] || out.source;
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
  if (!posts.length) throw new Error("No posts found in preview file.");

  process.stdout.write(`Preview: ${args.preview}\nCount: ${posts.length}\n\n`);
  posts.forEach((p, idx) => process.stdout.write(`----- #${idx + 1} -----\n${p}\n\n`));

  if (args.dryRun) {
    process.stdout.write("Dry run: not posting.\n");
    return;
  }
  if (!args.yes) {
    throw new Error("Refusing to post without --yes (safety).");
  }

  for (let i = 0; i < posts.length; i++) {
    const body = posts[i];
    const created_at = nowIsoZ();

    let result = null;
    let post_id = null;
    let share_link = null;
    let post_url = null;
    let visible = null;

    try {
      result = await postText(body, key);
      const data = result && result.data ? result.data : null;
      post_id = data && data.id ? data.id : null;
      share_link = data && data.shareLink ? data.shareLink : null;
      post_url = post_id ? `https://www.binance.com/square/post/${post_id}` : null;
      visible = await checkPublicVisible(post_id);
    } catch (e) {
      result = { success: false, error: String(e && e.message ? e.message : e) };
    }

    appendLog({
      created_at,
      source: args.source,
      body,
      result,
      post_id,
      post_url,
      share_link,
      public_visible: visible,
      preview: args.preview,
    });

    process.stdout.write(
      `Posted #${i + 1}/${posts.length}: id=${post_id || "NA"} visible=${visible && visible.ok ? "yes" : "no"}\n`,
    );

    if (result && result.success && visible && !visible.ok) {
      process.stdout.write("API success but public page not visible; stopping to avoid spam.\n");
      break;
    }

    if (i < posts.length - 1) {
      const delaySec = randInt(args.minDelay, args.maxDelay);
      process.stdout.write(`Waiting ${delaySec}s...\n`);
      await sleep(delaySec * 1000);
    }
  }
}

main().catch((e) => {
  console.error(e && e.message ? e.message : e);
  process.exitCode = 1;
});
