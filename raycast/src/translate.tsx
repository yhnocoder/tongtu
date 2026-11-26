import {
  Action,
  ActionPanel,
  Clipboard,
  Detail,
  Icon,
  LaunchProps,
  PopToRootType,
  getFrontmostApplication,
  open,
  showHUD,
} from "@raycast/api";
import { useEffect, useRef, useState } from "react";
import {
  NOT_INSTALLED,
  PaperMeta,
  PaperSource,
  PaperStatus,
  arxivId,
  arxivIdFromUrl,
  browserUrlScript,
  confirmRows,
  infoFailure,
  isComplete,
  paperArgument,
  runMode,
  startMessage,
  workdirName,
} from "./core";
import { defaultMode, installed, markOpened, osascript, parseJson, startRun, tongtu } from "./tongtu";

const BROWSER_TIMEOUT_MILLISECONDS = 2000;

type Found = { paper: string; source: PaperSource };
type Screen =
  | { kind: "loading" }
  | { kind: "message"; text: string }
  | { kind: "confirm"; paper: string; meta: PaperMeta; mode: string | null; shownMode: string };

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

async function findPaper(argument: string): Promise<Found | null> {
  if (argument.trim() !== "") return { paper: paperArgument(argument), source: "argument" };
  const fromBrowser = await browserPaper();
  if (fromBrowser) return { paper: fromBrowser, source: "browser" };
  const fromClipboard = await clipboardPaper();
  return fromClipboard ? { paper: fromClipboard, source: "clipboard" } : null;
}

async function finish(text: string): Promise<void> {
  await showHUD(text, { clearRootSearch: true, popToRootType: PopToRootType.Immediate });
}

async function start(paper: string, mode: string | null, shownMode: string): Promise<void> {
  startRun(paper, mode ? ["--mode", mode] : [], null);
  await finish(startMessage(paper, shownMode));
}

async function prepare(paperArgumentText: string, modeArgument: string | undefined): Promise<Screen | null> {
  if (!installed()) return { kind: "message", text: NOT_INSTALLED };
  const found = await findPaper(paperArgumentText);
  if (!found) {
    await finish("没有找到 arXiv 编号");
    return null;
  }
  const result = await tongtu(["status", found.paper, "--json"]);
  const status = result.code === 0 ? parseJson<PaperStatus>(result.stdout) : null;
  if (status?.running) {
    await finish(`${workdirName(status.workdir)} 正在翻译`);
    return null;
  }
  if (status?.pdf && isComplete(status)) {
    await markOpened(workdirName(status.workdir));
    await open(status.pdf);
    await finish(`已有译文，打开 ${workdirName(status.workdir)}`);
    return null;
  }
  const mode = runMode(modeArgument);
  const shownMode = mode ?? (await defaultMode());
  if (found.source !== "clipboard") {
    await start(found.paper, mode, shownMode);
    return null;
  }
  const info = await tongtu(["info", found.paper, "--json"]);
  const meta = info.code === 0 ? parseJson<PaperMeta>(info.stdout) : null;
  if (!meta) return { kind: "message", text: infoFailure(found.paper, info.stderr) };
  return { kind: "confirm", paper: found.paper, meta, mode, shownMode };
}

export default function Translate(props: LaunchProps<{ arguments: { paper?: string; mode?: string } }>) {
  const [screen, setScreen] = useState<Screen>({ kind: "loading" });
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    prepare(props.arguments.paper ?? "", props.arguments.mode).then((next) => next && setScreen(next));
  }, []);
  if (screen.kind === "loading") return <Detail isLoading markdown="" />;
  if (screen.kind === "message") return <Detail markdown={screen.text} />;
  const { paper, meta, mode, shownMode } = screen;
  return (
    <Detail
      markdown={`# ${meta.title}`}
      metadata={
        <Detail.Metadata>
          {confirmRows(meta, shownMode).map((row) => (
            <Detail.Metadata.Label key={row.title} title={row.title} text={row.text} />
          ))}
        </Detail.Metadata>
      }
      actions={
        <ActionPanel>
          <Action title="开始翻译" icon={Icon.Play} onAction={() => start(paper, mode, shownMode)} />
          <Action title="用 fast 翻译" icon={Icon.Play} onAction={() => start(paper, "fast", "fast")} />
          <Action title="用 smart 翻译" icon={Icon.Play} onAction={() => start(paper, "smart", "smart")} />
        </ActionPanel>
      }
    />
  );
}
