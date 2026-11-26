import assert from "node:assert/strict";
import test from "node:test";
import notify from "../assets/notify.js";

const { failureReason, notification, parseStatusOutput, statusCommand, toolPath } = notify;
const STAGE_NAMES = ["fetch", "precompile", "mask", "translate", "compile"];

function status(statuses: string[]) {
  return {
    workdir: "/home/papers/2611.15404v1",
    meta: null,
    stages: STAGE_NAMES.map((name, index) => ({ name, status: statuses[index] ?? "absent" })),
  };
}

const ALL_OK = ["ok", "ok", "ok", "ok", "ok"];

test("a stopped run sends no notification", () => {
  assert.equal(notification("2611.15404", 143, status(["ok", "ok", "ok"]), "A Title", ""), null);
  assert.equal(notification("2611.15404", 143, null, "", ""), null);
});

test("a run without a working directory reports the reason from the log", () => {
  assert.deepEqual(notification("9999.99999", 1, null, "", "arXiv API has no entry for 9999.99999; check the id and version\n"), {
    title: "翻译失败",
    body: "9999.99999：arXiv 上没有这篇论文",
  });
  assert.deepEqual(notification("2611.15404", 1, null, "", "arXiv API request for 2611.15404 failed: TimeoutError: timed out\n"), {
    title: "翻译失败",
    body: "2611.15404：连不上 arXiv",
  });
  assert.deepEqual(notification("draft", 2, null, "", "Usage: tongtu run\n\n  Invalid value: no such directory  \n\n"), {
    title: "翻译失败",
    body: "draft：Invalid value: no such directory",
  });
  assert.equal(failureReason(""), "");
});

test("fetch states without usable source are reported as untranslatable", () => {
  assert.deepEqual(notification("2611.15404", 1, status(["pdf_only"]), "A Title", ""), {
    title: "无法翻译",
    body: "A Title：这个版本没有 TeX 源码",
  });
  assert.deepEqual(notification("2611.15404", 1, status(["empty"]), "", ""), {
    title: "无法翻译",
    body: "2611.15404v1：源码包里没有 TeX 文件",
  });
  assert.deepEqual(notification("draft", 1, status(["source_missing"]), "", ""), {
    title: "无法翻译",
    body: "2611.15404v1：本地源码目录不存在",
  });
});

test("five ok stages report completion", () => {
  assert.deepEqual(notification("2611.15404", 0, status(ALL_OK), "A Title", ""), { title: "翻译完成", body: "A Title" });
  assert.deepEqual(notification("2611.15404", 0, status(ALL_OK), "", ""), { title: "翻译完成", body: "2611.15404v1" });
});

test("other outcomes name the first stage that is not ok", () => {
  assert.deepEqual(notification("2611.15404", 1, status(["ok", "ok", "ok", "validation_failed"]), "A Title", ""), {
    title: "翻译失败",
    body: "A Title 在 translate 阶段失败",
  });
  assert.deepEqual(notification("2611.15404", 1, status(["ok", "ok", "ok", "ok"]), "", ""), {
    title: "翻译失败",
    body: "2611.15404v1 在 compile 阶段失败",
  });
  assert.deepEqual(notification("2611.15404", 1, status(["download_failed"]), "", ""), {
    title: "翻译失败",
    body: "2611.15404v1 在 fetch 阶段失败",
  });
});

test("parseStatusOutput reads the JSON only when tongtu status exits 0", () => {
  assert.deepEqual(parseStatusOutput('{\n  "workdir": "/w"\n}\nexit=0\n'), { workdir: "/w" });
  assert.equal(parseStatusOutput("exit=1\n"), null);
  assert.equal(parseStatusOutput('{"workdir": "/w"}\nexit=2\n'), null);
  assert.equal(parseStatusOutput("not json\nexit=0\n"), null);
  assert.equal(parseStatusOutput(""), null);
});

test("statusCommand sets the plugin PATH and quotes the paper", () => {
  assert.equal(toolPath("/Users/me"), "/Users/me/.local/bin:/Users/me/.cargo/bin:/opt/homebrew/bin:/Library/TeX/texbin:/usr/bin:/bin");
  assert.equal(
    statusCommand("it's", "/Users/me"),
    "PATH='/Users/me/.local/bin:/Users/me/.cargo/bin:/opt/homebrew/bin:/Library/TeX/texbin:/usr/bin:/bin' tongtu status 'it'\\''s' --json 2>/dev/null; echo \"exit=$?\"",
  );
});

test("statusCommand passes the working directory when it is given", () => {
  assert.equal(
    statusCommand("draft", "/Users/me", "/home/my papers/draft"),
    "PATH='/Users/me/.local/bin:/Users/me/.cargo/bin:/opt/homebrew/bin:/Library/TeX/texbin:/usr/bin:/bin' tongtu status 'draft' --workdir '/home/my papers/draft' --json 2>/dev/null; echo \"exit=$?\"",
  );
  assert.equal(statusCommand("draft", "/Users/me", undefined), statusCommand("draft", "/Users/me"));
});
