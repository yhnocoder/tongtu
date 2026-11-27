import assert from "node:assert/strict";
import test from "node:test";
import {
  API_KEY_KEY,
  CLEAR_API_KEY_PAIR,
  FILTER_ALL,
  ENTRY_ACTION_TITLES,
  FILTER_UNREAD,
  LOCAL_STATE_TEXT,
  OLD_ARCHIVES,
  OTHER_MODEL,
  apiKeyPlaceholder,
  apiKeySet,
  SHOWN_AUTHORS,
  arxivId,
  arxivIdFromUrl,
  authorsText,
  arxivUrl,
  browserUrlScript,
  categories,
  clock,
  completedMarkdown,
  completedRows,
  configChanges,
  configErrors,
  configFile,
  dateText,
  dateTimeText,
  effortChoices,
  elapsed,
  entryActions,
  entryArxivUrl,
  entryRows,
  entrySubtitle,
  entryTitle,
  expandedPath,
  formValues,
  infoFailure,
  initialOpened,
  isPathArgument,
  isUnread,
  launchScript,
  localInfoId,
  localState,
  logFileName,
  lookupFailure,
  matches,
  minutesFromSeconds,
  modelChoices,
  needsAbstractZh,
  parseOpened,
  parseRecentDays,
  refreshSeconds,
  removableWorkdir,
  runMessage,
  runMode,
  retryActions,
  runOptions,
  runTitle,
  runningMarkdown,
  runningRows,
  secondsFromMinutes,
  sections,
  shellQuote,
  shownAbstract,
  shownLookup,
  startFailedMarkdown,
  startSettled,
  startingMarkdown,
  statusTarget,
  stateLabel,
  toolPath,
  typedPaper,
  unfinishedMarkdown,
  unfinishedRows,
  withAbstractZh,
  withOpened,
  withoutOpened,
  workdirName,
} from "../src/core.ts";
import type { Config, Lookup, PaperDetail, PaperMeta, PaperState, PaperStatus, PaperSummary } from "../src/core.ts";

const NOW = Date.parse("2026-11-26T12:00:00Z");
const STAGE_NAMES = ["fetch", "reconstruct", "precompile", "mask", "translate", "compile"];
const ALL_OK = ["ok", "ok", "ok", "ok", "ok", "ok"];

function status(state: PaperState, statuses: string[], overrides: Partial<PaperStatus> = {}): PaperStatus {
  return {
    workdir: "/home/papers/2611.15404v1",
    arxiv_id: "2611.15404",
    version: 1,
    running: state === "running",
    pid: null,
    started_at: null,
    mode: null,
    state,
    stage: state === "ok" ? null : "translate",
    stages: STAGE_NAMES.map((name, index) => ({
      name,
      status: statuses[index] ?? "absent",
      outputs_present: statuses[index] === "ok",
      message: "",
      manifest: null,
    })),
    pdf: null,
    meta: null,
    pages: null,
    source_pages: null,
    translate_seconds: null,
    translate_cost_usd: null,
    recent_actions: [],
    ...overrides,
  };
}

function paper(name: string, state: PaperState, overrides: Partial<PaperSummary> = {}): PaperSummary {
  return {
    name,
    workdir: `/home/papers/${name}`,
    state,
    stage: state === "ok" ? null : "translate",
    arxiv_id: "2611.15404",
    version: 1,
    title: `Title of ${name}`,
    primary_category: "cs.LG",
    added_at: "2026-11-25T08:00:00Z",
    updated_at: "2026-11-25T09:00:00Z",
    started_at: null,
    mode: null,
    pdf: state === "ok" ? `/home/papers/${name}/out/${name}.zh.pdf` : null,
    ...overrides,
  };
}

const META: PaperMeta = {
  arxiv_id: "2611.15404",
  version: 1,
  title: "Full Title",
  authors: ["Ada Lovelace", "Alan Turing"],
  abstract: "An abstract.",
  primary_category: "cs.LG",
  published: new Date(2026, 10, 11, 10, 0).toISOString(),
};

function detail(state: PaperState, statuses: string[], overrides: Partial<PaperStatus> = {}, meta: PaperMeta | null = META): PaperDetail {
  return { name: "2611.15404v1", addedAt: "2026-11-25T08:00:00Z", status: status(state, statuses, overrides), meta };
}

const CONFIG: Config = {
  "modes.default": "smart",
  "modes.fast.model": "claude-code/claude-sonnet-5-5",
  "modes.fast.effort": "low",
  "modes.fast.timeout_seconds": 3600,
  "modes.smart.model": "claude-code/claude-opus-5-5",
  "modes.smart.effort": "medium",
  "modes.smart.timeout_seconds": 5400,
  "fonts.main.regular": "LXGWWenKai-Light.ttf",
  "fonts.main.bold": "LXGWWenKai-Medium.ttf",
  "fonts.sans.regular": "SourceHanSansSC-Regular.otf",
  "fonts.sans.bold": "SourceHanSansSC-Bold.otf",
  "fonts.mono": null,
  "pdf.crop_margins": false,
  "abstract.base_url": "https://api.deepseek.com",
  "abstract.model": "deepseek-flash",
  "abstract.api_key": "",
};

test("arxivId takes new-style and old-style ids out of text", () => {
  assert.equal(arxivId("see 2412.19437v2 for details"), "2412.19437v2");
  assert.equal(arxivId("1412.6980"), "1412.6980");
  assert.equal(arxivId("2412.19437v2.pdf"), "2412.19437v2");
  assert.equal(arxivId("arXiv:2412.19437."), "2412.19437");
  assert.equal(arxivId("hep-th/9901001v3"), "hep-th/9901001v3");
  assert.equal(arxivId("math.GT/0309136"), "math.GT/0309136");
  assert.equal(arxivId("no id here"), null);
});

