import { Clipboard, LaunchProps, getFrontmostApplication, open, showHUD } from "@raycast/api";
import {
  NOT_INSTALLED,
  PaperStatus,
  arxivId,
  arxivIdFromUrl,
  browserUrlScript,
  isComplete,
  paperArgument,
  runMode,
  workdirName,
} from "./core";
import { installed, markOpened, osascript, parseJson, startRun, tongtu } from "./tongtu";

const BROWSER_TIMEOUT_MILLISECONDS = 2000;

async function browserPaper(): Promise<string | null> {
  try {
    const application = await getFrontmostApplication();
    const script = browserUrlScript(application.name);
    if (!script) return null;
    const result = await osascript(script, BROWSER_TIMEOUT_MILLISECONDS);
    return result.code === 0 ? arxivIdFromUrl(result.stdout) : null;
  } catch {
    return null;
  }
}

async function clipboardPaper(): Promise<string | null> {
  try {
    return arxivId((await Clipboard.readText()) ?? "");
  } catch {
    return null;
  }
}

async function findPaper(argument: string): Promise<string | null> {
  if (argument.trim() !== "") return paperArgument(argument);
  return (await browserPaper()) ?? (await clipboardPaper());
}

export default async function Command(props: LaunchProps<{ arguments: { paper?: string; mode?: string } }>) {
  if (!installed()) {
    await showHUD(NOT_INSTALLED);
    return;
  }
  const paper = await findPaper(props.arguments.paper ?? "");
  if (!paper) {
    await showHUD("没有找到 arXiv 编号");
    return;
  }
  const result = await tongtu(["status", paper, "--json"]);
  const status = result.code === 0 ? parseJson<PaperStatus>(result.stdout) : null;
  if (status?.running) {
    await showHUD(`${workdirName(status.workdir)} 正在翻译`);
    return;
  }
  if (status?.pdf && isComplete(status)) {
    await markOpened(workdirName(status.workdir));
    await open(status.pdf);
    await showHUD(`已有译文，打开 ${workdirName(status.workdir)}`);
    return;
  }
  const mode = runMode(props.arguments.mode);
  const shown = await startRun(paper, mode ? ["--mode", mode] : [], mode);
  await showHUD(`开始翻译 ${paper}（${shown}）`);
}
