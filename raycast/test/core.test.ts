import assert from "node:assert/strict";
import test from "node:test";
import {
  FILTER_ALL,
  FILTER_UNREAD,
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
  dateText,
  dateTimeText,
  effortChoices,
  elapsed,
  formValues,
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
  runMode,
  runOptions,
  runningMarkdown,
  runningRows,
  secondsFromMinutes,
  sections,
  shellQuote,
  stateLabel,
  toolPath,
  unfinishedMarkdown,
  unfinishedRows,
  withOpened,
  withoutOpened,
  workdirName,
} from "../src/core.ts";
import type { Config, PaperEntry, PaperState, PaperStatus } from "../src/core.ts";

const NOW = Date.parse("2026-11-26T12:00:00Z");
const STAGE_NAMES = ["fetch", "precompile", "mask", "translate", "compile"];

function status(statuses: string[], overrides: Partial<PaperStatus> = {}): PaperStatus {
  return {
    workdir: "/home/papers/2611.15404v1",
    arxiv_id: "2611.15404",
    version: 1,
    running: false,
    pid: null,
    started_at: null,
    mode: null,
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
    ...overrides,
  };
}

const ALL_OK = ["ok", "ok", "ok", "ok", "ok"];

function paper(name: string, state: PaperState, overrides: Partial<PaperEntry> = {}): PaperEntry {
  return {
    name,
    state,
    stage: state === "ok" ? null : "translate",
    status: status(state === "ok" ? ALL_OK : ["ok", "ok", "ok"], { pdf: state === "ok" ? `/home/papers/${name}/out/${name}.zh.pdf` : null }),
    info: {
      title: `Title of ${name}`,
      authors: ["Ada Lovelace", "Alan Turing"],
      abstract: "An abstract.",
      primary_category: "cs.LG",
      published: "2026-11-11T12:31:55Z",
    },
    added_at: "2026-11-25T08:00:00Z",
    updated_at: "2026-11-25T09:00:00Z",
    pages: null,
    source_pages: null,
    translate_seconds: null,
    translate_cost_usd: null,
    recent_actions: [],
    ...overrides,
  };
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
  assert.equal(arxivId("hep-th/9901001v3"), "hep-th/9901001v3");
  assert.equal(arxivId("math.GT/0309136"), "math.GT/0309136");
  assert.equal(arxivId("no id here"), null);
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
  assert.equal(paperArgument("  ~/papers/my draft  "), "~/papers/my draft");
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
    launchScript("hep-th/9901001", ["--mode", "fast"], "/support dir/runs/hep-th_9901001.log", "/assets/notify.js"),
    [
      "tongtu run 'hep-th/9901001' '--mode' 'fast' > '/support dir/runs/hep-th_9901001.log' 2>&1",
      "osascript -l JavaScript '/assets/notify.js' 'hep-th/9901001' $? '/support dir/runs/hep-th_9901001.log'",
    ].join("\n"),
  );
});

test("runOptions maps every action to its tongtu run options", () => {
  assert.deepEqual(runOptions("retry", "/w"), { options: ["--workdir", "/w"], mode: null, redo: false });
  assert.deepEqual(runOptions("retrySmart", "/w"), { options: ["--workdir", "/w", "--mode", "smart"], mode: "smart", redo: false });
  assert.deepEqual(runOptions("redoFast", "/w"), {
    options: ["--workdir", "/w", "--from", "translate", "--mode", "fast"],
    mode: "fast",
    redo: true,
  });
  assert.deepEqual(runOptions("redoSmart", "/w"), {
    options: ["--workdir", "/w", "--from", "translate", "--mode", "smart"],
    mode: "smart",
    redo: true,
  });
  assert.deepEqual(runOptions("recompile", "/w"), { options: ["--workdir", "/w", "--from", "compile"], mode: null, redo: true });
});

