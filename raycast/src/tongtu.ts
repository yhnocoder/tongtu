import { getPreferenceValues } from "@raycast/api";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const STAGES = ["fetch", "precompile", "mask", "survey", "translate", "review", "compile"] as const;
export const ROOT = process.env.TONGTU_HOME || path.join(os.homedir(), ".local/share/tongtu");
const CACHE = path.join(os.homedir(), ".cache/tongtu-raycast");
const ARXIV_ID = /\d{4}\.\d{4,5}(v\d+)?/;
const SESSION_LOGS: Record<string, string> = { precompile: "precompile-fix.jsonl", review: "review.jsonl", compile: "compile-fix.jsonl" };
export const ASPECT: Record<string, "3/4" | "3/2" | "16/9"> = { full: "3/4", half: "3/2", third: "16/9" };
const HEIGHT_PER_WIDTH: Record<string, number> = { full: 4 / 3, half: 2 / 3, third: 9 / 16 };
const ENV = {
  HOME: os.homedir(),
  USER: os.userInfo().username,
  LOGNAME: os.userInfo().username,
  PATH: `${os.homedir()}/.local/bin:/opt/homebrew/bin:/Library/TeX/texbin:/usr/bin:/bin`,
  LANG: "en_US.UTF-8",
  TMPDIR: os.tmpdir(),
  ...(process.env.TONGTU_HOME ? { TONGTU_HOME: process.env.TONGTU_HOME } : {}),
};

export type Prefs = {
  repo: string;
  view: "grid" | "list";
  groupBy: "none" | "added" | "submitted" | "category";
  thumbnail: "full" | "half" | "third";
  margin: string;
  columns: string;
  recentDays: string;
};

export const prefs = () => getPreferenceValues<Prefs>();

export type Stage = { name: string; status: string; message: string; warnings: string[] };

export type Meta = { title: string; authors: string[]; category: string; published: string };

export type Paper = Meta & {
  id: string;
  dir: string;
  pdf: string;
  pgid: number | null;
  status: "running" | "ok" | "failed" | "partial";
  stage: string;
  done: number;
  message: string;
  stages: Stage[];
  events: string[];
  translated: [number, number] | null;
  added: number;
  mtime: number;
};

