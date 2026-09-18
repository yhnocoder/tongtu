import { environment, getPreferenceValues } from "@raycast/api";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const STAGES = ["fetch", "precompile", "mask", "survey", "translate", "review", "compile"] as const;
export const ROOT = process.env.TONGTU_HOME || path.join(os.homedir(), ".local/share/tongtu");
const ARXIV_ID = /\d{4}\.\d{4,5}(v\d+)?/;
const ENV = {
  HOME: os.homedir(),
  USER: os.userInfo().username,
  LOGNAME: os.userInfo().username,
  PATH: `${os.homedir()}/.local/bin:/opt/homebrew/bin:/Library/TeX/texbin:/usr/bin:/bin`,
  LANG: "en_US.UTF-8",
  TMPDIR: os.tmpdir(),
  ...(process.env.TONGTU_HOME ? { TONGTU_HOME: process.env.TONGTU_HOME } : {}),
};

export type Stage = { name: string; status: string; message: string; warnings: string[] };

const SESSION_LOGS: Record<string, string> = { precompile: "precompile-fix.jsonl", review: "review.jsonl", compile: "compile-fix.jsonl" };
const CACHE = path.join(os.homedir(), ".cache/tongtu-raycast");

export type Paper = {
  id: string;
  title: string;
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
  mtime: number;
};

const q = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`;
const titlePath = (id: string) => path.join(environment.supportPath, `${id}.title`);
export const normalize = (paper: string) => paper.match(ARXIV_ID)?.[0] ?? paper.trim();

export function start(paper: string) {
  const id = normalize(paper);
  const repo = getPreferenceValues<{ repo: string }>().repo.replace(/^~/, os.homedir());
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
  let mtime = 0;
  let failed: Stage | null = null;
  for (const name of STAGES) {
    const file = path.join(dir, "build/manifests", `${name}.json`);
    const text = read(file);
    if (!text) break;
    const manifest = JSON.parse(text);
    mtime = Math.max(mtime, fs.statSync(file).mtimeMs);
    stages.push({ name, status: manifest.status, message: manifest.message ?? "", warnings: manifest.warnings ?? [] });
    if (manifest.status !== "ok") {
      failed = stages[stages.length - 1];
      break;
    }
  }
  const done = stages.length - (failed ? 1 : 0);
  const stage = failed?.name ?? STAGES[done] ?? "compile";
  return {
    id,
    title: read(titlePath(id)),
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
    mtime: mtime || fs.statSync(dir).mtimeMs,
  };
}

export function scan(): Paper[] {
  let ids: string[] = [];
  try {
    ids = fs.readdirSync(ROOT, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name);
  } catch {}
  const groups = runningGroups();
  return ids
    .map((id) => inspect(id, groups.get(id) ?? null))
    .sort((a, b) => Number(b.status === "running") - Number(a.status === "running") || b.mtime - a.mtime);
}

export async function fetchTitles(papers: Paper[]): Promise<boolean> {
  const missing = papers.filter((p) => !p.title && ARXIV_ID.test(p.id)).map((p) => p.id);
  if (!missing.length) return false;
  const xml = await (await fetch(`https://export.arxiv.org/api/query?id_list=${missing.join(",")}&max_results=${missing.length}`)).text();
  fs.mkdirSync(environment.supportPath, { recursive: true });
  for (const entry of xml.split("<entry>").slice(1)) {
    const id = entry.match(/<id>http:\/\/arxiv\.org\/abs\/([^<]+)<\/id>/)?.[1] ?? "";
    const title = entry.match(/<title>([\s\S]*?)<\/title>/)?.[1].replace(/\s+/g, " ").trim();
    const match = missing.find((m) => id === m || id.startsWith(m + "v"));
    if (match && title) fs.writeFileSync(titlePath(match), title);
  }
  return true;
}

export function thumbnail(paper: Paper): string | null {
  const png = path.join(CACHE, `${paper.id}.png`);
  try {
    const pdf = fs.statSync(paper.pdf);
    if (!fs.existsSync(png) || fs.statSync(png).mtimeMs < pdf.mtimeMs) {
      fs.mkdirSync(CACHE, { recursive: true });
      execFileSync("sips", ["-s", "format", "png", "-Z", "1200", paper.pdf, "--out", png], { stdio: "ignore" });
    }
    return png;
  } catch {
    return null;
  }
}
