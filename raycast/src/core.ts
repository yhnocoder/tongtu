const STAGES = ["fetch", "precompile", "mask", "translate", "compile"];
export const NOT_INSTALLED = "没有找到 tongtu 命令，安装方法见 README";
export const DEFAULT_RECENT_DAYS = 7;
const ACTIVE_REFRESH_SECONDS = 5;
const IDLE_REFRESH_SECONDS = 60;
export const DETAIL_REFRESH_SECONDS = 2;
export const START_REFRESH_DELAY_SECONDS = 2;
export const START_WAIT_SECONDS = 20;
export const INPUT_DEBOUNCE_SECONDS = 0.3;

type StageState = {
  name: string;
  status: string;
  outputs_present: boolean;
  message: string;
  manifest: string | null;
};

export type PaperState = "running" | "ok" | "failed" | "interrupted";

export type PaperStatus = {
  workdir: string;
  arxiv_id: string | null;
  version: number | null;
  running: boolean;
  pid: number | null;
  started_at: string | null;
  mode: string | null;
  state: PaperState;
  stage: string | null;
  stages: StageState[];
  pdf: string | null;
  meta: string | null;
  pages: number | null;
  source_pages: number | null;
  translate_seconds: number | null;
  translate_cost_usd: number | null;
  recent_actions: string[];
};

export type PaperMeta = {
  arxiv_id: string;
  version: number;
  title: string;
  authors: string[];
  abstract: string;
  primary_category: string;
  published: string;
  abstract_zh?: string | null;
};

export type PaperSummary = {
  name: string;
  workdir: string;
  state: PaperState;
  stage: string | null;
  arxiv_id: string | null;
  version: number | null;
  title: string | null;
  primary_category: string | null;
  added_at: string;
  updated_at: string;
  started_at: string | null;
  mode: string | null;
  pdf: string | null;
};

export type PaperTarget = Pick<PaperSummary, "name" | "workdir" | "state" | "stage" | "arxiv_id" | "version" | "mode" | "pdf">;
export type PaperDetail = { name: string; addedAt: string | null; status: PaperStatus; meta: PaperMeta | null };
export type PaperSource = "argument" | "browser" | "clipboard";
type ConfigValue = string | number | boolean | null;
export type Config = Record<string, ConfigValue>;
export type FormValues = Record<string, string | boolean>;
export type Row = { title: string; text: string; link?: string };
export type Section = { title: string; papers: PaperSummary[] };

