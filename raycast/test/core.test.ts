import assert from "node:assert/strict";
import test from "node:test";
import {
  FILTER_ALL,
  FILTER_UNREAD,
  OLD_ARCHIVES,
  OTHER_MODEL,
  arxivId,
  arxivIdFromUrl,
  arxivUrl,
  browserUrlScript,
  categories,
  clock,
  completedMarkdown,
  completedRows,
  configChanges,
  configErrors,
  configFile,
  confirmRows,
  dateText,
  dateTimeText,
  effortChoices,
  elapsed,
  formValues,
  infoFailure,
  initialOpened,
  isComplete,
  isUnread,
  launchScript,
  logFileName,
  matches,
  minutesFromSeconds,
  modelChoices,
  paperArgument,
  parseOpened,
  parseRecentDays,
  refreshSeconds,
  removableWorkdir,
  runMessage,
  runMode,
  runOptions,
  runningMarkdown,
  runningRows,
  secondsFromMinutes,
  sections,
  shellQuote,
  startMessage,
  stateLabel,
  toolPath,
  unfinishedMarkdown,
  unfinishedRows,
  withOpened,
  withoutOpened,
  workdirName,
} from "../src/core.ts";
import type { Config, PaperDetail, PaperMeta, PaperState, PaperStatus, PaperSummary } from "../src/core.ts";

const NOW = Date.parse("2026-11-26T12:00:00Z");
const STAGE_NAMES = ["fetch", "precompile", "mask", "translate", "compile"];
const ALL_OK = ["ok", "ok", "ok", "ok", "ok"];

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
      warnings: [],
      manifest: null,
    })),
    pdf: null,
    meta: null,
    glossary: null,
    notes: null,
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
  return { summary: paper("2611.15404v1", state), status: status(state, statuses, overrides), meta };
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

test("paperArgument keeps text that holds no arXiv id", () => {
  assert.equal(paperArgument(" https://arxiv.org/abs/2412.19437v2 "), "2412.19437v2");
  assert.equal(paperArgument("  my draft  "), "my draft");
});

test("paperArgument takes a path as it is", () => {
  assert.equal(paperArgument(" /papers/2412.19437v2/src "), "/papers/2412.19437v2/src");
  assert.equal(paperArgument("~/papers/2412.19437"), "~/papers/2412.19437");
  assert.equal(paperArgument("./2412.19437"), "./2412.19437");
  assert.equal(paperArgument("../hep-th/9901001"), "../hep-th/9901001");
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
  assert.deepEqual(runOptions("retry", "/w"), { options: ["--workdir", "/w"], redo: false });
  assert.deepEqual(runOptions("retrySmart", "/w"), { options: ["--workdir", "/w", "--mode", "smart"], redo: false });
  assert.deepEqual(runOptions("redoFast", "/w"), { options: ["--workdir", "/w", "--from", "translate", "--mode", "fast"], redo: true });
  assert.deepEqual(runOptions("redoSmart", "/w"), { options: ["--workdir", "/w", "--from", "translate", "--mode", "smart"], redo: true });
  assert.deepEqual(runOptions("recompile", "/w"), { options: ["--workdir", "/w", "--from", "compile"], redo: true });
});

test("runMessage and startMessage word the HUD by action", () => {
  assert.equal(runMessage("retry", "2611.15404v1"), "继续翻译 2611.15404v1");
  assert.equal(runMessage("retrySmart", "2611.15404v1"), "继续翻译 2611.15404v1（smart）");
  assert.equal(runMessage("redoFast", "2611.15404v1"), "开始翻译 2611.15404v1（fast）");
  assert.equal(runMessage("redoSmart", "2611.15404v1"), "开始翻译 2611.15404v1（smart）");
  assert.equal(runMessage("recompile", "2611.15404v1"), "重新编译 2611.15404v1");
  assert.equal(startMessage("2611.15404", "smart"), "开始翻译 2611.15404（smart）");
  assert.equal(startMessage("2611.15404", ""), "开始翻译 2611.15404");
});

test("removableWorkdir needs an absolute path that ends in the paper name", () => {
  assert.equal(removableWorkdir("2611.15404v1", "/home/papers/2611.15404v1"), true);
  assert.equal(removableWorkdir("2611.15404v1", "/home/papers/2611.15404v1/"), true);
  assert.equal(removableWorkdir("2611.15404v1", "/home/papers"), false);
  assert.equal(removableWorkdir("2611.15404v1", "papers/2611.15404v1"), false);
  assert.equal(removableWorkdir("", "/"), false);
  assert.equal(removableWorkdir("2611.15404v1", ""), false);
});