test("arxivId rejects digits and options that only look like ids", () => {
  assert.equal(arxivId("price 12345.67890"), null);
  assert.equal(arxivId("1234.567890"), null);
  assert.equal(arxivId("2024.12345.67"), null);
  assert.equal(arxivId("foo --mode/1234567"), null);
  assert.equal(arxivId("-hep-th/9901001"), null);
  assert.equal(arxivId("HEP/9901001"), null);
  assert.equal(arxivId("hep-th/99010012"), null);
});

test("arxivId needs a new-style id to start after a separator", () => {
  assert.equal(arxivId("IMG_2024.12345.jpg"), null);
  assert.equal(arxivId("v2024.12345"), null);
  assert.equal(arxivId("1.2024.12345"), null);
  assert.equal(arxivId("arXiv:2611.15404"), "2611.15404");
  assert.equal(arxivId("abs/2611.15404v1"), "2611.15404v1");
  assert.equal(arxivId("2611.15404"), "2611.15404");
  assert.equal(arxivId("read\t2611.15404 today"), "2611.15404");
  assert.equal(arxivId("line one\n2611.15404"), "2611.15404");
  assert.equal(arxivId("IMG_2024.12345.jpg hep-th/9901001"), "hep-th/9901001");
});

test("arxivId takes an old-style id only under an old arXiv archive name", () => {
  assert.equal(arxivId("https://www.jstor.org/stable/1234567"), null);
  assert.equal(arxivId("issues/1234567"), null);
  assert.equal(arxivId("mathematics/0309136"), null);
  assert.equal(arxivId("stat/0701001"), null);
  assert.equal(arxivId("cs/0112017"), "cs/0112017");
  assert.equal(arxivId("cs.CL/0112017"), "cs.CL/0112017");
  assert.equal(arxivId("math-ph/0506203v2"), "math-ph/0506203v2");
  assert.equal(arxivId("abs/cond-mat/9605103"), "cond-mat/9605103");
  for (const archive of OLD_ARCHIVES) assert.equal(arxivId(`see ${archive}/9901001`), `${archive}/9901001`);
  assert.equal(OLD_ARCHIVES.length, 34);
});

test("arxivIdFromUrl requires an arxiv.org host", () => {
  assert.equal(arxivIdFromUrl("https://arxiv.org/abs/2412.19437v2"), "2412.19437v2");
  assert.equal(arxivIdFromUrl("https://www.arxiv.org/pdf/2412.19437"), "2412.19437");
  assert.equal(arxivIdFromUrl("https://export.arxiv.org:443/abs/hep-th/9901001\n"), "hep-th/9901001");
  assert.equal(arxivIdFromUrl("https://example.org/abs/2412.19437"), null);
  assert.equal(arxivIdFromUrl("https://notarxiv.org/abs/2412.19437"), null);
  assert.equal(arxivIdFromUrl("https://arxiv.org/list/cs.LG/recent"), null);
  assert.equal(arxivIdFromUrl("2412.19437"), null);
});

test("isPathArgument recognises local paths", () => {
  assert.equal(isPathArgument("/papers/draft"), true);
  assert.equal(isPathArgument("~/draft"), true);
  assert.equal(isPathArgument("./draft"), true);
  assert.equal(isPathArgument("2611.15404"), false);
  assert.equal(isPathArgument("hep-th/9901001"), false);
});

test("typedPaper accepts an id, a link or a path and nothing else", () => {
  assert.equal(typedPaper(""), null);
  assert.equal(typedPaper("   "), null);
  assert.equal(typedPaper("attention is all you need"), null);
  assert.equal(typedPaper("1706"), null);
  assert.equal(typedPaper(" 1706.03762 "), "1706.03762");
  assert.equal(typedPaper("https://arxiv.org/abs/1706.03762v7"), "1706.03762v7");
  assert.equal(typedPaper("hep-th/9901001"), "hep-th/9901001");
  assert.equal(typedPaper(" ~/papers/draft "), "~/papers/draft");
  assert.equal(typedPaper("/papers/1706.03762/src"), "/papers/1706.03762/src");
  assert.equal(typedPaper("./draft"), "./draft");
});

test("browserUrlScript covers the supported browsers only", () => {
  assert.equal(browserUrlScript("Safari"), 'tell application "Safari" to get URL of front document');
  assert.equal(browserUrlScript("Arc"), 'tell application "Arc" to get URL of active tab of front window');
  assert.equal(browserUrlScript("Google Chrome"), 'tell application "Google Chrome" to get URL of active tab of front window');
  assert.equal(browserUrlScript("Ardia"), 'tell application "Ardia" to get URL of front window');
  assert.equal(browserUrlScript("Finder"), null);
});

test("runMode accepts fast and smart only", () => {
  assert.equal(runMode("fast"), "fast");
  assert.equal(runMode("smart"), "smart");
  assert.equal(runMode(""), null);
  assert.equal(runMode(undefined), null);
  assert.equal(runMode("turbo"), null);
});

test("shellQuote escapes single quotes", () => {
  assert.equal(shellQuote("plain"), "'plain'");
  assert.equal(shellQuote("it's $HOME"), "'it'\\''s $HOME'");
});

test("toolPath expands the home directory", () => {
  assert.equal(toolPath("/Users/me"), "/Users/me/.local/bin:/Users/me/.cargo/bin:/opt/homebrew/bin:/Library/TeX/texbin:/usr/bin:/bin");
});

