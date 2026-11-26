import { LocalStorage, environment } from "@raycast/api";
import { execFile, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  Config,
  DEFAULT_MODE_KEY,
  DEFAULT_RECENT_DAYS,
  launchScript,
  logFileName,
  parseOpened,
  parseRecentDays,
  shellQuote,
  toolDirectories,
  toolPath,
  withOpened,
} from "./core";

const HOME = os.homedir();
const THUMBNAILS = path.join(HOME, ".cache/tongtu-raycast");
const THUMBNAIL_WIDTH = 1200;
const THUMBNAIL_MARGIN = 0.06;
const THUMBNAIL_HEIGHT_PER_WIDTH = 2 / 3;
const OUTPUT_LIMIT = 64 * 1024 * 1024;
const ENV = {
  HOME,
  USER: os.userInfo().username,
  LOGNAME: os.userInfo().username,
  PATH: toolPath(HOME),
  LANG: "en_US.UTF-8",
  TMPDIR: os.tmpdir(),
  ...(process.env.TONGTU_HOME ? { TONGTU_HOME: process.env.TONGTU_HOME } : {}),
};

export const TONGTU_HOME = process.env.TONGTU_HOME;
export const USER_HOME = HOME;

export type Result = { code: number; stdout: string; stderr: string };

function executable(): string | null {
  return toolDirectories(HOME).map((directory) => path.join(directory, "tongtu")).find((file) => fs.existsSync(file)) ?? null;
}

export function installed(): boolean {
  return executable() !== null;
}

function execute(file: string, args: string[], timeout?: number): Promise<Result> {
  return new Promise((resolve) => {
    execFile(file, args, { env: ENV, encoding: "utf8", maxBuffer: OUTPUT_LIMIT, timeout }, (error, stdout, stderr) => {
      const code = error ? (typeof error.code === "number" ? error.code : 1) : 0;
      resolve({ code, stdout: String(stdout ?? ""), stderr: String(stderr ?? "") });
    });
  });
}

export function tongtu(args: string[]): Promise<Result> {
  const file = executable();
  if (!file) return Promise.resolve({ code: 127, stdout: "", stderr: "" });
  return execute(file, args);
}

export function osascript(script: string, timeout: number): Promise<Result> {
  return execute("/usr/bin/osascript", ["-e", script], timeout);
}

export function parseJson<T>(text: string): T | null {
  try {
    return JSON.parse(text) as T;
  } catch {
    return null;
  }
}

export function readJson<T>(file: string | null): T | null {
  if (!file) return null;
  try {
    return JSON.parse(fs.readFileSync(file, "utf8")) as T;
  } catch {
    return null;
  }
}

export async function readConfig(): Promise<Config | null> {
  const result = await tongtu(["config", "list", "--json"]);
  return result.code === 0 ? parseJson<Config>(result.stdout) : null;
}

export async function startRun(paper: string, runArguments: string[], mode: string | null): Promise<string> {
  const runs = path.join(environment.supportPath, "runs");
  fs.mkdirSync(runs, { recursive: true });
  const script = launchScript(paper, runArguments, path.join(runs, logFileName(paper)), path.join(environment.assetsPath, "notify.js"));
  spawn("/bin/zsh", ["-c", script], { detached: true, stdio: "ignore", env: ENV }).unref();
  if (mode) return mode;
  const config = await readConfig();
  return String(config?.[DEFAULT_MODE_KEY] ?? "");
}

export function preview(file: string): void {
  spawn("/usr/bin/qlmanage", ["-p", file], { detached: true, stdio: "ignore", env: ENV }).unref();
}

export async function loadOpened(): Promise<string[] | null> {
  return parseOpened(await LocalStorage.getItem<string>("opened"));
}

export async function saveOpened(opened: string[]): Promise<void> {
  await LocalStorage.setItem("opened", JSON.stringify(opened));
}

export async function markOpened(name: string): Promise<void> {
  const opened = await loadOpened();
  if (opened) await saveOpened(withOpened(opened, name));
}

export async function loadRecentDays(): Promise<number> {
  return parseRecentDays(await LocalStorage.getItem<string>("recentDays")) ?? DEFAULT_RECENT_DAYS;
}

function modified(file: string): number {
  try {
    return fs.statSync(file).mtimeMs;
  } catch {
    return 0;
  }
}

function thumbnailFile(name: string): string {
  return path.join(THUMBNAILS, `${name}.png`);
}

const rendering = new Set<string>();

export function thumbnail(name: string, pdf: string | null): string | null {
  const pdfTime = pdf ? modified(pdf) : 0;
  if (!pdf || !pdfTime) return null;
  const png = thumbnailFile(name);
  if (modified(png) >= pdfTime) return png;
  if (rendering.has(png)) return null;
  rendering.add(png);
  const temporary = `${png}.tmp.png`;
  const margin = Math.round(THUMBNAIL_MARGIN * 1000);
  const script = [
    `mkdir -p ${shellQuote(THUMBNAILS)}`,
    `sips -s format png -Z ${THUMBNAIL_WIDTH} ${shellQuote(pdf)} --out ${shellQuote(temporary)}`,
    `read w h <<< "$(sips -g pixelWidth -g pixelHeight ${shellQuote(temporary)} | awk '/pixel/ {printf "%s ", $2}')"`,
    `cw=$((w * ${1000 - 2 * margin} / 1000)); ch=$((cw * ${Math.round(THUMBNAIL_HEIGHT_PER_WIDTH * 1000)} / 1000)); max=$((h - h * ${margin} / 1000)); ch=$((ch > max ? max : ch))`,
    `sips --cropOffset $((h * ${margin} / 1000)) $((w * ${margin} / 1000)) --cropToHeightWidth $ch $cw ${shellQuote(temporary)}`,
    `mv ${shellQuote(temporary)} ${shellQuote(png)}`,
  ].join(" && ");
  execFile("/bin/zsh", ["-c", script], { env: ENV }, () => rendering.delete(png));
  return null;
}

export function removePaperFiles(name: string, workdir: string): void {
  fs.rmSync(workdir, { recursive: true, force: true });
  fs.rmSync(thumbnailFile(name), { force: true });
}
