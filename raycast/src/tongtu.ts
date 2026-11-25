import { getPreferenceValues } from "@raycast/api";
import { execFile, execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

export const STAGES = ["fetch", "precompile", "mask", "translate", "compile"] as const;
export const ROOT = path.join(process.env.TONGTU_HOME || path.join(os.homedir(), ".tongtu"), "papers");
const CACHE = path.join(os.homedir(), ".cache/tongtu-raycast");
const ARXIV_ID = /\d{4}\.\d{4,5}(v\d+)?/;
const ARXIV_DIR = /^(\d{4}\.\d{4,5}|[a-z-]+(\.[A-Z]{2})?_\d{7})(v\d+)?$/;
const SESSION_LOGS: Record<string, string> = { precompile: "precompile-fix.jsonl", translate: "translate.jsonl", compile: "compile-fix.jsonl" };
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

type StageState = Stage & { outputs_present: boolean; manifest: string | null };

type PaperStatus = {
  workdir: string;
  arxiv_id: string | null;
  version: number | null;
  running: boolean;
  pid: number | null;
  started_at: string | null;
  mode: string | null;
  stages: StageState[];
  pdf: string | null;
  meta: string | null;
  glossary: string | null;
  notes: string | null;
};

type PaperMeta = { title: string; authors: string[]; primary_category: string; published: string };

const ABSENT = "absent";
const RUN_MARKER = "running.json";

export type Meta = { title: string; authors: string[]; category: string; published: string };

export type Paper = Meta & {
  id: string;
  dir: string;
  pdf: string;
  pid: number | null;
  status: "running" | "ok" | "failed" | "partial";
  stage: string;
  done: number;
  message: string;
  stages: Stage[];
  events: string[];
  started: number | null;
  added: number;
  mtime: number;
};

const q = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`;
const statusPath = (id: string) => path.join(CACHE, `${id}.status.json`);
const run = promisify(execFile);
const repoDir = () => prefs().repo.replace(/^~/, os.homedir());
export const normalize = (paper: string) => paper.match(ARXIV_ID)?.[0] ?? paper.trim();

function unversioned(name: string): boolean {
  const match = ARXIV_DIR.exec(name);
  return match !== null && match[3] === undefined;
}

export function start(paper: string) {
  const id = normalize(paper);
  const repo = repoDir();
  const name = id.replace(/\//g, "_");
  const locate = unversioned(name)
    ? `dirs=(${q(ROOT)}/${q(name)}v*(N/n)); dir=\${dirs[-1]:-${q(ROOT)}}`
    : `dir=${q(path.join(ROOT, name))}`;
  const script = [
    `uv run --directory ${q(repo)} tongtu run ${q(id)} > /dev/null 2>&1; code=$?`,
    locate,
    `if [ $code -eq 0 ]`,
    `then terminal-notifier -title Tongtu -message ${q(`${id} translated`)} -open "file://$dir/out"`,
    `else terminal-notifier -title Tongtu -message ${q(`${id} failed`)} -open "file://$dir"; fi`,
  ].join("; ");
  spawn("/bin/zsh", ["-c", script], { detached: true, stdio: "ignore", env: ENV }).unref();
}

export function stop(paper: Paper) {
  if (!paper.pid) return;
  try {
    const pgid = Number(execFileSync("ps", ["-o", "pgid=", "-p", String(paper.pid)], { encoding: "utf8" }).trim());
    if (pgid) process.kill(-pgid, "SIGTERM");
  } catch {}
}

export function remove(paper: Paper) {
  stop(paper);
  fs.rmSync(paper.dir, { recursive: true, force: true });
  for (const file of fs.readdirSync(CACHE)) if (file.startsWith(`${paper.id}.`) || file.startsWith(`${paper.id}-`)) fs.rmSync(path.join(CACHE, file));
}

export function quickLook(file: string) {
  spawn("qlmanage", ["-p", file], { detached: true, stdio: "ignore" }).unref();
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

function readMeta(file: string | null | undefined): Meta {
  const meta = JSON.parse((file && read(file)) || "null") as PaperMeta | null;
  if (!meta) return { title: "", authors: [], category: "", published: "" };
  return { title: meta.title, authors: meta.authors, category: meta.primary_category, published: meta.published.slice(0, 10) };
}

function manifestsKey(dir: string): string {
  const manifests = path.join(dir, "build/manifests");
  const files = [manifests, ...STAGES.map((name) => path.join(manifests, `${name}.json`)), path.join(dir, RUN_MARKER)];
  return files.map(mtime).join(",");
}

async function paperStatus(id: string, dir: string): Promise<PaperStatus> {
  const key = manifestsKey(dir);
  const cached = JSON.parse(read(statusPath(id)) || "null") as { key: string; status: PaperStatus } | null;
  if (cached && cached.key === key && !cached.status.running) return cached.status;
  const command = `uv run --directory ${q(repoDir())} tongtu status ${q(id)} --workdir ${q(dir)} --json`;
  const { stdout } = await run("/bin/zsh", ["-c", command], { env: ENV, encoding: "utf8" });
  const status = JSON.parse(stdout) as PaperStatus;
  fs.mkdirSync(CACHE, { recursive: true });
  fs.writeFileSync(statusPath(id), JSON.stringify({ key, status }));
  return status;
}

function sessionEvents(dir: string, stage: string): string[] {
  const file = SESSION_LOGS[stage];
  if (!file) return [];
  const events: string[] = [];
  for (const line of read(path.join(dir, "logs", file)).split("\n")) {
    if (!line.startsWith("{")) continue;
    try {
      const event = JSON.parse(line);
      if (event.type === "tool" && typeof event.summary === "string") events.push(event.summary);
    } catch {}
  }
  return events.slice(-8);
}

async function inspect(id: string): Promise<Paper> {
  const dir = path.join(ROOT, id);
  const stages: Stage[] = [];
  let latest = 0;
  let failed: Stage | null = null;
  let status: PaperStatus | null = null;
  let error = "";
  try {
    status = await paperStatus(id, dir);
  } catch (e) {
    error = `tongtu status failed: ${e instanceof Error ? e.message : String(e)}`;
  }
  for (const state of status?.stages ?? []) {
    if (state.status === ABSENT || !state.manifest) break;
    latest = Math.max(latest, mtime(state.manifest));
    stages.push({ name: state.name, status: state.status, message: state.message, warnings: state.warnings });
    if (state.status !== "ok") {
      failed = stages[stages.length - 1];
      break;
    }
  }
  const done = stages.length - (failed ? 1 : 0);
  const stage = failed?.name ?? STAGES[done] ?? "compile";
  const pid = status?.running ? status.pid : null;
  return {
    ...readMeta(status?.meta),
    id,
    dir,
    pdf: status?.pdf ?? "",
    pid,
    status: pid ? "running" : failed ? "failed" : done === STAGES.length ? "ok" : "partial",
    stage,
    done,
    message: failed ? `${failed.status} ${failed.message}`.trim() : error,
    stages,
    events: sessionEvents(dir, stage),
    started: pid && status?.started_at ? Date.parse(status.started_at) : null,
    added: mtime(path.join(dir, "build/manifests/fetch.json")) || mtime(dir),
    mtime: latest || mtime(dir),
  };
}

export async function scan(): Promise<Paper[]> {
  let ids: string[] = [];
  try {
    ids = fs.readdirSync(ROOT, { withFileTypes: true }).filter((e) => e.isDirectory() && !unversioned(e.name)).map((e) => e.name);
  } catch {}
  const papers = await Promise.all(ids.map((id) => inspect(id)));
  return papers.sort((a, b) => b.mtime - a.mtime);
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