test("launchScript runs tongtu and then the notification script", () => {
  assert.equal(logFileName("hep-th/9901001"), "hep-th_9901001.log");
  assert.equal(
    launchScript("hep-th/9901001", ["--mode", "fast"], "/support dir/runs/hep-th_9901001.log", "/assets/notify.js", null),
    [
      "tongtu run 'hep-th/9901001' '--mode' 'fast' > '/support dir/runs/hep-th_9901001.log' 2>&1",
      "osascript -l JavaScript '/assets/notify.js' 'hep-th/9901001' $? '/support dir/runs/hep-th_9901001.log'",
    ].join("\n"),
  );
});

test("launchScript passes the working directory to the notification script", () => {
  assert.equal(
    launchScript("draft", ["--workdir", "/home/papers/draft"], "/runs/draft.log", "/assets/notify.js", "/home/papers/draft"),
    [
      "tongtu run 'draft' '--workdir' '/home/papers/draft' > '/runs/draft.log' 2>&1",
      "osascript -l JavaScript '/assets/notify.js' 'draft' $? '/runs/draft.log' '/home/papers/draft'",
    ].join("\n"),
  );
});

test("runOptions maps every action to its tongtu run options", () => {
  assert.deepEqual(runOptions("retry", "/w", null), { options: ["--workdir", "/w"], redo: false });
  assert.deepEqual(runOptions("retry", "/w", "fast"), { options: ["--workdir", "/w", "--mode", "fast"], redo: false });
  assert.deepEqual(runOptions("retryFast", "/w", "smart"), { options: ["--workdir", "/w", "--mode", "fast"], redo: false });
  assert.deepEqual(runOptions("retrySmart", "/w", null), { options: ["--workdir", "/w", "--mode", "smart"], redo: false });
  assert.deepEqual(runOptions("redoFast", "/w", "smart"), {
    options: ["--workdir", "/w", "--from", "translate", "--mode", "fast"],
    redo: true,
  });
  assert.deepEqual(runOptions("redoSmart", "/w", null), { options: ["--workdir", "/w", "--from", "translate", "--mode", "smart"], redo: true });
  assert.deepEqual(runOptions("recompile", "/w", "fast"), { options: ["--workdir", "/w", "--from", "compile"], redo: true });
});

test("runMessage words the result by action", () => {
  assert.equal(runMessage("retry", "2611.15404v1", null), "继续翻译 2611.15404v1");
  assert.equal(runMessage("retry", "2611.15404v1", "fast"), "继续翻译 2611.15404v1（fast）");
  assert.equal(runMessage("retryFast", "2611.15404v1", "smart"), "继续翻译 2611.15404v1（fast）");
  assert.equal(runMessage("retrySmart", "2611.15404v1", null), "继续翻译 2611.15404v1（smart）");
  assert.equal(runMessage("redoFast", "2611.15404v1", null), "开始翻译 2611.15404v1（fast）");
  assert.equal(runMessage("redoSmart", "2611.15404v1", null), "开始翻译 2611.15404v1（smart）");
  assert.equal(runMessage("recompile", "2611.15404v1", null), "重新编译 2611.15404v1");
});

test("retryActions offers the last mode and the other one", () => {
  assert.deepEqual(retryActions("smart"), ["retry", "retryFast"]);
  assert.deepEqual(retryActions("fast"), ["retry", "retrySmart"]);
  assert.deepEqual(retryActions(null), ["retry", "retryFast", "retrySmart"]);
  assert.equal(runTitle("retry", "smart"), "重试（smart）");
  assert.equal(runTitle("retry", null), "重试");
  assert.equal(runTitle("retryFast", "smart"), "用 fast 重试");
  assert.equal(runTitle("retrySmart", null), "用 smart 重试");
  assert.equal(runTitle("redoFast", "smart"), "用 fast 重新翻译");
});

test("a listed paper retries with the mode of its summary", () => {
  const failed = paper("2611.15404v1", "failed", { mode: "fast" });
  assert.deepEqual(retryActions(failed.mode), ["retry", "retrySmart"]);
  assert.equal(runTitle("retry", failed.mode), "重试（fast）");
  assert.deepEqual(runOptions("retry", failed.workdir, failed.mode).options, ["--workdir", "/home/papers/2611.15404v1", "--mode", "fast"]);
});

test("removableWorkdir needs an absolute path that ends in the paper name", () => {
  assert.equal(removableWorkdir("2611.15404v1", "/home/papers/2611.15404v1"), true);
  assert.equal(removableWorkdir("2611.15404v1", "/home/papers/2611.15404v1/"), true);
  assert.equal(removableWorkdir("2611.15404v1", "/home/papers"), false);
  assert.equal(removableWorkdir("2611.15404v1", "papers/2611.15404v1"), false);
  assert.equal(removableWorkdir("", "/"), false);
  assert.equal(removableWorkdir("2611.15404v1", ""), false);
});

test("refreshSeconds is short only while a paper is running", () => {
  assert.equal(refreshSeconds([paper("a", "ok"), paper("b", "running")]), 5);
  assert.equal(refreshSeconds([paper("a", "ok"), paper("b", "failed")]), 60);
  assert.equal(refreshSeconds([]), 60);
});

test("infoFailure tells a missing paper from other failures", () => {
  assert.equal(
    infoFailure("9999.99999", "cannot get info for 9999.99999: arXiv API has no entry for 9999.99999; check the id and version\n"),
    "arXiv 上没有 9999.99999",
  );
  assert.equal(
    infoFailure("2611.15404", "cannot get info for 2611.15404: arXiv API request for 2611.15404 failed: TimeoutError\n"),
    "cannot get info for 2611.15404: arXiv API request for 2611.15404 failed: TimeoutError",
  );
  assert.equal(infoFailure("2611.15404", ""), "tongtu info 2611.15404 没有给出结果");
});

function lookup(paper: string, found: PaperStatus | null, meta: PaperMeta | null, infoError = ""): Lookup {
  return { paper, status: found, meta, infoError, directoryMissing: false };
}

