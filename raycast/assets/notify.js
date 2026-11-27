const STAGES = ["fetch", "reconstruct", "precompile", "mask", "translate", "compile"];
const STOPPED_EXIT_CODE = 143;
const TOOL_DIRECTORIES = ["~/.local/bin", "~/.cargo/bin", "/opt/homebrew/bin", "/Library/TeX/texbin", "/usr/bin", "/bin"];
const UNTRANSLATABLE = {
  fetch: {
    empty: "源码包里没有 TeX 文件",
    source_missing: "本地源码目录或 PDF 文件不存在",
  },
  reconstruct: {
    no_text_layer: "PDF 没有文字层，可能是扫描版",
  },
};
const FAILURES = {
  reconstruct: {
    reconstruct_failed: "从 PDF 重建的源码没有通过核对",
  },
};

function shellQuote(text) {
  return "'" + text.replace(/'/g, "'\\''") + "'";
}

function toolPath(home) {
  return TOOL_DIRECTORIES.map((directory) => directory.replace(/^~/, home)).join(":");
}

function statusCommand(paper, home, workdir) {
  const location = workdir ? " --workdir " + shellQuote(workdir) : "";
  return "PATH=" + shellQuote(toolPath(home)) + " tongtu status " + shellQuote(paper) + location + ' --json 2>/dev/null; echo "exit=$?"';
}

function parseStatusOutput(output) {
  const match = /^([\s\S]*?)\n?exit=(\d+)\s*$/.exec(output);
  if (!match || match[2] !== "0") return null;
  try {
    return JSON.parse(match[1]);
  } catch (error) {
    return null;
  }
}

function failureReason(log) {
  if (log.includes("has no entry for")) return "arXiv 上没有这篇论文";
  if (log.includes("API request for")) return "连不上 arXiv";
  const lines = log.split("\n").filter((line) => line.trim() !== "");
  return lines.length > 0 ? lines[lines.length - 1].trim() : "";
}

function workdirName(workdir) {
  return workdir.replace(/\/+$/, "").split("/").pop();
}

function notification(paper, runExitCode, status, title, log) {
  if (runExitCode === STOPPED_EXIT_CODE) return null;
  if (!status) return { title: "翻译失败", body: paper + "：" + failureReason(log) };
  const name = title || workdirName(status.workdir) || paper;
  const stageStatus = (stage) => (status.stages.find((state) => state.name === stage) || { status: "absent" }).status;
  const unfinished = STAGES.find((stage) => stageStatus(stage) !== "ok");
  if (!unfinished) return { title: "翻译完成", body: name };
  const untranslatable = (UNTRANSLATABLE[unfinished] || {})[stageStatus(unfinished)];
  if (untranslatable) return { title: "无法翻译", body: name + "：" + untranslatable };
  const failure = (FAILURES[unfinished] || {})[stageStatus(unfinished)];
  if (failure) return { title: "翻译失败", body: name + " 在 " + unfinished + " 阶段失败：" + failure };
  return { title: "翻译失败", body: name + " 在 " + unfinished + " 阶段失败" };
}

function readText(path) {
  const content = $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null);
  return content.isNil() ? "" : content.js;
}

function readTitle(path) {
  if (!path) return "";
  try {
    return JSON.parse(readText(path)).title || "";
  } catch (error) {
    return "";
  }
}

function run(argv) {
  const app = Application.currentApplication();
  app.includeStandardAdditions = true;
  const paper = argv[0];
  const home = $.NSHomeDirectory().js;
  let output = "";
  try {
    output = app.doShellScript(statusCommand(paper, home, argv[3]), { alteringLineEndings: false });
  } catch (error) {
    output = "";
  }
  const status = parseStatusOutput(output);
  const message = notification(paper, Number(argv[1]), status, status ? readTitle(status.meta) : "", readText(argv[2]));
  if (message) app.displayNotification(message.body, { withTitle: message.title });
}

if (typeof module !== "undefined") {
  module.exports = { failureReason, notification, parseStatusOutput, shellQuote, statusCommand, toolPath };
}
