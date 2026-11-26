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
  | { kind: "message"; text: string; hud: boolean }
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

function done(text: string): Screen {
  return { kind: "message", text, hud: true };
}

function start(paper: string, mode: string | null, shownMode: string): Screen {
  startRun(paper, mode ? ["--mode", mode] : [], null);
  return done(startMessage(paper, shownMode));
}

function failure(error: unknown): Screen {
  return { kind: "message", text: error instanceof Error ? error.message : String(error), hud: false };
}

async function prepare(paperArgumentText: string, modeArgument: string | undefined): Promise<Screen> {
  if (!installed()) return { kind: "message", text: NOT_INSTALLED, hud: false };
  const found = await findPaper(paperArgumentText);
  if (!found) return done("没有找到 arXiv 编号");
  const result = await tongtu(["status", found.paper, "--json"]);
  const status = result.code === 0 ? parseJson<PaperStatus>(result.stdout) : null;
  if (status?.running) return done(`${workdirName(status.workdir)} 正在翻译`);
  if (status?.pdf && isComplete(status)) {
    await markOpened(workdirName(status.workdir));
    await open(status.pdf);
    return done(`已有译文，打开 ${workdirName(status.workdir)}`);
  }
  const mode = runMode(modeArgument);
  const shownMode = mode ?? (await defaultMode());
  if (found.source !== "clipboard") return start(found.paper, mode, shownMode);
  const info = await tongtu(["info", found.paper, "--json"]);
  const meta = info.code === 0 ? parseJson<PaperMeta>(info.stdout) : null;
  if (!meta) return { kind: "message", text: infoFailure(found.paper, info.stderr), hud: false };
  return { kind: "confirm", paper: found.paper, meta, mode, shownMode };
}

export default function Translate(props: LaunchProps<{ arguments: { paper?: string; mode?: string } }>) {
  const [screen, setScreen] = useState<Screen>({ kind: "loading" });
  const prepared = useRef(false);
  const launched = useRef(false);

  function show(next: Screen): void {
    setScreen(next);
    if (next.kind !== "message" || !next.hud) return;
    showHUD(next.text, { clearRootSearch: true, popToRootType: PopToRootType.Immediate }).catch(() => undefined);
  }

  function launch(paper: string, mode: string | null, shownMode: string): void {
    if (launched.current) return;
    launched.current = true;
    try {
      show(start(paper, mode, shownMode));
    } catch (error) {
      show(failure(error));
    }
  }

  useEffect(() => {
    if (prepared.current) return;
    prepared.current = true;
    prepare(props.arguments.paper ?? "", props.arguments.mode).then(show, (error) => show(failure(error)));
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
          <Action title="开始翻译" icon={Icon.Play} onAction={() => launch(paper, mode, shownMode)} />
          <Action title="用 fast 翻译" icon={Icon.Play} onAction={() => launch(paper, "fast", "fast")} />
          <Action title="用 smart 翻译" icon={Icon.Play} onAction={() => launch(paper, "smart", "smart")} />
        </ActionPanel>
      }
    />
  );
}