const NO_ENTRY = "cannot get info for 9999.99999: arXiv API has no entry for 9999.99999; check the id and version\n";

test("localState treats a missing or not yet fetched paper as untranslated", () => {
  assert.equal(localState(null), "none");
  assert.equal(localState(status("interrupted", [], { stage: "fetch" })), "none");
  assert.equal(localState(status("running", [], { stage: "fetch" })), "running");
  assert.equal(localState(status("failed", ["download_failed"], { stage: "fetch" })), "failed");
  assert.equal(localState(status("interrupted", ["ok", "ok", "ok"])), "interrupted");
  assert.equal(localState(status("ok", ALL_OK)), "ok");
  assert.deepEqual(LOCAL_STATE_TEXT, {
    none: "未翻译",
    ok: "已翻译",
    running: "正在翻译",
    failed: "翻译失败",
    interrupted: "翻译中断",
  });
});

test("entryActions puts the action for return first in every state", () => {
  assert.deepEqual(entryActions(lookup("2611.15404", null, META)), ["start", "startFast", "startSmart", "arxiv"]);
  assert.deepEqual(entryActions(lookup("2611.15404v1", status("interrupted", [], { stage: "fetch" }), META)), [
    "start",
    "startFast",
    "startSmart",
    "arxiv",
  ]);
  assert.deepEqual(entryActions(lookup("2611.15404", status("ok", ALL_OK), META)), ["detail", "openPdf", "arxiv"]);
  assert.deepEqual(entryActions(lookup("2611.15404", status("running", ["ok"]), META)), ["detail", "stop"]);
  assert.deepEqual(entryActions(lookup("2611.15404", status("failed", ["ok", "x"], { stage: "precompile" }), META)), [
    "detail",
    "retry",
    "retryFast",
    "retrySmart",
  ]);
  assert.deepEqual(entryActions(lookup("2611.15404", status("interrupted", ["ok", "ok", "ok"], { mode: "smart" }), META)), [
    "detail",
    "retry",
    "retryFast",
  ]);
  assert.deepEqual(entryActions(lookup("~/papers/draft", null, null)), ["start", "startFast", "startSmart"]);
  const local = status("ok", ALL_OK, { arxiv_id: null, version: null, workdir: "/home/papers/draft" });
  assert.deepEqual(entryActions(lookup("~/papers/draft", local, null)), ["detail", "openPdf"]);
  assert.deepEqual(Object.keys(ENTRY_ACTION_TITLES).sort(), [
    "arxiv",
    "detail",
    "openPdf",
    "start",
    "startFast",
    "startSmart",
    "stop",
  ]);
});

test("lookupFailure reports a failed info only when the paper is not local", () => {
  assert.equal(lookupFailure(lookup("9999.99999", null, null, NO_ENTRY)), "arXiv 上没有 9999.99999");
  assert.equal(
    lookupFailure(lookup("2611.15404", null, null, "cannot get info for 2611.15404: arXiv API request for 2611.15404 failed\n")),
    "cannot get info for 2611.15404: arXiv API request for 2611.15404 failed",
  );
  assert.equal(lookupFailure(lookup("2611.15404v1", status("interrupted", [], { stage: "fetch" }), null, NO_ENTRY)), "arXiv 上没有 2611.15404v1");
  assert.equal(lookupFailure(lookup("2611.15404", status("ok", ALL_OK), null, NO_ENTRY)), null);
  assert.equal(lookupFailure(lookup("2611.15404", null, META)), null);
  assert.equal(lookupFailure(lookup("./draft", null, null)), null);
});

test("entryTitle and entrySubtitle describe the paper and where the input came from", () => {
  const remote = lookup("2611.15404", null, META);
  assert.equal(entryTitle(remote), "Full Title");
  assert.equal(entrySubtitle(remote, "clipboard"), "2611.15404v1 · cs.LG · 来自剪贴板");
  assert.equal(entrySubtitle(remote, "browser"), "2611.15404v1 · cs.LG · 来自浏览器");
  assert.equal(entrySubtitle(remote, "argument"), "2611.15404v1 · cs.LG · 来自参数");
  assert.equal(entrySubtitle(remote, null), "2611.15404v1 · cs.LG");
  const localOnly = lookup("2611.15404", status("ok", ALL_OK), null, NO_ENTRY);
  assert.equal(entryTitle(localOnly), "2611.15404v1");
  assert.equal(entrySubtitle(localOnly, null), "2611.15404v1");
  assert.equal(entryArxivUrl(localOnly), "https://arxiv.org/abs/2611.15404v1");
  const directory = lookup("~/papers/my draft/", null, null);
  assert.equal(entryTitle(directory), "my draft");
  assert.equal(entrySubtitle(directory, "argument"), "来自参数");
  assert.equal(entrySubtitle(directory, null), "");
  assert.equal(entryArxivUrl(directory), null);
  assert.equal(entryArxivUrl(remote), "https://arxiv.org/abs/2611.15404v1");
});

test("entryRows shows authors, date, state and mode", () => {
  assert.deepEqual(entryRows(lookup("2611.15404", null, META), "smart"), [
    { title: "作者", text: "Ada Lovelace, Alan Turing" },
    { title: "发表日期", text: "2026-11-11" },
    { title: "状态", text: "未翻译" },
    { title: "模式", text: "smart" },
  ]);
  assert.deepEqual(entryRows(lookup("./draft", status("running", ["ok"]), null), ""), [{ title: "状态", text: "正在翻译" }]);
  assert.deepEqual(entryRows(lookup("./draft", status("failed", ["ok", "x"]), null), "fast"), [
    { title: "状态", text: "翻译失败" },
    { title: "模式", text: "fast" },
  ]);
});