test("isComplete needs five ok stages and a pdf", () => {
  assert.equal(isComplete(status(ALL_OK, { pdf: "/p.pdf" })), true);
  assert.equal(isComplete(status(ALL_OK)), false);
  assert.equal(isComplete(status(["ok", "ok", "ok", "ok", "compile_failed"], { pdf: "/p.pdf" })), false);
  assert.equal(isComplete(status(["ok", "ok", "ok"], { pdf: "/p.pdf" })), false);
  assert.equal(workdirName("/home/papers/2611.15404v1/"), "2611.15404v1");
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

test("matches searches title, authors and name and applies the filter", () => {
  const read = paper("2611.15404v1", "ok");
  const unread = paper("2512.02556v2", "ok", { info: { ...read.info!, primary_category: "cs.CL" } });
  const opened = ["2611.15404v1"];
  assert.equal(matches(read, "", FILTER_ALL, opened), true);
  assert.equal(matches(read, "title 2611", FILTER_ALL, opened), true);
  assert.equal(matches(read, "TURING", FILTER_ALL, opened), true);
  assert.equal(matches(read, "15404", FILTER_ALL, opened), true);
  assert.equal(matches(read, "hinton", FILTER_ALL, opened), false);
  assert.equal(matches(read, "", FILTER_UNREAD, opened), false);
  assert.equal(matches(unread, "", FILTER_UNREAD, opened), true);
  assert.equal(matches(unread, "", "cs.CL", opened), true);
  assert.equal(matches(read, "", "cs.CL", opened), false);
  assert.equal(matches(paper("local", "failed", { info: null }), "local", FILTER_ALL, opened), true);
  assert.deepEqual(categories([read, unread, paper("local", "failed", { info: null })]), ["cs.CL", "cs.LG"]);
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
  const running = paper("r", "running", { status: status(["ok", "ok", "ok"], { running: true, started_at: "2026-11-26T11:56:39Z" }) });
  assert.equal(stateLabel(running, NOW), "translate 03:21");
  assert.equal(stateLabel(paper("f", "failed", { stage: "compile" }), NOW), "compile 失败");
  assert.equal(stateLabel(paper("i", "interrupted"), NOW), "translate 中断");
  assert.equal(stateLabel(paper("o", "ok", { added_at: added }), NOW), "cs.LG · 2026-11-25");
  assert.equal(stateLabel(paper("o", "ok", { added_at: added, info: null }), NOW), "2026-11-25");
});

test("arxivUrl carries the version and is empty for local papers", () => {
  assert.equal(arxivUrl(status(ALL_OK)), "https://arxiv.org/abs/2611.15404v1");
  assert.equal(arxivUrl(status(ALL_OK, { arxiv_id: null, version: null })), null);
});

test("completedMarkdown shows only the sections whose files exist", () => {
  const entry = paper("2611.15404v1", "ok");
  const bare = completedMarkdown(entry, null, null, null);
  assert.equal(bare, "# Title of 2611.15404v1\n\nAn abstract.");
  const full = completedMarkdown(
    entry,
    "/cache dir/2611.15404v1.png",
    { notes: [{ where: "main.tex:12", problem: "macro\nundefined", resolution: "kept the original" }] },
    { terms: [{ word: "trust region", translation: "信赖域" }, { word: "a|b", translation: "甲" }] },
  );
  assert.ok(full.includes("![](file:///cache%20dir/2611.15404v1.png)"));
  assert.ok(full.includes("## 译者备注 · 1 条"));
  assert.ok(full.includes("**main.tex:12**\n\n问题：macro undefined\n\n处理：kept the original"));
  assert.ok(full.includes("## 本篇术语 · 2 条"));
  assert.ok(full.includes("| 原词 | 译法 |\n|---|---|\n| trust region | 信赖域 |\n| a\\|b | 甲 |"));
  assert.ok(completedMarkdown(paper("local", "ok", { info: null }), null, null, null).startsWith("# local"));
});

test("completedRows leaves out what is not available", () => {
  const added = new Date(2026, 10, 25, 10, 0).toISOString();
  const published = new Date(2026, 10, 11, 10, 0).toISOString();
  const entry = paper("2611.15404v1", "ok", {
    added_at: added,
    pages: 23,
    source_pages: 21,
    translate_seconds: 754.4,
    translate_cost_usd: 1.234,
    status: status(ALL_OK, { mode: "smart", pdf: "/p.pdf" }),
  });
  entry.info!.published = published;
  entry.status.stages[1].warnings = ["one", "two"];
  assert.deepEqual(completedRows(entry), [
    { title: "arXiv", text: "2611.15404v1", link: "https://arxiv.org/abs/2611.15404v1" },
    { title: "作者", text: "Ada Lovelace, Alan Turing" },
    { title: "分类与发表日期", text: "cs.LG · 2026-11-11" },
    { title: "加入日期", text: "2026-11-25" },
    { title: "页数", text: "中文 23 页，原文 21 页" },
    { title: "模式", text: "smart" },
    { title: "翻译耗时与费用", text: "12 分 34 秒 · $1.23" },
    { title: "提醒", text: "2 条" },
  ]);
  const local = paper("local", "ok", { added_at: added, info: null, status: status(ALL_OK, { arxiv_id: null, version: null }) });
  assert.deepEqual(completedRows(local), [
    { title: "加入日期", text: "2026-11-25" },
    { title: "提醒", text: "无" },
  ]);
});

test("running detail shows the stages, the running time and recent actions", () => {
  const started = new Date(2026, 10, 26, 9, 5).toISOString();
  const now = Date.parse(started) + 201_000;
  const entry = paper("2611.15404v1", "running", {
    status: status(["ok", "ok", "ok"], { running: true, started_at: started, mode: "fast" }),
    recent_actions: ["Read en.tex", "Write  parts/01.tex"],
  });
  assert.equal(
    runningMarkdown(entry, now),
    [
      "# Title of 2611.15404v1",
      "fetch ✓ → precompile ✓ → mask ✓ → **translate** → compile",
      "`translate` 已运行 03:21",
      "## 会话最近的动作",
      "- Read en.tex\n- Write parts/01.tex",
    ].join("\n\n"),
  );
  assert.ok(!runningMarkdown({ ...entry, recent_actions: [] }, now).includes("会话最近的动作"));
  assert.deepEqual(runningRows(entry, now), [
    { title: "arXiv", text: "2611.15404v1", link: "https://arxiv.org/abs/2611.15404v1" },
    { title: "模式", text: "fast" },
    { title: "开始时间", text: "2026-11-26 09:05" },
    { title: "总用时", text: "03:21" },
  ]);
});

test("unfinished detail gives the reason and the next steps", () => {
  const failed = paper("2611.15404v1", "failed", { stage: "compile", status: status(["ok", "ok", "ok", "ok", "compile_failed"], { mode: "smart" }) });
  failed.status.stages[4].message = "latexmk exited\nwith 12";
  assert.equal(
    unfinishedMarkdown(failed),
    [
      "# Title of 2611.15404v1",
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
  const interrupted = paper("2611.15404v1", "interrupted");
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