const NEW_ARXIV_ID = /(?<![\w.])\d{4}\.\d{4,5}(v\d+)?(?!\d|\.\d)/;
export const OLD_ARCHIVES = [
  "acc-phys",
  "adap-org",
  "alg-geom",
  "ao-sci",
  "astro-ph",
  "atom-ph",
  "bayes-an",
  "chao-dyn",
  "chem-ph",
  "cmp-lg",
  "comp-gas",
  "cond-mat",
  "cs",
  "dg-ga",
  "funct-an",
  "gr-qc",
  "hep-ex",
  "hep-lat",
  "hep-ph",
  "hep-th",
  "math",
  "math-ph",
  "mtrl-th",
  "nlin",
  "nucl-ex",
  "nucl-th",
  "patt-sol",
  "physics",
  "plasm-ph",
  "q-alg",
  "q-bio",
  "quant-ph",
  "solv-int",
  "supr-con",
];
const OLD_ARXIV_ID = new RegExp(`(?<![A-Za-z-])(?:${OLD_ARCHIVES.join("|")})(\\.[A-Z]{2})?/\\d{7}(v\\d+)?(?!\\d)`);
const PATH_ARGUMENT = /^[/~.]/;
const URL_HOST = /^[a-z][a-z0-9+.-]*:\/\/(?:[^/?#@]*@)?([^/?#:]+)/i;
const CHROMIUM_BROWSERS = ["Google Chrome", "Microsoft Edge", "Brave Browser", "Arc", "Chromium", "Vivaldi"];
const TOOL_DIRECTORIES = ["~/.local/bin", "~/.cargo/bin", "/opt/homebrew/bin", "/Library/TeX/texbin", "/usr/bin", "/bin"];
const DAY_MILLISECONDS = 86_400_000;
export const SHOWN_AUTHORS = 12;

function nonEmptyLines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

export function authorsText(authors: string[]): string {
  const shown = authors.slice(0, SHOWN_AUTHORS).join(", ");
  return authors.length > SHOWN_AUTHORS ? `${shown}, ...` : shown;
}

export function arxivId(text: string): string | null {
  return (NEW_ARXIV_ID.exec(text) ?? OLD_ARXIV_ID.exec(text))?.[0] ?? null;
}

export function arxivIdFromUrl(url: string): string | null {
  const host = URL_HOST.exec(url.trim())?.[1].toLowerCase();
  if (host !== "arxiv.org" && !host?.endsWith(".arxiv.org")) return null;
  return arxivId(url);
}

export function typedPaper(text: string): string | null {
  const trimmed = text.trim();
  return PATH_ARGUMENT.test(trimmed) ? trimmed : arxivId(trimmed);
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

export function launchScript(
  paper: string,
  runArguments: string[],
  logFile: string,
  notifyScript: string,
  workdir: string | null,
): string {
  const run = ["tongtu", "run", shellQuote(paper), ...runArguments.map(shellQuote)].join(" ");
  const notify = `osascript -l JavaScript ${shellQuote(notifyScript)} ${shellQuote(paper)} $? ${shellQuote(logFile)}`;
  return [`${run} > ${shellQuote(logFile)} 2>&1`, workdir === null ? notify : `${notify} ${shellQuote(workdir)}`].join("\n");
}

type RetryAction = "retry" | "retryFast" | "retrySmart";
export type RunAction = RetryAction | "redoFast" | "redoSmart" | "recompile";

const RUN_TITLES: Record<RunAction, string> = {
  retry: "重试",
  retryFast: "用 fast 重试",
  retrySmart: "用 smart 重试",
  redoFast: "用 fast 重新翻译",
  redoSmart: "用 smart 重新翻译",
  recompile: "只重新编译",
};

const RUN_OPTIONS: Record<RunAction, { options: string[]; redo: boolean }> = {
  retry: { options: [], redo: false },
  retryFast: { options: ["--mode", "fast"], redo: false },
  retrySmart: { options: ["--mode", "smart"], redo: false },
  redoFast: { options: ["--from", "translate", "--mode", "fast"], redo: true },
  redoSmart: { options: ["--from", "translate", "--mode", "smart"], redo: true },
  recompile: { options: ["--from", "compile"], redo: true },
};

const RETRY_MODES: Record<RetryAction, string | null> = { retry: null, retryFast: "fast", retrySmart: "smart" };

export function retryActions(mode: string | null): RetryAction[] {
  const actions: RetryAction[] = ["retry", "retryFast", "retrySmart"];
  return actions.filter((action) => action === "retry" || RETRY_MODES[action] !== (mode || null));
}

export function runTitle(action: RunAction, mode: string | null): string {
  return action === "retry" && mode ? `重试（${mode}）` : RUN_TITLES[action];
}

export function runOptions(action: RunAction, workdir: string, mode: string | null): { options: string[]; redo: boolean } {
  const { options, redo } = RUN_OPTIONS[action];
  const retryMode = action === "retry" && mode ? ["--mode", mode] : [];
  return { options: ["--workdir", workdir, ...options, ...retryMode], redo };
}

export function runMessage(action: RunAction, name: string, mode: string | null): string {
  if (action === "recompile") return `重新编译 ${name}`;
  if (action === "redoFast" || action === "redoSmart") return `开始翻译 ${name}（${action === "redoFast" ? "fast" : "smart"}）`;
  const shown = action === "retry" ? mode || null : RETRY_MODES[action];
  return shown ? `继续翻译 ${name}（${shown}）` : `继续翻译 ${name}`;
}

export function workdirName(workdir: string): string {
  return workdir.replace(/\/+$/, "").split("/").pop() ?? workdir;
}

export function removableWorkdir(name: string, workdir: string): boolean {
  return name !== "" && workdir.startsWith("/") && workdirName(workdir) === name;
}

export function refreshSeconds(papers: PaperSummary[]): number {
  return papers.some((paper) => paper.state === "running") ? ACTIVE_REFRESH_SECONDS : IDLE_REFRESH_SECONDS;
}

export function infoFailure(paper: string, stderr: string): string {
  if (stderr.includes("has no entry for")) return `arXiv 上没有 ${paper}`;
  const lines = nonEmptyLines(stderr);
  return lines[lines.length - 1] ?? `tongtu info ${paper} 没有给出结果`;
}

function recentTitle(days: number): string {
  return `最近 ${days} 天`;
}

export function sections(papers: PaperSummary[], recentDays: number, now: number): Section[] {
  const cutoff = now - recentDays * DAY_MILLISECONDS;
  const recent = (paper: PaperSummary) => Date.parse(paper.updated_at) >= cutoff;
  return [
    { title: "进行中", papers: papers.filter((paper) => paper.state === "running") },
    { title: "需要处理", papers: papers.filter((paper) => paper.state === "failed" || paper.state === "interrupted") },
    { title: recentTitle(recentDays), papers: papers.filter((paper) => paper.state === "ok" && recent(paper)) },
    { title: "更早", papers: papers.filter((paper) => paper.state === "ok" && !recent(paper)) },
  ].filter((section) => section.papers.length > 0);
}

export const FILTER_ALL = "all";
export const FILTER_UNREAD = "unread";

export function categories(papers: PaperSummary[]): string[] {
  const found = papers.map((paper) => paper.primary_category ?? "").filter((category) => category !== "");
  return [...new Set(found)].sort();
}

export function isUnread(paper: PaperSummary, opened: string[] | null): boolean {
  return paper.state === "ok" && opened !== null && !opened.includes(paper.name);
}

export function matches(paper: PaperSummary, search: string, filter: string, opened: string[] | null): boolean {
  if (filter === FILTER_UNREAD && !isUnread(paper, opened)) return false;
  if (filter !== FILTER_ALL && filter !== FILTER_UNREAD && paper.primary_category !== filter) return false;
  const words = search.toLowerCase().split(/\s+/).filter((word) => word !== "");
  const text = `${paper.title ?? ""}\n${paper.name}`.toLowerCase();
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

export function initialOpened(papers: PaperSummary[]): string[] {
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

export function paperTitle(paper: PaperSummary): string {
  return paper.title || paper.name;
}

export function stateLabel(paper: PaperSummary, now: number): string {
  if (paper.state === "running") return [paper.stage ?? "", elapsed(paper.started_at, now)].filter((part) => part !== "").join(" ");
  if (paper.state === "failed") return [paper.stage ?? "", "失败"].filter((part) => part !== "").join(" ");
  if (paper.state === "interrupted") return [paper.stage ?? "", "中断"].filter((part) => part !== "").join(" ");
  return [paper.primary_category ?? "", dateText(paper.added_at)].filter((part) => part !== "").join(" · ");
}

type ArxivReference = { arxiv_id: string | null; version: number | null };

export function arxivVersion(paper: ArxivReference): string | null {
  if (!paper.arxiv_id) return null;
  return paper.version === null ? paper.arxiv_id : `${paper.arxiv_id}v${paper.version}`;
}

export function arxivUrl(paper: ArxivReference): string | null {
  const id = arxivVersion(paper);
  return id ? `https://arxiv.org/abs/${id}` : null;
}

function oneLine(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function detailTitle(detail: PaperDetail): string {
  return detail.meta?.title || detail.name;
}

function stageLine(status: PaperStatus): string {
  return STAGES.map((name) => {
    const stage = status.stages.find((state) => state.name === name);
    if (name === status.stage) {
      if (status.state === "failed") return `**${name} ✗**`;
      if (status.state === "running") return `**${name}**`;
      return name;
    }
    return stage?.status === "ok" ? `${name} ✓` : name;
  }).join(" → ");
}

export function shownAbstract(meta: PaperMeta | null): string {
  return meta?.abstract_zh || meta?.abstract || "";
}

export function needsAbstractZh(meta: PaperMeta | null): boolean {
  return meta !== null && !meta.abstract_zh;
}

export function withAbstractZh(meta: PaperMeta | null, translated: PaperMeta | null): PaperMeta | null {
  if (!meta || !translated?.abstract_zh) return meta;
  if (translated.arxiv_id !== meta.arxiv_id || translated.version !== meta.version) return meta;
  return { ...meta, abstract_zh: translated.abstract_zh };
}

export function completedMarkdown(detail: PaperDetail, thumbnailBase64: string | null): string {
  const parts = [`# ${detailTitle(detail)}`];
  if (thumbnailBase64) parts.push(`![](data:image/png;base64,${thumbnailBase64})`);
  const abstract = shownAbstract(detail.meta);
  if (abstract) parts.push(abstract);
  return parts.join("\n\n");
}

export function runningMarkdown(detail: PaperDetail): string {
  const parts = [`# ${detailTitle(detail)}`, stageLine(detail.status)];
  if (detail.status.recent_actions.length > 0) {
    parts.push("## 会话最近的动作", detail.status.recent_actions.map((action) => `- ${oneLine(action)}`).join("\n"));
  }
  return parts.join("\n\n");
}

function unfinishedNextSteps(mode: string | null): string[] {
  return [...retryActions(mode).map((action) => runTitle(action, mode)), "查看日志"];
}

export function unfinishedMarkdown(detail: PaperDetail): string {
  const { status } = detail;
  const failed = status.stages.find((stage) => stage.name === status.stage);
  const reason =
    status.state === "failed"
      ? `\`${status.stage}\` 失败：${oneLine(`${failed?.status ?? ""} ${failed?.message ?? ""}`)}`
      : `在 \`${status.stage}\` 阶段中断`;
  return [
    `# ${detailTitle(detail)}`,
    stageLine(status),
    "## 原因",
    reason,
    "## 下一步",
    unfinishedNextSteps(status.mode).map((step) => `- ${step}`).join("\n"),
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

function paperRows(detail: PaperDetail): Row[] {
  const { addedAt, status, meta } = detail;
  const rows = arxivRow(status);
  if (meta?.authors.length) rows.push({ title: "作者", text: authorsText(meta.authors) });
  if (meta) rows.push({ title: "分类与发表日期", text: `${meta.primary_category} · ${dateText(meta.published)}` });
  if (addedAt) rows.push({ title: "加入日期", text: dateText(addedAt) });
  return rows;
}

function sourcePagesRow(status: PaperStatus): Row[] {
  return status.source_pages === null ? [] : [{ title: "页数", text: `原文 ${status.source_pages} 页` }];
}

export function completedRows(detail: PaperDetail): Row[] {
  const { status } = detail;
  const rows = paperRows(detail);
  const pages = [
    status.pages === null ? "" : `中文 ${status.pages} 页`,
    status.source_pages === null ? "" : `原文 ${status.source_pages} 页`,
  ].filter((part) => part !== "");
  if (pages.length > 0) rows.push({ title: "页数", text: pages.join("，") });
  rows.push(...modeRow(status));
  const effort = [
    status.translate_seconds === null ? "" : durationText(status.translate_seconds),
    status.translate_cost_usd === null ? "" : `$${status.translate_cost_usd.toFixed(2)}`,
  ].filter((part) => part !== "");
  if (effort.length > 0) rows.push({ title: "翻译耗时与费用", text: effort.join(" · ") });
  return rows;
}

export function runningRows(detail: PaperDetail, now: number): Row[] {
  const { status } = detail;
  const rows = [...paperRows(detail), ...sourcePagesRow(status), ...modeRow(status)];
  if (status.started_at) {
    rows.push({ title: "开始时间", text: dateTimeText(status.started_at) });
    rows.push({ title: "总用时", text: elapsed(status.started_at, now) });
  }
  return rows;
}

export function unfinishedRows(detail: PaperDetail): Row[] {
  const { status } = detail;
  const rows = [...paperRows(detail), ...sourcePagesRow(status), ...modeRow(status)];
  if (status.stage) {
    rows.push({ title: status.state === "failed" ? "失败的阶段" : "中断的阶段", text: status.stage });
    rows.push({ title: "已完成的阶段数", text: `${Math.max(0, STAGES.indexOf(status.stage))} / ${STAGES.length}` });
  }
  return rows;
}

type LocalState = "none" | PaperState;
export type Lookup = {
  paper: string;
  status: PaperStatus | null;
  meta: PaperMeta | null;
  infoError: string;
  directoryMissing: boolean;
};
export type EntryAction = "start" | "startFast" | "startSmart" | "detail" | "openPdf" | "arxiv" | "stop" | RetryAction;

export const LOCAL_STATE_TEXT: Record<LocalState, string> = {
  none: "未翻译",
  ok: "已翻译",
  running: "正在翻译",
  failed: "翻译失败",
  interrupted: "翻译中断",
};

const SOURCE_TEXT: Record<PaperSource, string> = {
  argument: "来自参数",
  browser: "来自浏览器",
  clipboard: "来自剪贴板",
};

export const ENTRY_ACTION_TITLES: Record<Exclude<EntryAction, RetryAction>, string> = {
  start: "开始翻译",
  startFast: "用 fast 翻译",
  startSmart: "用 smart 翻译",
  detail: "查看详情",
  openPdf: "打开中文 PDF",
  arxiv: "打开 arXiv 页面",
  stop: "停止",
};

export function isPathArgument(paper: string): boolean {
  return PATH_ARGUMENT.test(paper);
}

export function localState(status: PaperStatus | null): LocalState {
  if (!status) return "none";
  if (status.state === "interrupted" && status.stage === STAGES[0]) return "none";
  return status.state;
}

export function expandedPath(paper: string, home: string): string {
  return paper.replace(/^~(?=\/|$)/, home);
}

export function shownLookup(lookup: Lookup | null, text: string): Lookup | null {
  return lookup && lookup.paper === typedPaper(text) ? lookup : null;
}

export function localInfoId(paper: string, status: PaperStatus | null, meta: PaperMeta | null): string | null {
  if (!status?.arxiv_id || status.version === null) return null;
  const local = `${status.arxiv_id}v${status.version}`;
  if (local === paper || (meta && arxivVersion(meta) === local)) return null;
  return local;
}

export function lookupFailure(lookup: Lookup): string | null {
  if (lookup.directoryMissing) return "没有这个目录";
  if (isPathArgument(lookup.paper) || lookup.meta || localState(lookup.status) !== "none") return null;
  return infoFailure(lookup.paper, lookup.infoError);
}

export function entryTitle(lookup: Lookup): string {
  if (lookup.meta?.title) return lookup.meta.title;
  if (isPathArgument(lookup.paper)) return workdirName(lookup.paper);
  return lookup.status ? workdirName(lookup.status.workdir) : lookup.paper;
}

function entryVersion(lookup: Lookup): string {
  if (lookup.meta) return `${lookup.meta.arxiv_id}v${lookup.meta.version}`;
  if (isPathArgument(lookup.paper)) return "";
  return (lookup.status && arxivVersion(lookup.status)) || lookup.paper;
}

export function entrySubtitle(lookup: Lookup, source: PaperSource | null): string {
  const parts = [entryVersion(lookup), lookup.meta?.primary_category ?? "", source ? SOURCE_TEXT[source] : ""];
  return parts.filter((part) => part !== "").join(" · ");
}

export function entryArxivUrl(lookup: Lookup): string | null {
  const version = entryVersion(lookup);
  return version === "" ? null : `https://arxiv.org/abs/${version}`;
}

export function entryRows(lookup: Lookup, mode: string): Row[] {
  const rows: Row[] = [];
  if (lookup.meta?.authors.length) rows.push({ title: "作者", text: authorsText(lookup.meta.authors) });
  if (lookup.meta) rows.push({ title: "发表日期", text: dateText(lookup.meta.published) });
  rows.push({ title: "状态", text: LOCAL_STATE_TEXT[localState(lookup.status)] });
  if (mode !== "") rows.push({ title: "模式", text: mode });
  return rows;
}

export function entryActions(lookup: Lookup): EntryAction[] {
  const state = localState(lookup.status);
  const arxiv: EntryAction[] = entryArxivUrl(lookup) ? ["arxiv"] : [];
  if (state === "none") return ["start", "startFast", "startSmart", ...arxiv];
  if (state === "ok") return ["detail", "openPdf", ...arxiv];
  if (state === "running") return ["detail", "stop"];
  return ["detail", ...retryActions(lookup.status?.mode ?? null)];
}

export function statusTarget(status: PaperStatus): PaperTarget {
  return {
    name: workdirName(status.workdir),
    workdir: status.workdir,
    state: status.state,
    stage: status.stage,
    arxiv_id: status.arxiv_id,
    version: status.version,
    pdf: status.pdf,
    mode: status.mode,
  };
}

export function startingMarkdown(paper: string): string {
  return `# ${paper}\n\n正在启动`;
}

export function startFailedMarkdown(paper: string): string {
  return `# ${paper}\n\n没有启动成功，原因见系统通知`;
}

export function startSettled(status: PaperStatus | null): boolean {
  return status !== null && (status.running || status.stages.some((stage) => stage.status !== "absent"));
}

export const MODES = ["fast", "smart"];
const MODEL_PRESETS = ["claude-code/claude-sonnet-5-5", "claude-code/claude-opus-5-5"];
export const OTHER_MODEL = "other";
const EFFORTS = ["low", "medium", "high"];
export const DEFAULT_MODE_KEY = "modes.default";
export const CROP_KEY = "pdf.crop_margins";
export const FONT_FIELDS: [string, string][] = [
  ["fonts.main.regular", "正文"],
  ["fonts.main.bold", "正文粗体"],
  ["fonts.sans.regular", "无衬线"],
  ["fonts.sans.bold", "无衬线粗体"],
  ["fonts.mono", "等宽"],
];

export const ABSTRACT_FIELDS: [string, string][] = [
  ["abstract.base_url", "接口地址"],
  ["abstract.model", "模型"],
];
export const API_KEY_KEY = "abstract.api_key";
export const CLEAR_API_KEY_PAIR = [API_KEY_KEY, ""];

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

export function apiKeySet(config: Config): boolean {
  return text(config[API_KEY_KEY]) !== "";
}

export function apiKeyPlaceholder(config: Config): string {
  return apiKeySet(config) ? "已设置，留空表示不修改" : "未设置";
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
  for (const [key] of ABSTRACT_FIELDS) values[key] = text(config[key]);
  values[API_KEY_KEY] = "";
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
  for (const [key] of ABSTRACT_FIELDS) set(key, String(form[key]).trim());
  const apiKey = String(form[API_KEY_KEY] ?? "").trim();
  if (apiKey !== "") pairs.push(API_KEY_KEY, apiKey);
  if (form[CROP_KEY] !== (config[CROP_KEY] === true)) pairs.push(CROP_KEY, form[CROP_KEY] ? "true" : "false");
  return { pairs, errors };
}

export function configErrors(stderr: string, keys: string[]): { fields: Record<string, string>; first: string } {
  const lines = nonEmptyLines(stderr);
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