test("statusTarget carries what the actions need", () => {
  assert.deepEqual(statusTarget(status("ok", ALL_OK, { pdf: "/home/papers/2611.15404v1/out/x.pdf", mode: "fast" })), {
    name: "2611.15404v1",
    workdir: "/home/papers/2611.15404v1",
    state: "ok",
    stage: null,
    arxiv_id: "2611.15404",
    version: 1,
    pdf: "/home/papers/2611.15404v1/out/x.pdf",
    mode: "fast",
  });
  assert.equal(startingMarkdown("2611.15404"), "# 2611.15404\n\n正在启动");
});

test("sections groups papers by state and age and drops empty sections", () => {
  const papers = [
    paper("running", "running"),
    paper("failed", "failed"),
    paper("interrupted", "interrupted"),
    paper("recent", "ok", { updated_at: "2026-11-20T12:00:00Z" }),
    paper("old", "ok", { updated_at: "2026-11-01T12:00:00Z" }),
  ];
  const grouped = sections(papers, 7, NOW).map((section) => [section.title, section.papers.map((entry) => entry.name)]);
  assert.deepEqual(grouped, [
    ["进行中", ["running"]],
    ["需要处理", ["failed", "interrupted"]],
    ["最近 7 天", ["recent"]],
    ["更早", ["old"]],
  ]);
  assert.deepEqual(
    sections(papers, 30, NOW).map((section) => section.title),
    ["进行中", "需要处理", "最近 30 天"],
  );
  assert.deepEqual(sections([], 7, NOW), []);
});

test("matches searches title and name and applies the filter", () => {
  const read = paper("2611.15404v1", "ok");
  const unread = paper("2512.02556v2", "ok", { primary_category: "cs.CL" });
  const local = paper("local", "failed", { title: null, primary_category: null });
  const opened = ["2611.15404v1"];
  assert.equal(matches(read, "", FILTER_ALL, opened), true);
  assert.equal(matches(read, "TITLE 2611", FILTER_ALL, opened), true);
  assert.equal(matches(read, "15404", FILTER_ALL, opened), true);
  assert.equal(matches(read, "turing", FILTER_ALL, opened), false);
  assert.equal(matches(read, "", FILTER_UNREAD, opened), false);
  assert.equal(matches(unread, "", FILTER_UNREAD, opened), true);
  assert.equal(matches(unread, "", "cs.CL", opened), true);
  assert.equal(matches(read, "", "cs.CL", opened), false);
  assert.equal(matches(local, "local", FILTER_ALL, opened), true);
  assert.deepEqual(categories([read, unread, local]), ["cs.CL", "cs.LG"]);
});

test("opened set starts from the finished papers and follows open and redo", () => {
  const papers = [paper("a", "ok"), paper("b", "running"), paper("c", "ok")];
  assert.equal(parseOpened(undefined), null);
  assert.equal(parseOpened("not json"), null);
  assert.deepEqual(parseOpened('["a", 3, "c"]'), ["a", "c"]);
  assert.deepEqual(initialOpened(papers), ["a", "c"]);
  assert.deepEqual(withOpened(["a"], "c"), ["a", "c"]);
  assert.deepEqual(withOpened(["a"], "a"), ["a"]);
  assert.deepEqual(withoutOpened(["a", "c"], "a"), ["c"]);
  assert.equal(isUnread(papers[0], ["c"]), true);
  assert.equal(isUnread(papers[0], ["a"]), false);
  assert.equal(isUnread(papers[0], null), false);
  assert.equal(isUnread(papers[1], []), false);
});

test("parseRecentDays accepts positive integers only", () => {
  assert.equal(parseRecentDays("14"), 14);
  assert.equal(parseRecentDays(" 3 "), 3);
  assert.equal(parseRecentDays("0"), null);
  assert.equal(parseRecentDays("1.5"), null);
  assert.equal(parseRecentDays("-2"), null);
  assert.equal(parseRecentDays("week"), null);
  assert.equal(parseRecentDays(undefined), null);
});

test("clock and elapsed format minutes and seconds", () => {
  assert.equal(clock(0), "00:00");
  assert.equal(clock(201.9), "03:21");
  assert.equal(clock(4503), "75:03");
  assert.equal(clock(-5), "00:00");
  assert.equal(elapsed("2026-11-26T11:56:39Z", NOW), "03:21");
  assert.equal(elapsed(null, NOW), "00:00");
});

test("dateText and dateTimeText use the local time zone", () => {
  const iso = new Date(2026, 10, 6, 9, 5).toISOString();
  assert.equal(dateText(iso), "2026-11-06");
  assert.equal(dateTimeText(iso), "2026-11-06 09:05");
});

test("stateLabel describes each state", () => {
  const added = new Date(2026, 10, 25, 10, 0).toISOString();
  assert.equal(stateLabel(paper("r", "running", { started_at: "2026-11-26T11:56:39Z" }), NOW), "translate 03:21");
  assert.equal(stateLabel(paper("f", "failed", { stage: "compile" }), NOW), "compile 失败");
  assert.equal(stateLabel(paper("i", "interrupted"), NOW), "translate 中断");
  assert.equal(stateLabel(paper("o", "ok", { added_at: added }), NOW), "cs.LG · 2026-11-25");
  assert.equal(stateLabel(paper("o", "ok", { added_at: added, primary_category: null }), NOW), "2026-11-25");
});

test("stateLabel of a running paper without a stage shows the time only", () => {
  assert.equal(stateLabel(paper("r", "running", { stage: null, started_at: "2026-11-26T11:56:39Z" }), NOW), "03:21");
});

test("arxivUrl carries the version and is empty for local papers", () => {
  assert.equal(arxivUrl(paper("2611.15404v1", "ok")), "https://arxiv.org/abs/2611.15404v1");
  assert.equal(arxivUrl(paper("local", "ok", { arxiv_id: null, version: null })), null);
});

