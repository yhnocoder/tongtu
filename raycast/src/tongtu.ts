import { environment, getPreferenceValues } from "@raycast/api";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const STAGES = ["fetch", "precompile", "mask", "survey", "translate", "review", "compile"] as const;
export const ROOT = process.env.TONGTU_HOME || path.join(os.homedir(), ".local/share/tongtu");
const LOGS = path.join(os.homedir(), "Library/Logs/tongtu");
const ARXIV_ID = /^\d{4}\.\d{4,5}(v\d+)?$/;
const ENV = {
  HOME: os.homedir(),
  USER: os.userInfo().username,
  LOGNAME: os.userInfo().username,
  PATH: `${os.homedir()}/.local/bin:/opt/homebrew/bin:/Library/TeX/texbin:/usr/bin:/bin`,
  LANG: "en_US.UTF-8",
  TMPDIR: os.tmpdir(),
  ...(process.env.TONGTU_HOME ? { TONGTU_HOME: process.env.TONGTU_HOME } : {}),
};

export type Stage = { name: string; status: string; message: string };

export type Paper = {
  id: string;
  title: string;
  dir: string;
  pdf: string;
  log: string;
  pid: number | null;
  status: "running" | "ok" | "failed" | "partial";
  stage: string;
  done: number;
  message: string;
  stages: Stage[];
  translated: [number, number] | null;
  mtime: number;
};

const q = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`;
export const logPath = (id: string) => path.join(LOGS, `${id}.log`);
const pidPath = (id: string) => path.join(LOGS, `${id}.pid`);
const titlePath = (id: string) => path.join(environment.supportPath, `${id}.title`);
export const normalize = (paper: string) => paper.trim().replace(/\/$/, "").split("/abs/").pop()!.replace("/", "_");

export function start(paper: string) {
  const id = normalize(paper);
  const repo = getPreferenceValues<{ repo: string }>().repo.replace(/^~/, os.homedir());
  const dir = path.join(ROOT, id);
  fs.mkdirSync(LOGS, { recursive: true });
  const script = [
    `uv run --directory ${q(repo)} tongtu run ${q(paper)} > ${q(logPath(id))} 2>&1`,
    `code=$?; rm -f ${q(pidPath(id))}`,
    `if [ $code = 0 ]; then terminal-notifier -title Tongtu -message ${q(`${id} translated`)} -open ${q(`file://${dir}/out`)}`,
    `else terminal-notifier -title Tongtu -message ${q(`${id} failed`)} -open ${q(`file://${dir}`)}; fi`,
  ].join("; ");
  const child = spawn("/bin/zsh", ["-c", script], { detached: true, stdio: "ignore", env: ENV });
  fs.writeFileSync(pidPath(id), String(child.pid));
  child.unref();
}

export function stop(paper: Paper) {
  if (paper.pid) process.kill(-paper.pid, "SIGTERM");
}

function alive(id: string): number | null {
  try {
    const pid = Number(fs.readFileSync(pidPath(id), "utf8"));
    process.kill(pid, 0);
    return pid;
  } catch {
    return null;
  }
}

function read(file: string): string {
  try {
    return fs.readFileSync(file, "utf8");
  } catch {
    return "";
  }
}

function inspect(id: string): Paper {
  const dir = path.join(ROOT, id);
  const pid = alive(id);
  const stages: Stage[] = [];
  let mtime = 0;
  let failed: Stage | null = null;
  for (const name of STAGES) {
    const file = path.join(dir, "build/manifests", `${name}.json`);
    const text = read(file);
    if (!text) break;
    const manifest = JSON.parse(text);
    mtime = Math.max(mtime, fs.statSync(file).mtimeMs);
    stages.push({ name, status: manifest.status, message: manifest.message ?? "" });
    if (manifest.status !== "ok") {
      failed = stages[stages.length - 1];
      break;
    }
  }
  const done = stages.length - (failed ? 1 : 0);
  const stage = failed?.name ?? STAGES[done] ?? "compile";
  const chunks = stage === "translate" ? tail(logPath(id), 200).match(/chunks (\d+)\/(\d+)(?![\s\S]*chunks \d+\/\d+)/) : null;
  const status = pid ? "running" : failed ? "failed" : done === STAGES.length ? "ok" : "partial";
  try {
    mtime = Math.max(mtime, fs.statSync(logPath(id)).mtimeMs);
  } catch {}
  return {
    id,
    title: read(titlePath(id)),
    dir,
    pdf: path.join(dir, "out/zh.pdf"),
    log: logPath(id),
    pid,
    status,
    stage,
    done,
    message: failed ? `${failed.status} ${failed.message}`.trim() : "",
    stages,
    translated: chunks ? [Number(chunks[1]), Number(chunks[2])] : null,
    mtime,
  };
}

export function scan(): Paper[] {
  let ids: string[] = [];
  try {
    ids = fs.readdirSync(ROOT, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name);
  } catch {}
  return ids.map(inspect).sort((a, b) => Number(b.status === "running") - Number(a.status === "running") || b.mtime - a.mtime);
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

export function tail(file: string, lines = 60): string {
  const all = read(file).trimEnd().split("\n");
  return all.filter((line, i) => line !== all[i + 1]).slice(-lines).join("\n");
}

export function thumbnail(paper: Paper): string | null {
  const png = path.join(environment.supportPath, `${paper.id}.png`);
  try {
    const pdf = fs.statSync(paper.pdf);
    if (!fs.existsSync(png) || fs.statSync(png).mtimeMs < pdf.mtimeMs) {
      fs.mkdirSync(environment.supportPath, { recursive: true });
      execFileSync("sips", ["-s", "format", "png", "-Z", "1200", paper.pdf, "--out", png], { stdio: "ignore" });
    }
    return png;
  } catch {
    return null;
  }
}