const q = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`;
const metaPath = (id: string) => path.join(CACHE, `${id}.json`);
export const normalize = (paper: string) => paper.match(ARXIV_ID)?.[0] ?? paper.trim();

export function start(paper: string) {
  const id = normalize(paper);
  const repo = prefs().repo.replace(/^~/, os.homedir());
  const dir = path.join(ROOT, id);
  const script = [
    `if uv run --directory ${q(repo)} tongtu run ${q(id)} > /dev/null 2>&1`,
    `then terminal-notifier -title Tongtu -message ${q(`${id} translated`)} -open ${q(`file://${dir}/out`)}`,
    `else terminal-notifier -title Tongtu -message ${q(`${id} failed`)} -open ${q(`file://${dir}`)}; fi`,
  ].join("; ");
  spawn("/bin/zsh", ["-c", script], { detached: true, stdio: "ignore", env: ENV }).unref();
}

export function stop(paper: Paper) {
  if (paper.pgid) process.kill(-paper.pgid, "SIGTERM");
}

export function remove(paper: Paper) {
  stop(paper);
  fs.rmSync(paper.dir, { recursive: true, force: true });
  for (const file of fs.readdirSync(CACHE)) if (file.startsWith(`${paper.id}.`) || file.startsWith(`${paper.id}-`)) fs.rmSync(path.join(CACHE, file));
}

export function quickLook(file: string) {
  spawn("qlmanage", ["-p", file], { detached: true, stdio: "ignore" }).unref();
}

function runningGroups(): Map<string, number> {
  const groups = new Map<string, number>();
  for (const line of execFileSync("ps", ["-ax", "-o", "pgid=,command="], { encoding: "utf8" }).split("\n")) {
    const match = line.match(/^\s*(\d+) .*tongtu run (\S+)$/);
    if (match) groups.set(match[2], Number(match[1]));
  }
  return groups;
}

function read(file: string): string {
  try {
    return fs.readFileSync(file, "utf8");
  } catch {
    return "";
  }
}

function mtime(file: string): number {
  try {
    return fs.statSync(file).mtimeMs;
  } catch {
    return 0;
  }
}

function translateProgress(dir: string): [number, number] | null {
  const brief = read(path.join(dir, "build/brief.json"));
  if (!brief) return null;
  const total = JSON.parse(brief).chunks.filter((c: { translatable_chars: number }) => c.translatable_chars).length;
  let done: string[] = [];
  try {
    done = fs.readdirSync(path.join(dir, "logs")).flatMap((f) => f.match(/^translate-(.+)-\d+\.json$/)?.[1] ?? []);
  } catch {}
  return [new Set(done).size, total];
}

function sessionEvents(dir: string, stage: string): string[] {
  const file = SESSION_LOGS[stage];
  if (!file) return [];
  const events: string[] = [];
  for (const line of read(path.join(dir, "logs", file)).split("\n")) {
    if (!line.startsWith('{"type":"assistant"')) continue;
    for (const block of JSON.parse(line).message?.content ?? []) {
      if (block.type === "text" && block.text.trim()) events.push(block.text.trim());
      if (block.type === "tool_use") events.push(`${block.name}: ${block.input.command ?? block.input.file_path ?? block.input.description ?? ""}`);
    }
  }
  return events.slice(-8);
}

function inspect(id: string, pgid: number | null): Paper {
  const dir = path.join(ROOT, id);
  const stages: Stage[] = [];
  let latest = 0;
  let failed: Stage | null = null;
  for (const name of STAGES) {
    const file = path.join(dir, "build/manifests", `${name}.json`);
    const text = read(file);
    if (!text) break;
    const manifest = JSON.parse(text);
    latest = Math.max(latest, mtime(file));
    stages.push({ name, status: manifest.status, message: manifest.message ?? "", warnings: manifest.warnings ?? [] });
    if (manifest.status !== "ok") {
      failed = stages[stages.length - 1];
      break;
    }
  }
  const done = stages.length - (failed ? 1 : 0);
  const stage = failed?.name ?? STAGES[done] ?? "compile";
  const meta: Meta = { title: "", authors: [], category: "", published: "", ...JSON.parse(read(metaPath(id)) || "{}") };
  return {
    ...meta,
    id,
    dir,
    pdf: path.join(dir, "out/zh.pdf"),
    pgid,
    status: pgid ? "running" : failed ? "failed" : done === STAGES.length ? "ok" : "partial",
    stage,
    done,
    message: failed ? `${failed.status} ${failed.message}`.trim() : "",
    stages,
    events: sessionEvents(dir, stage),
    translated: stage === "translate" ? translateProgress(dir) : null,
    added: mtime(path.join(dir, "build/manifests/fetch.json")) || mtime(dir),
    mtime: latest || mtime(dir),
  };
}

export function scan(): Paper[] {
  let ids: string[] = [];
  try {
    ids = fs.readdirSync(ROOT, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name);
  } catch {}
  const groups = runningGroups();
  return ids.map((id) => inspect(id, groups.get(id) ?? null)).sort((a, b) => b.mtime - a.mtime);
}

export function groupKey(p: Paper, by: Prefs["groupBy"]): string {
  if (by === "added") return new Date(p.added).toISOString().slice(0, 7);
  if (by === "submitted") {
    const m = p.id.match(/^(\d{2})(\d{2})\./);
    return m ? `20${m[1]}-${m[2]}` : "other";
  }
  if (by === "category") return p.category || "unknown";
  return "";
}

export async function fetchMeta(papers: Paper[]): Promise<boolean> {
  const missing = papers.filter((p) => !p.title && ARXIV_ID.test(p.id)).map((p) => p.id);
  if (!missing.length) return false;
  const xml = await (await fetch(`https://export.arxiv.org/api/query?id_list=${missing.join(",")}&max_results=${missing.length}`)).text();
  fs.mkdirSync(CACHE, { recursive: true });
  for (const entry of xml.split("<entry>").slice(1)) {
    const id = entry.match(/<id>http:\/\/arxiv\.org\/abs\/([^<]+)<\/id>/)?.[1] ?? "";
    const match = missing.find((m) => id === m || id.startsWith(m + "v"));
    const title = entry.match(/<title>([\s\S]*?)<\/title>/)?.[1].replace(/\s+/g, " ").trim();
    if (!match || !title) continue;
    const meta: Meta = {
      title,
      authors: [...entry.matchAll(/<name>([^<]*)<\/name>/g)].map((m) => m[1].trim()),
      category: entry.match(/<arxiv:primary_category[^>]*term="([^"]+)"/)?.[1] ?? "",
      published: entry.match(/<published>([^<]*)<\/published>/)?.[1].slice(0, 10) ?? "",
    };
    fs.writeFileSync(metaPath(match), JSON.stringify(meta));
  }
  return true;
}

const rendering = new Set<string>();

export function thumbnail(paper: Paper): string | null {
  const { thumbnail: mode, margin } = prefs();
  const png = path.join(CACHE, `${paper.id}-${mode}-${margin}.png`);
  const pdfTime = mtime(paper.pdf);
  if (!pdfTime) return null;
  if (mtime(png) >= pdfTime) return png;
  if (rendering.has(png)) return null;
  rendering.add(png);
  const m = Math.min(Math.max(Number(margin) || 0, 0), 40) / 100;
  const ratio = HEIGHT_PER_WIDTH[mode] ?? 4 / 3;
  const tmp = `${png}.tmp.png`;
  const script = [
    `mkdir -p ${q(CACHE)}`,
    `sips -s format png -Z 1200 ${q(paper.pdf)} --out ${q(tmp)}`,
    `read w h <<< "$(sips -g pixelWidth -g pixelHeight ${q(tmp)} | awk '/pixel/ {printf "%s ", $2}')"`,
    `cw=$((w * ${Math.round((1 - 2 * m) * 1000)} / 1000)); ch=$((cw * ${Math.round(ratio * 1000)} / 1000)); max=$((h - h * ${Math.round(m * 1000)} / 1000)); ch=$((ch > max ? max : ch))`,
    `sips --cropOffset $((h * ${Math.round(m * 1000)} / 1000)) $((w * ${Math.round(m * 1000)} / 1000)) --cropToHeightWidth $ch $cw ${q(tmp)}`,
    `mv ${q(tmp)} ${q(png)}`,
  ].join(" && ");
  spawn("/bin/zsh", ["-c", script], { detached: true, stdio: "ignore" }).on("exit", () => rendering.delete(png)).unref();
  return null;
}