test("completedMarkdown shows the title, the thumbnail and the abstract", () => {
  const complete = detail("ok", ALL_OK);
  assert.equal(completedMarkdown(complete, null), "# Full Title\n\nAn abstract.");
  assert.equal(completedMarkdown(complete, "iVBORw0KGgo="), "# Full Title\n\n![](data:image/png;base64,iVBORw0KGgo=)\n\nAn abstract.");
  assert.equal(completedMarkdown(detail("ok", ALL_OK, {}, null), null), "# 2611.15404v1");
});

test("completedRows leaves out what is not available", () => {
  const added = new Date(2026, 10, 25, 10, 0).toISOString();
  const complete = detail("ok", ALL_OK, {
    mode: "smart",
    pdf: "/p.pdf",
    pages: 23,
    source_pages: 21,
    translate_seconds: 754.4,
    translate_cost_usd: 1.234,
  });
  complete.addedAt = added;
  assert.deepEqual(completedRows(complete), [
    { title: "arXiv", text: "2611.15404v1", link: "https://arxiv.org/abs/2611.15404v1" },
    { title: "作者", text: "Ada Lovelace, Alan Turing" },
    { title: "分类与发表日期", text: "cs.LG · 2026-11-11" },
    { title: "加入日期", text: "2026-11-25" },
    { title: "页数", text: "中文 23 页，原文 21 页" },
    { title: "模式", text: "smart" },
    { title: "翻译耗时与费用", text: "12 分 34 秒 · $1.23" },
  ]);
  const local = detail("ok", ALL_OK, { arxiv_id: null, version: null }, null);
  local.addedAt = added;
  assert.deepEqual(completedRows(local), [{ title: "加入日期", text: "2026-11-25" }]);
  local.addedAt = null;
  assert.deepEqual(completedRows(local), []);
});

test("running detail marks the current stage and keeps the time in the metadata", () => {
  const started = new Date(2026, 10, 26, 9, 5).toISOString();
  const now = Date.parse(started) + 201_000;
  const running = detail("running", ["ok", "ok", "ok", "ok"], {
    started_at: started,
    mode: "fast",
    source_pages: 12,
    recent_actions: ["Read en.tex", "Write  parts/01.tex"],
  });
  assert.equal(
    runningMarkdown(running),
    [
      "# Full Title",
      "fetch ✓ → reconstruct ✓ → precompile ✓ → mask ✓ → **translate** → compile",
      "## 会话最近的动作",
      "- Read en.tex\n- Write parts/01.tex",
    ].join("\n\n"),
  );
  assert.ok(!runningMarkdown(running).includes("已运行"));
  assert.ok(!runningMarkdown(detail("running", ["ok", "ok", "ok"])).includes("会话最近的动作"));
  assert.deepEqual(runningRows(running, now), [
    { title: "arXiv", text: "2611.15404v1", link: "https://arxiv.org/abs/2611.15404v1" },
    { title: "作者", text: "Ada Lovelace, Alan Turing" },
    { title: "分类与发表日期", text: `cs.LG · ${dateText(META.published)}` },
    { title: "加入日期", text: dateText("2026-11-25T08:00:00Z") },
    { title: "页数", text: "原文 12 页" },
    { title: "模式", text: "fast" },
    { title: "开始时间", text: "2026-11-26 09:05" },
    { title: "总用时", text: "03:21" },
  ]);
});

test("unfinished detail gives the reason and the next steps", () => {
  const failed = detail("failed", ["ok", "ok", "ok", "ok", "ok", "compile_failed"], { stage: "compile", mode: "smart", source_pages: 12 });
  failed.status.stages[5].message = "latexmk exited\nwith 12";
  assert.equal(
    unfinishedMarkdown(failed),
    [
      "# Full Title",
      "fetch ✓ → reconstruct ✓ → precompile ✓ → mask ✓ → translate ✓ → **compile ✗**",
      "## 原因",
      "`compile` 失败：compile_failed latexmk exited with 12",
      "## 下一步",
      "- 重试（smart）\n- 用 fast 重试\n- 查看日志",
    ].join("\n\n"),
  );
  assert.deepEqual(unfinishedRows(failed), [
    { title: "arXiv", text: "2611.15404v1", link: "https://arxiv.org/abs/2611.15404v1" },
    { title: "作者", text: "Ada Lovelace, Alan Turing" },
    { title: "分类与发表日期", text: `cs.LG · ${dateText(META.published)}` },
    { title: "加入日期", text: dateText("2026-11-25T08:00:00Z") },
    { title: "页数", text: "原文 12 页" },
    { title: "模式", text: "smart" },
    { title: "失败的阶段", text: "compile" },
    { title: "已完成的阶段数", text: "5 / 6" },
  ]);
  const interrupted = { ...detail("interrupted", ["ok", "ok", "ok", "ok"], {}, null), addedAt: null };
  assert.ok(unfinishedMarkdown(interrupted).includes("fetch ✓ → reconstruct ✓ → precompile ✓ → mask ✓ → translate → compile"));
  assert.ok(unfinishedMarkdown(interrupted).includes("在 `translate` 阶段中断"));
  assert.ok(unfinishedMarkdown(interrupted).includes("- 重试\n- 用 fast 重试\n- 用 smart 重试\n- 查看日志"));
  assert.deepEqual(unfinishedRows(interrupted).slice(1), [
    { title: "中断的阶段", text: "translate" },
    { title: "已完成的阶段数", text: "4 / 6" },
  ]);
});

test("minutes and seconds convert both ways", () => {
  assert.equal(minutesFromSeconds(3600), "60");
  assert.equal(minutesFromSeconds(90), "1.5");
  assert.equal(secondsFromMinutes("60"), 3600);
  assert.equal(secondsFromMinutes(" 1.5 "), 90);
  assert.equal(secondsFromMinutes("0"), null);
  assert.equal(secondsFromMinutes("-5"), null);
  assert.equal(secondsFromMinutes("abc"), null);
  assert.equal(secondsFromMinutes(""), null);
});

