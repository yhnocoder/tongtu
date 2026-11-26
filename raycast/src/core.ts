export const STAGES = ["fetch", "precompile", "mask", "translate", "compile"];
export const NOT_INSTALLED = "没有找到 tongtu 命令，安装方法见 README";
export const DEFAULT_RECENT_DAYS = 7;

export type StageState = {
  name: string;
  status: string;
  outputs_present: boolean;
  message: string;
  warnings: string[];
  manifest: string | null;
};

export type PaperStatus = {
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

export type PaperMeta = {
  title: string;
  authors: string[];
  abstract: string;
  primary_category: string;
  published: string;
};

export type PaperState = "running" | "ok" | "failed" | "interrupted";

export type PaperEntry = {
  name: string;
  state: PaperState;
  stage: string | null;
  status: PaperStatus;
  info: PaperMeta | null;
  added_at: string;
  updated_at: string;
  pages: number | null;
  source_pages: number | null;
  translate_seconds: number | null;
  translate_cost_usd: number | null;
  recent_actions: string[];
};

export type Note = { where: string; problem: string; resolution: string };
export type NotesFile = { notes: Note[] };
export type GlossaryFile = { terms: { word: string; translation: string }[] };
export type ConfigValue = string | number | boolean | null;
export type Config = Record<string, ConfigValue>;
export type FormValues = Record<string, string | boolean>;
export type Row = { title: string; text: string; link?: string };
export type Section = { title: string; papers: PaperEntry[] };

const NEW_ARXIV_ID = /\d{4}\.\d{4,5}(v\d+)?/;
const OLD_ARXIV_ID = /[a-z-]+(\.[A-Z]{2})?\/\d{7}(v\d+)?/;
const URL_HOST = /^[a-z][a-z0-9+.-]*:\/\/(?:[^/?#@]*@)?([^/?#:]+)/i;
const CHROMIUM_BROWSERS = ["Google Chrome", "Microsoft Edge", "Brave Browser", "Arc", "Chromium", "Vivaldi"];
const TOOL_DIRECTORIES = ["~/.local/bin", "~/.cargo/bin", "/opt/homebrew/bin", "/Library/TeX/texbin", "/usr/bin", "/bin"];
const DAY_MILLISECONDS = 86_400_000;

export function arxivId(text: string): string | null {
  return (NEW_ARXIV_ID.exec(text) ?? OLD_ARXIV_ID.exec(text))?.[0] ?? null;
}

export function arxivIdFromUrl(url: string): string | null {
  const host = URL_HOST.exec(url.trim())?.[1].toLowerCase();
  if (host !== "arxiv.org" && !host?.endsWith(".arxiv.org")) return null;
  return arxivId(url);
}

export function paperArgument(text: string): string {
  return arxivId(text) ?? text.trim();
}

export function browserUrlScript(application: string): string | null {
  if (application === "Safari") return 'tell application "Safari" to get URL of front document';
  if (application === "Ardia") return 'tell application "Ardia" to get URL of front window';
  if (CHROMIUM_BROWSERS.includes(application)) return `tell application "${application}" to get URL of active tab of front window`;
  return null;
}

export function runMode(argument: string | undefined): string | null {
  return argument === "fast" || argument === "smart" ? argument : null;
}

export function shellQuote(text: string): string {
  return `'${text.replace(/'/g, `'\\''`)}'`;
}

export function toolDirectories(home: string): string[] {
  return TOOL_DIRECTORIES.map((directory) => directory.replace(/^~/, home));
}

export function toolPath(home: string): string {
  return toolDirectories(home).join(":");
}

export function logFileName(paper: string): string {
  return `${paper.replace(/\//g, "_")}.log`;
}

export function launchScript(paper: string, runArguments: string[], logFile: string, notifyScript: string): string {
  const run = ["tongtu", "run", shellQuote(paper), ...runArguments.map(shellQuote)].join(" ");
  return [
    `${run} > ${shellQuote(logFile)} 2>&1`,
    `osascript -l JavaScript ${shellQuote(notifyScript)} ${shellQuote(paper)} $? ${shellQuote(logFile)}`,
  ].join("\n");
}

export type RunAction = "retry" | "retrySmart" | "redoFast" | "redoSmart" | "recompile";

export const RUN_TITLES: Record<RunAction, string> = {
  retry: "重试",
  retrySmart: "用 smart 模式重试",
  redoFast: "用 fast 重新翻译",
  redoSmart: "用 smart 重新翻译",
  recompile: "只重新编译",
};

const RUN_OPTIONS: Record<RunAction, { options: string[]; mode: string | null; redo: boolean }> = {
  retry: { options: [], mode: null, redo: false },
  retrySmart: { options: ["--mode", "smart"], mode: "smart", redo: false },
  redoFast: { options: ["--from", "translate", "--mode", "fast"], mode: "fast", redo: true },
  redoSmart: { options: ["--from", "translate", "--mode", "smart"], mode: "smart", redo: true },
  recompile: { options: ["--from", "compile"], mode: null, redo: true },
};

export function runOptions(action: RunAction, workdir: string): { options: string[]; mode: string | null; redo: boolean } {
  const { options, mode, redo } = RUN_OPTIONS[action];
  return { options: ["--workdir", workdir, ...options], mode, redo };
}

export function workdirName(workdir: string): string {
  return workdir.replace(/\/+$/, "").split("/").pop() ?? workdir;
}

export function isComplete(status: PaperStatus): boolean {
  return status.pdf !== null && STAGES.every((name) => status.stages.find((stage) => stage.name === name)?.status === "ok");
}

export function recentTitle(days: number): string {
  return `最近 ${days} 天`;
}

export function sections(papers: PaperEntry[], recentDays: number, now: number): Section[] {
  const cutoff = now - recentDays * DAY_MILLISECONDS;
  const recent = (paper: PaperEntry) => Date.parse(paper.updated_at) >= cutoff;
  return [
    { title: "进行中", papers: papers.filter((paper) => paper.state === "running") },
    { title: "需要处理", papers: papers.filter((paper) => paper.state === "failed" || paper.state === "interrupted") },
    { title: recentTitle(recentDays), papers: papers.filter((paper) => paper.state === "ok" && recent(paper)) },
    { title: "更早", papers: papers.filter((paper) => paper.state === "ok" && !recent(paper)) },
  ].filter((section) => section.papers.length > 0);
}

export const FILTER_ALL = "all";
export const FILTER_UNREAD = "unread";

export function categories(papers: PaperEntry[]): string[] {
  const found = papers.map((paper) => paper.info?.primary_category ?? "").filter((category) => category !== "");
  return [...new Set(found)].sort();
}

export function isUnread(paper: PaperEntry, opened: string[] | null): boolean {
  return paper.state === "ok" && opened !== null && !opened.includes(paper.name);
}

export function matches(paper: PaperEntry, search: string, filter: string, opened: string[] | null): boolean {
  if (filter === FILTER_UNREAD && !isUnread(paper, opened)) return false;
  if (filter !== FILTER_ALL && filter !== FILTER_UNREAD && paper.info?.primary_category !== filter) return false;
  const words = search.toLowerCase().split(/\s+/).filter((word) => word !== "");
  const text = [paper.info?.title ?? "", ...(paper.info?.authors ?? []), paper.name].join("\n").toLowerCase();
  return words.every((word) => text.includes(word));
}

export function parseOpened(stored: string | undefined): string[] | null {
  if (stored === undefined) return null;
  try {
    const names: unknown = JSON.parse(stored);
    return Array.isArray(names) ? names.filter((name): name is string => typeof name === "string") : null;
  } catch {
    return null;
  }
}

export function initialOpened(papers: PaperEntry[]): string[] {
  return papers.filter((paper) => paper.state === "ok").map((paper) => paper.name);
}

export function withOpened(opened: string[], name: string): string[] {
  return opened.includes(name) ? opened : [...opened, name];
}

export function withoutOpened(opened: string[], name: string): string[] {
  return opened.filter((other) => other !== name);
}

export function parseRecentDays(text: string | undefined): number | null {
  const trimmed = (text ?? "").trim();
  if (!/^\d+$/.test(trimmed) || Number(trimmed) < 1) return null;
  return Number(trimmed);
}

const pad = (value: number) => String(value).padStart(2, "0");

export function clock(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${pad(Math.floor(whole / 60))}:${pad(whole % 60)}`;
}

export function elapsed(startedAt: string | null, now: number): string {
  return startedAt ? clock((now - Date.parse(startedAt)) / 1000) : "00:00";
}

export function dateText(iso: string): string {
  const date = new Date(iso);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function dateTimeText(iso: string): string {
  const date = new Date(iso);
  return `${dateText(iso)} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function paperTitle(paper: PaperEntry): string {
  return paper.info?.title || paper.name;
}

export function stateLabel(paper: PaperEntry, now: number): string {
  if (paper.state === "running") return `${paper.stage} ${elapsed(paper.status.started_at, now)}`;
  if (paper.state === "failed") return `${paper.stage} 失败`;
  if (paper.state === "interrupted") return `${paper.stage} 中断`;
  return [paper.info?.primary_category ?? "", dateText(paper.added_at)].filter((part) => part !== "").join(" · ");
}

export function arxivVersion(status: PaperStatus): string | null {
  if (!status.arxiv_id) return null;
  return status.version === null ? status.arxiv_id : `${status.arxiv_id}v${status.version}`;
}

export function arxivUrl(status: PaperStatus): string | null {
  const id = arxivVersion(status);
  return id ? `https://arxiv.org/abs/${id}` : null;
}

function oneLine(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function stageLine(paper: PaperEntry): string {
  return STAGES.map((name) => {
    const stage = paper.status.stages.find((state) => state.name === name);
    if (name === paper.stage) {
      if (paper.state === "failed") return `**${name} ✗**`;
      if (paper.state === "running") return `**${name}**`;
      return name;
    }
    return stage?.status === "ok" ? `${name} ✓` : name;
  }).join(" → ");
}

export function completedMarkdown(
  paper: PaperEntry,
  thumbnail: string | null,
  notes: NotesFile | null,
  glossary: GlossaryFile | null,
): string {
  const parts = [`# ${paperTitle(paper)}`];
  if (thumbnail) parts.push(`![](file://${encodeURI(thumbnail)})`);
  if (paper.info?.abstract) parts.push(paper.info.abstract);
  if (notes) {
    parts.push(`## 译者备注 · ${notes.notes.length} 条`);
    for (const note of notes.notes) {
      parts.push(`**${oneLine(note.where)}**\n\n问题：${oneLine(note.problem)}\n\n处理：${oneLine(note.resolution)}`);
    }
  }
  if (glossary) {
    const cell = (text: string) => oneLine(text).replace(/\|/g, "\\|");
    const rows = glossary.terms.map((term) => `| ${cell(term.word)} | ${cell(term.translation)} |`);
    parts.push(`## 本篇术语 · ${glossary.terms.length} 条`);
    if (rows.length > 0) parts.push(["| 原词 | 译法 |", "|---|---|", ...rows].join("\n"));
  }
  return parts.join("\n\n");
}

export function runningMarkdown(paper: PaperEntry, now: number): string {
  const parts = [`# ${paperTitle(paper)}`, stageLine(paper), `\`${paper.stage}\` 已运行 ${elapsed(paper.status.started_at, now)}`];
  if (paper.recent_actions.length > 0) {
    parts.push("## 会话最近的动作", paper.recent_actions.map((action) => `- ${oneLine(action)}`).join("\n"));
  }
  return parts.join("\n\n");
}

export const UNFINISHED_NEXT_STEPS = [RUN_TITLES.retry, RUN_TITLES.retrySmart, "查看日志"];

export function unfinishedMarkdown(paper: PaperEntry): string {
  const failed = paper.status.stages.find((stage) => stage.name === paper.stage);
  const reason =
    paper.state === "failed"
      ? `\`${paper.stage}\` 失败：${oneLine(`${failed?.status ?? ""} ${failed?.message ?? ""}`)}`
      : `在 \`${paper.stage}\` 阶段中断`;
  return [
    `# ${paperTitle(paper)}`,
    stageLine(paper),
    "## 原因",
    reason,
    "## 下一步",
    UNFINISHED_NEXT_STEPS.map((step) => `- ${step}`).join("\n"),
  ].join("\n\n");
}

function arxivRow(status: PaperStatus): Row[] {
  const id = arxivVersion(status);
  const link = arxivUrl(status);
  return id && link ? [{ title: "arXiv", text: id, link }] : [];
}

function modeRow(status: PaperStatus): Row[] {
  return status.mode ? [{ title: "模式", text: status.mode }] : [];
}

function durationText(seconds: number): string {
  const whole = Math.round(seconds);
  return whole < 60 ? `${whole} 秒` : `${Math.floor(whole / 60)} 分 ${whole % 60} 秒`;
}

export function completedRows(paper: PaperEntry): Row[] {
  const rows = arxivRow(paper.status);
  if (paper.info?.authors.length) rows.push({ title: "作者", text: paper.info.authors.join(", ") });
  if (paper.info) rows.push({ title: "分类与发表日期", text: `${paper.info.primary_category} · ${dateText(paper.info.published)}` });
  rows.push({ title: "加入日期", text: dateText(paper.added_at) });
  const pages = [
    paper.pages === null ? "" : `中文 ${paper.pages} 页`,
    paper.source_pages === null ? "" : `原文 ${paper.source_pages} 页`,
  ].filter((part) => part !== "");
  if (pages.length > 0) rows.push({ title: "页数", text: pages.join("，") });
  rows.push(...modeRow(paper.status));
  const effort = [
    paper.translate_seconds === null ? "" : durationText(paper.translate_seconds),
    paper.translate_cost_usd === null ? "" : `$${paper.translate_cost_usd.toFixed(2)}`,
  ].filter((part) => part !== "");
  if (effort.length > 0) rows.push({ title: "翻译耗时与费用", text: effort.join(" · ") });
  const warnings = paper.status.stages.reduce((count, stage) => count + stage.warnings.length, 0);
  rows.push({ title: "提醒", text: warnings === 0 ? "无" : `${warnings} 条` });
  return rows;
}

export function runningRows(paper: PaperEntry, now: number): Row[] {
  const rows = [...arxivRow(paper.status), ...modeRow(paper.status)];
  if (paper.status.started_at) {
    rows.push({ title: "开始时间", text: dateTimeText(paper.status.started_at) });
    rows.push({ title: "总用时", text: elapsed(paper.status.started_at, now) });
  }
  return rows;
}

export function unfinishedRows(paper: PaperEntry): Row[] {
  const rows = [...arxivRow(paper.status), ...modeRow(paper.status)];
  if (paper.stage) {
    rows.push({ title: paper.state === "failed" ? "失败的阶段" : "中断的阶段", text: paper.stage });
    rows.push({ title: "已完成的阶段数", text: `${Math.max(0, STAGES.indexOf(paper.stage))} / ${STAGES.length}` });
  }
  return rows;
}

export const MODES = ["fast", "smart"];
export const MODEL_PRESETS = ["claude-code/claude-sonnet-5-5", "claude-code/claude-opus-5-5"];
export const OTHER_MODEL = "other";
export const EFFORTS = ["low", "medium", "high"];
export const DEFAULT_MODE_KEY = "modes.default";
export const CROP_KEY = "pdf.crop_margins";
export const FONT_FIELDS: [string, string][] = [
  ["fonts.main.regular", "正文"],
  ["fonts.main.bold", "正文粗体"],
  ["fonts.sans.regular", "无衬线"],
  ["fonts.sans.bold", "无衬线粗体"],
  ["fonts.mono", "等宽"],
];

export const modelKey = (mode: string) => `modes.${mode}.model`;
export const customModelKey = (mode: string) => `modes.${mode}.model.custom`;
export const effortKey = (mode: string) => `modes.${mode}.effort`;
export const timeoutKey = (mode: string) => `modes.${mode}.timeout_seconds`;

const text = (value: ConfigValue | undefined) => (value === null || value === undefined ? "" : String(value));

export function minutesFromSeconds(seconds: number): string {
  return String(Math.round((seconds / 60) * 1000) / 1000);
}

export function secondsFromMinutes(minutes: string): number | null {
  const trimmed = minutes.trim();
  if (!/^\d+(\.\d+)?$/.test(trimmed) || Number(trimmed) <= 0) return null;
  return Math.round(Number(trimmed) * 60 * 1000) / 1000;
}

export function modelChoices(current: string): string[] {
  return current === "" || MODEL_PRESETS.includes(current) ? MODEL_PRESETS : [...MODEL_PRESETS, current];
}

export function effortChoices(current: string): string[] {
  return EFFORTS.includes(current) ? EFFORTS : [...EFFORTS, current];
}

export function formValues(config: Config): FormValues {
  const values: FormValues = { [DEFAULT_MODE_KEY]: text(config[DEFAULT_MODE_KEY]), [CROP_KEY]: config[CROP_KEY] === true };
  for (const mode of MODES) {
    values[modelKey(mode)] = text(config[modelKey(mode)]);
    values[customModelKey(mode)] = "";
    values[effortKey(mode)] = text(config[effortKey(mode)]);
    values[timeoutKey(mode)] = minutesFromSeconds(Number(config[timeoutKey(mode)]));
  }
  for (const [key] of FONT_FIELDS) values[key] = text(config[key]);
  return values;
}

export function configChanges(config: Config, form: FormValues): { pairs: string[]; errors: Record<string, string> } {
  const pairs: string[] = [];
  const errors: Record<string, string> = {};
  const set = (key: string, value: string) => {
    if (value !== text(config[key])) pairs.push(key, value);
  };
  set(DEFAULT_MODE_KEY, String(form[DEFAULT_MODE_KEY]));
  for (const mode of MODES) {
    const chosen = String(form[modelKey(mode)]);
    set(modelKey(mode), chosen === OTHER_MODEL ? String(form[customModelKey(mode)]).trim() : chosen);
    set(effortKey(mode), String(form[effortKey(mode)]));
    const seconds = secondsFromMinutes(String(form[timeoutKey(mode)]));
    if (seconds === null) errors[timeoutKey(mode)] = "要求是正数";
    else if (seconds !== Number(config[timeoutKey(mode)])) pairs.push(timeoutKey(mode), String(seconds));
  }
  for (const [key] of FONT_FIELDS) set(key, String(form[key]).trim());
  if (form[CROP_KEY] !== (config[CROP_KEY] === true)) pairs.push(CROP_KEY, form[CROP_KEY] ? "true" : "false");
  return { pairs, errors };
}

export function configErrors(stderr: string, keys: string[]): { fields: Record<string, string>; first: string } {
  const lines = stderr
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
  const fields: Record<string, string> = {};
  for (const line of lines) {
    const separator = line.indexOf(": ");
    const key = separator < 0 ? "" : line.slice(0, separator);
    if (keys.includes(key) && !(key in fields)) fields[key] = line.slice(separator + 2);
  }
  return { fields, first: lines[0] ?? "" };
}

export function configFile(home: string, tongtuHome: string | undefined): string {
  return `${tongtuHome || `${home}/.tongtu`}/config.toml`;
}