test("isComplete needs the ok state and a pdf", () => {
  assert.equal(isComplete(status("ok", ALL_OK, { pdf: "/p.pdf" })), true);
  assert.equal(isComplete(status("ok", ALL_OK)), false);
  assert.equal(isComplete(status("failed", ["ok", "ok", "ok", "ok", "compile_failed"], { pdf: "/p.pdf" })), false);
  assert.equal(isComplete(status("interrupted", ["ok", "ok", "ok"], { pdf: "/p.pdf" })), false);
  assert.equal(workdirName("/home/papers/2611.15404v1/"), "2611.15404v1");
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

test("confirmRows lists what the confirmation screen shows", () => {
  assert.deepEqual(confirmRows(META, "smart"), [
    { title: "作者", text: "Ada Lovelace, Alan Turing" },
    { title: "编号与版本", text: "2611.15404v1" },
    { title: "主分类", text: "cs.LG" },
    { title: "发表日期", text: "2026-11-11" },
    { title: "模式", text: "smart" },
  ]);
  assert.equal(confirmRows(META, "").length, 4);
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

test("completedMarkdown shows only the sections whose files exist", () => {
  const complete = detail("ok", ALL_OK);
  assert.equal(completedMarkdown(complete, null, null, null), "# Full Title\n\nAn abstract.");
  const full = completedMarkdown(
    complete,
    "/cache dir/2611.15404v1.png",
    { notes: [{ where: "main.tex:12", problem: "macro\nundefined", resolution: "kept the original" }] },
    { terms: [{ word: "trust region", translation: "信赖域" }, { word: "a|b", translation: "甲" }] },
  );
  assert.ok(full.includes("![](file:///cache%20dir/2611.15404v1.png)"));
  assert.ok(full.includes("## 译者备注 · 1 条"));
  assert.ok(full.includes("**main.tex:12**\n\n问题：macro undefined\n\n处理：kept the original"));
  assert.ok(full.includes("## 本篇术语 · 2 条"));
  assert.ok(full.includes("| 原词 | 译法 |\n|---|---|\n| trust region | 信赖域 |\n| a\\|b | 甲 |"));
  assert.equal(completedMarkdown(detail("ok", ALL_OK, {}, null), null, null, null), "# Title of 2611.15404v1");
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
  complete.summary.added_at = added;
  complete.status.stages[1].warnings = ["one", "two"];
  assert.deepEqual(completedRows(complete), [
    { title: "arXiv", text: "2611.15404v1", link: "https://arxiv.org/abs/2611.15404v1" },
    { title: "作者", text: "Ada Lovelace, Alan Turing" },
    { title: "分类与发表日期", text: "cs.LG · 2026-11-11" },
    { title: "加入日期", text: "2026-11-25" },
    { title: "页数", text: "中文 23 页，原文 21 页" },
    { title: "模式", text: "smart" },
    { title: "翻译耗时与费用", text: "12 分 34 秒 · $1.23" },
    { title: "提醒", text: "2 条" },
  ]);
  const local = detail("ok", ALL_OK, { arxiv_id: null, version: null }, null);
  local.summary.added_at = added;
  assert.deepEqual(completedRows(local), [
    { title: "加入日期", text: "2026-11-25" },
    { title: "提醒", text: "无" },
  ]);
});

test("running detail marks the current stage and keeps the time in the metadata", () => {
  const started = new Date(2026, 10, 26, 9, 5).toISOString();
  const now = Date.parse(started) + 201_000;
  const running = detail("running", ["ok", "ok", "ok"], {
    started_at: started,
    mode: "fast",
    recent_actions: ["Read en.tex", "Write  parts/01.tex"],
  });
  assert.equal(
    runningMarkdown(running),
    [
      "# Full Title",
      "fetch ✓ → precompile ✓ → mask ✓ → **translate** → compile",
      "## 会话最近的动作",
      "- Read en.tex\n- Write parts/01.tex",
    ].join("\n\n"),
  );
  assert.ok(!runningMarkdown(running).includes("已运行"));
  assert.ok(!runningMarkdown(detail("running", ["ok", "ok", "ok"])).includes("会话最近的动作"));
  assert.deepEqual(runningRows(running, now), [
    { title: "arXiv", text: "2611.15404v1", link: "https://arxiv.org/abs/2611.15404v1" },
    { title: "模式", text: "fast" },
    { title: "开始时间", text: "2026-11-26 09:05" },
    { title: "总用时", text: "03:21" },
  ]);
});

test("unfinished detail gives the reason and the next steps", () => {
  const failed = detail("failed", ["ok", "ok", "ok", "ok", "compile_failed"], { stage: "compile", mode: "smart" });
  failed.status.stages[4].message = "latexmk exited\nwith 12";
  assert.equal(
    unfinishedMarkdown(failed),
    [
      "# Full Title",
      "fetch ✓ → precompile ✓ → mask ✓ → translate ✓ → **compile ✗**",
      "## 原因",
      "`compile` 失败：compile_failed latexmk exited with 12",
      "## 下一步",
      "- 重试\n- 用 smart 模式重试\n- 查看日志",
    ].join("\n\n"),
  );
  assert.deepEqual(unfinishedRows(failed), [
    { title: "arXiv", text: "2611.15404v1", link: "https://arxiv.org/abs/2611.15404v1" },
    { title: "模式", text: "smart" },
    { title: "失败的阶段", text: "compile" },
    { title: "已完成的阶段数", text: "4 / 5" },
  ]);
  const interrupted = detail("interrupted", ["ok", "ok", "ok"]);
  assert.ok(unfinishedMarkdown(interrupted).includes("fetch ✓ → precompile ✓ → mask ✓ → translate → compile"));
  assert.ok(unfinishedMarkdown(interrupted).includes("在 `translate` 阶段中断"));
  assert.deepEqual(unfinishedRows(interrupted).slice(1), [
    { title: "中断的阶段", text: "translate" },
    { title: "已完成的阶段数", text: "3 / 5" },
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