test("model and effort choices list the current value when it is not a preset", () => {
  assert.deepEqual(modelChoices("claude-code/claude-opus-5-5"), ["claude-code/claude-sonnet-5-5", "claude-code/claude-opus-5-5"]);
  assert.deepEqual(modelChoices("codex/gpt-5"), ["claude-code/claude-sonnet-5-5", "claude-code/claude-opus-5-5", "codex/gpt-5"]);
  assert.deepEqual(effortChoices("medium"), ["low", "medium", "high"]);
  assert.deepEqual(effortChoices("xhigh"), ["low", "medium", "high", "xhigh"]);
});

test("formValues turns config values into form values", () => {
  const values = formValues(CONFIG);
  assert.equal(values["modes.default"], "smart");
  assert.equal(values["modes.smart.timeout_seconds"], "90");
  assert.equal(values["modes.fast.model.custom"], "");
  assert.equal(values["fonts.mono"], "");
  assert.equal(values["pdf.crop_margins"], false);
});

test("configChanges lists only the changed keys", () => {
  assert.deepEqual(configChanges(CONFIG, formValues(CONFIG)), { pairs: [], errors: {} });
  const form = {
    ...formValues(CONFIG),
    "modes.default": "fast",
    "modes.fast.model": OTHER_MODEL,
    "modes.fast.model.custom": " codex/gpt-5 ",
    "modes.smart.effort": "high",
    "modes.smart.timeout_seconds": "30",
    "fonts.main.bold": "",
    "fonts.mono": " /fonts/Mono.ttf ",
    "pdf.crop_margins": true,
  };
  assert.deepEqual(configChanges(CONFIG, form), {
    pairs: [
      "modes.default",
      "fast",
      "modes.fast.model",
      "codex/gpt-5",
      "modes.smart.effort",
      "high",
      "modes.smart.timeout_seconds",
      "1800",
      "fonts.main.bold",
      "",
      "fonts.mono",
      "/fonts/Mono.ttf",
      "pdf.crop_margins",
      "true",
    ],
    errors: {},
  });
  assert.deepEqual(configChanges({ ...CONFIG, "pdf.crop_margins": true }, { ...formValues(CONFIG), "pdf.crop_margins": false }).pairs, [
    "pdf.crop_margins",
    "false",
  ]);
});

test("configChanges reports a timeout that is not a positive number", () => {
  const result = configChanges(CONFIG, { ...formValues(CONFIG), "modes.fast.timeout_seconds": "-5", "modes.default": "fast" });
  assert.deepEqual(result.errors, { "modes.fast.timeout_seconds": "要求是正数" });
  assert.deepEqual(result.pairs, ["modes.default", "fast"]);
});

test("configErrors assigns stderr lines to form fields", () => {
  const keys = Object.keys(CONFIG);
  const stderr = "modes.fast.model: Value error, expected runtime/model\nconfig key nope does not exist; run tongtu config list to see the keys\n";
  assert.deepEqual(configErrors(stderr, keys), {
    fields: { "modes.fast.model": "Value error, expected runtime/model" },
    first: "modes.fast.model: Value error, expected runtime/model",
  });
  assert.deepEqual(configErrors("config key nope does not exist\n", keys), { fields: {}, first: "config key nope does not exist" });
  assert.deepEqual(configErrors("", keys), { fields: {}, first: "" });
});

test("configFile follows TONGTU_HOME", () => {
  assert.equal(configFile("/Users/me", undefined), "/Users/me/.tongtu/config.toml");
  assert.equal(configFile("/Users/me", "/Users/me/.tongtu-dev"), "/Users/me/.tongtu-dev/config.toml");
});

test("shownAbstract prefers the Chinese abstract", () => {
  assert.equal(shownAbstract(META), "An abstract.");
  assert.equal(shownAbstract({ ...META, abstract_zh: null }), "An abstract.");
  assert.equal(shownAbstract({ ...META, abstract_zh: "" }), "An abstract.");
  assert.equal(shownAbstract({ ...META, abstract_zh: "中文摘要" }), "中文摘要");
  assert.equal(shownAbstract(null), "");
});

test("completedMarkdown shows the Chinese abstract when there is one", () => {
  const complete = detail("ok", ALL_OK, {}, { ...META, abstract_zh: "中文摘要" });
  assert.equal(completedMarkdown(complete, null), "# Full Title\n\n中文摘要");
});

test("needsAbstractZh asks only for a paper with info and no Chinese abstract", () => {
  assert.equal(needsAbstractZh(META), true);
  assert.equal(needsAbstractZh({ ...META, abstract_zh: null }), true);
  assert.equal(needsAbstractZh({ ...META, abstract_zh: "中文摘要" }), false);
  assert.equal(needsAbstractZh(null), false);
});

test("withAbstractZh takes the Chinese abstract only from the same version", () => {
  const translated = { ...META, title: "other", abstract_zh: "中文摘要" };
  assert.deepEqual(withAbstractZh(META, translated), { ...META, abstract_zh: "中文摘要" });
  assert.deepEqual(withAbstractZh(META, { ...translated, version: 2 }), META);
  assert.deepEqual(withAbstractZh(META, { ...translated, arxiv_id: "2611.99999" }), META);
  assert.deepEqual(withAbstractZh(META, { ...META, abstract_zh: null }), META);
  assert.deepEqual(withAbstractZh(META, null), META);
  assert.equal(withAbstractZh(null, translated), null);
});

test("apiKeyPlaceholder tells whether a key is set", () => {
  assert.equal(apiKeySet(CONFIG), false);
  assert.equal(apiKeyPlaceholder(CONFIG), "未设置");
  const withKey = { ...CONFIG, [API_KEY_KEY]: "********" };
  assert.equal(apiKeySet(withKey), true);
  assert.equal(apiKeyPlaceholder(withKey), "已设置，留空表示不修改");
  assert.equal(formValues(withKey)[API_KEY_KEY], "");
});

test("configChanges writes the key only when it is filled in", () => {
  const withKey = { ...CONFIG, [API_KEY_KEY]: "********" };
  assert.deepEqual(configChanges(withKey, formValues(withKey)).pairs, []);
  assert.deepEqual(configChanges(withKey, { ...formValues(withKey), [API_KEY_KEY]: "   " }).pairs, []);
  assert.deepEqual(configChanges(CONFIG, { ...formValues(CONFIG), [API_KEY_KEY]: " sk-new " }).pairs, [API_KEY_KEY, "sk-new"]);
  assert.deepEqual(configChanges(withKey, { ...formValues(withKey), [API_KEY_KEY]: "********" }).pairs, [API_KEY_KEY, "********"]);
  assert.deepEqual(configChanges(CONFIG, { ...formValues(CONFIG), [API_KEY_KEY]: "123456" }).pairs, [API_KEY_KEY, "123456"]);
  assert.deepEqual(CLEAR_API_KEY_PAIR, [API_KEY_KEY, ""]);
});

test("configChanges lists the changed abstract translation fields", () => {
  const form = { ...formValues(CONFIG), "abstract.base_url": " https://chat.example.com ", "abstract.model": "deepseek-flash" };
  assert.deepEqual(configChanges(CONFIG, form).pairs, ["abstract.base_url", "https://chat.example.com"]);
});

test("authorsText lists at most 12 authors", () => {
  const names = Array.from({ length: 14 }, (_, index) => `A${index + 1}`);
  assert.equal(SHOWN_AUTHORS, 12);
  assert.equal(authorsText(names.slice(0, 1)), "A1");
  assert.equal(authorsText(names.slice(0, 12)), names.slice(0, 12).join(", "));
  assert.equal(authorsText(names.slice(0, 13)), `${names.slice(0, 12).join(", ")}, ...`);
  assert.equal(authorsText(names), `${names.slice(0, 12).join(", ")}, ...`);
});

test("the author rows use authorsText", () => {
  const many = { ...META, authors: Array.from({ length: 13 }, (_, index) => `A${index + 1}`) };
  const expected = authorsText(many.authors);
  assert.ok(expected.endsWith(", ..."));
  const rows = [
    completedRows(detail("ok", ALL_OK, {}, many)),
    runningRows(detail("running", ["ok"], {}, many), NOW),
    unfinishedRows(detail("failed", ["ok", "x"], { stage: "precompile" }, many)),
    entryRows(lookup("2611.15404", null, many), ""),
  ];
  for (const found of rows) assert.equal(found.find((row) => row.title === "作者")?.text, expected);
});

test("lookupFailure reports a local directory that does not exist", () => {
  assert.equal(lookupFailure({ ...lookup("~/papers/missing", null, null), directoryMissing: true }), "没有这个目录");
});

test("expandedPath expands only a leading home directory", () => {
  assert.equal(expandedPath("~/papers/draft", "/Users/me"), "/Users/me/papers/draft");
  assert.equal(expandedPath("~", "/Users/me"), "/Users/me");
  assert.equal(expandedPath("~other/draft", "/Users/me"), "~other/draft");
  assert.equal(expandedPath("./draft", "/Users/me"), "./draft");
  assert.equal(expandedPath("/tmp/~/draft", "/Users/me"), "/tmp/~/draft");
});

test("shownLookup shows a lookup only for the paper of the current input", () => {
  const found = lookup("2611.15404", null, META);
  assert.equal(shownLookup(found, "2611.15404"), found);
  assert.equal(shownLookup(found, " https://arxiv.org/abs/2611.15404 "), found);
  assert.equal(shownLookup(found, "2611.1540"), null);
  assert.equal(shownLookup(found, "2611.15405"), null);
  assert.equal(shownLookup(found, ""), null);
  assert.equal(shownLookup(null, "2611.15404"), null);
});

test("localInfoId names the local version when it differs from the info", () => {
  const local = status("ok", ALL_OK);
  assert.equal(localInfoId("2611.15404", local, { ...META, version: 3 }), "2611.15404v1");
  assert.equal(localInfoId("2611.15404", local, null), "2611.15404v1");
  assert.equal(localInfoId("2611.15404", local, META), null);
  assert.equal(localInfoId("2611.15404v1", local, null), null);
  assert.equal(localInfoId("2611.15404", null, META), null);
  assert.equal(localInfoId("./draft", status("ok", ALL_OK, { arxiv_id: null, version: null }), null), null);
  const old = status("ok", ALL_OK, { arxiv_id: "hep-th/9901001", version: 2, workdir: "/home/papers/hep-th_9901001v2" });
  assert.equal(localInfoId("hep-th/9901001", old, { ...META, arxiv_id: "hep-th/9901001", version: 3 }), "hep-th/9901001v2");
});

test("startSettled waits for a running paper or a finished stage", () => {
  assert.equal(startSettled(null), false);
  assert.equal(startSettled(status("interrupted", [], { stage: "fetch" })), false);
  assert.equal(startSettled(status("running", [], { stage: "fetch" })), true);
  assert.equal(startSettled(status("failed", ["download_failed"], { stage: "fetch" })), true);
  assert.equal(startSettled(status("interrupted", ["ok"], { stage: "precompile" })), true);
  assert.equal(startFailedMarkdown("2611.15404"), "# 2611.15404\n\n没有启动成功，原因见系统通知");
});
