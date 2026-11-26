import {
  Action,
  ActionPanel,
  Clipboard,
  Icon,
  LaunchProps,
  List,
  Toast,
  getFrontmostApplication,
  showToast,
  useNavigation,
} from "@raycast/api";
import fs from "node:fs";
import { useEffect, useRef, useState } from "react";
import {
  ENTRY_ACTION_TITLES,
  EntryAction,
  INPUT_DEBOUNCE_SECONDS,
  Lookup,
  NOT_INSTALLED,
  PaperMeta,
  PaperSource,
  PaperStatus,
  START_REFRESH_DELAY_SECONDS,
  apiKeySet,
  arxivId,
  arxivIdFromUrl,
  arxivVersion,
  browserUrlScript,
  entryActions,
  entryArxivUrl,
  entryRows,
  entrySubtitle,
  entryTitle,
  expandedPath,
  isPathArgument,
  localInfoId,
  lookupFailure,
  needsAbstractZh,
  runMode,
  runTitle,
  shownAbstract,
  shownLookup,
  statusTarget,
  typedPaper,
  withAbstractZh,
} from "./core";
import { PaperDetailView, RefreshAction, feedback, openPdf, run, stop } from "./detail";
import { USER_HOME, defaultMode, installed, osascript, parseJson, readConfig, startRun, tongtu } from "./tongtu";

const BROWSER_TIMEOUT_MILLISECONDS = 2000;

feedback.closesPanel = false;

type Prefill = { text: string; source: PaperSource };

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

async function findPrefill(argument: string): Promise<Prefill | null> {
  if (argument.trim() !== "") return { text: argument.trim(), source: "argument" };
  const fromBrowser = await browserPaper();
  if (fromBrowser) return { text: fromBrowser, source: "browser" };
  const fromClipboard = await clipboardPaper();
  return fromClipboard ? { text: fromClipboard, source: "clipboard" } : null;
}

async function info(paper: string): Promise<{ meta: PaperMeta | null; infoError: string }> {
  const result = await tongtu(["info", paper, "--json"]);
  return result.code === 0 ? { meta: parseJson<PaperMeta>(result.stdout), infoError: "" } : { meta: null, infoError: result.stderr };
}

function isDirectory(directory: string): boolean {
  try {
    return fs.statSync(directory).isDirectory();
  } catch {
    return false;
  }
}

async function lookUp(paper: string): Promise<Lookup> {
  if (isPathArgument(paper) && !isDirectory(expandedPath(paper, USER_HOME))) {
    return { paper, status: null, meta: null, infoError: "", directoryMissing: true };
  }
  const statusRequest = tongtu(["status", paper, "--json"]);
  const infoRequest = isPathArgument(paper) ? null : info(paper);
  const [statusResult, remote] = await Promise.all([statusRequest, infoRequest]);
  const status = statusResult.code === 0 ? parseJson<PaperStatus>(statusResult.stdout) : null;
  const local = localInfoId(paper, status, remote?.meta ?? null);
  const found = local ? await info(local) : (remote ?? { meta: null, infoError: "" });
  return { paper, status, ...found, directoryMissing: false };
}

export default function Translate(props: LaunchProps<{ arguments: { paper?: string; mode?: string } }>) {
  const { push } = useNavigation();
  const missing = !installed();
  const [text, setText] = useState("");
  const [source, setSource] = useState<PaperSource | null>(null);
  const [prefilling, setPrefilling] = useState(!missing);
  const [paper, setPaper] = useState<string | null>(null);
  const [lookup, setLookup] = useState<Lookup | null>(null);
  const [shownMode, setShownMode] = useState(runMode(props.arguments.mode) ?? "");
  const edited = useRef(false);
  const latest = useRef(0);
  const shownPaper = useRef<string | null>(null);
  const launching = useRef(false);
  const translating = useRef(new Set<string>());

  useEffect(() => {
    if (missing) return;
    if (shownMode === "") defaultMode().then(setShownMode);
    findPrefill(props.arguments.paper ?? "")
      .then((prefill) => {
        if (!prefill || edited.current) return;
        setText(prefill.text);
        setSource(prefill.source);
        setPaper(typedPaper(prefill.text));
      })
      .finally(() => setPrefilling(false));
  }, []);

  useEffect(() => {
    if (!edited.current) return;
    const debounce = setTimeout(() => setPaper(typedPaper(text)), INPUT_DEBOUNCE_SECONDS * 1000);
    return () => clearTimeout(debounce);
  }, [text]);

  shownPaper.current = paper;

  async function load(): Promise<void> {
    const request = ++latest.current;
    const wanted = shownPaper.current;
    if (wanted === null) {
      setLookup(null);
      return;
    }
    const found = await lookUp(wanted);
    if (request !== latest.current) return;
    setLookup(found);
    if (found.meta && needsAbstractZh(found.meta)) translateAbstract(wanted, arxivVersion(found.meta) ?? wanted);
  }

  async function translateAbstract(wanted: string, id: string): Promise<void> {
    if (translating.current.has(id)) return;
    translating.current.add(id);
    const config = await readConfig();
    if (!config || !apiKeySet(config)) {
      translating.current.delete(id);
      return;
    }
    const result = await tongtu(["info", id, "--json", "--zh"]);
    translating.current.delete(id);
    const translated = result.code === 0 ? parseJson<PaperMeta>(result.stdout) : null;
    setLookup((shown) => (shown && shown.paper === wanted ? { ...shown, meta: withAbstractZh(shown.meta, translated) } : shown));
  }

  useEffect(() => {
    load();
  }, [paper]);

  function changeText(next: string): void {
    if (next === text) return;
    edited.current = true;
    setText(next);
    setSource(null);
  }

  async function start(target: string, mode: string | null): Promise<void> {
    if (launching.current) return;
    launching.current = true;
    try {
      startRun(target, mode ? ["--mode", mode] : [], null);
    } catch (error) {
      launching.current = false;
      await showToast({
        style: Toast.Style.Failure,
        title: `没有启动 ${target}`,
        message: error instanceof Error ? error.message : String(error),
      });
      return;
    }
    push(<PaperDetailView paper={target} starting onClose={load} />);
    setTimeout(() => {
      launching.current = false;
      load();
    }, START_REFRESH_DELAY_SECONDS * 1000);
  }

  function entryAction(action: EntryAction, found: Lookup) {
    const local = found.status ? statusTarget(found.status) : null;
    const url = entryArxivUrl(found);
    if (action === "start") {
      return <Action key={action} title={ENTRY_ACTION_TITLES.start} icon={Icon.Play} onAction={() => start(found.paper, runMode(props.arguments.mode))} />;
    }
    if (action === "startFast") return <Action key={action} title={ENTRY_ACTION_TITLES.startFast} icon={Icon.Play} onAction={() => start(found.paper, "fast")} />;
    if (action === "startSmart") return <Action key={action} title={ENTRY_ACTION_TITLES.startSmart} icon={Icon.Play} onAction={() => start(found.paper, "smart")} />;
    if (action === "arxiv") return url && <Action.OpenInBrowser key={action} title={ENTRY_ACTION_TITLES.arxiv} url={url} />;
    if (!local) return null;
    if (action === "detail") {
      return (
        <Action.Push
          key={action}
          title={ENTRY_ACTION_TITLES.detail}
          icon={Icon.Sidebar}
          target={<PaperDetailView paper={local.name} workdir={local.workdir} onClose={load} />}
        />
      );
    }
    if (action === "openPdf") return <Action key={action} title={ENTRY_ACTION_TITLES.openPdf} icon={Icon.Document} onAction={() => openPdf(local)} />;
    if (action === "stop") {
      return <Action key={action} title={ENTRY_ACTION_TITLES.stop} icon={Icon.Stop} style={Action.Style.Destructive} onAction={() => stop(local, load)} />;
    }
    return <Action key={action} title={runTitle(action, local.mode)} icon={Icon.Play} onAction={() => run(local, action, load)} />;
  }

  const current = shownLookup(lookup, text);
  const failure = current ? lookupFailure(current) : null;
  const waiting = prefilling || typedPaper(text) !== paper || (paper !== null && current === null);
  const refreshOnly = (
    <ActionPanel>
      <RefreshAction reload={load} />
    </ActionPanel>
  );
  return (
    <List
      isLoading={waiting}
      isShowingDetail={current !== null && failure === null}
      filtering={false}
      searchText={text}
      onSearchTextChange={changeText}
      searchBarPlaceholder="输入 arXiv 编号或链接"
    >
      {missing && <List.EmptyView title={NOT_INSTALLED} />}
      {!missing && paper === null && !waiting && <List.EmptyView title="没有找到 arXiv 编号" description="在上面输入编号或链接" />}
      {!missing && failure !== null && <List.EmptyView title={failure} actions={refreshOnly} />}
      {!missing && current !== null && failure === null && (
        <List.Item
          title={entryTitle(current)}
          subtitle={entrySubtitle(current, source)}
          detail={
            <List.Item.Detail
              markdown={shownAbstract(current.meta)}
              metadata={
                <List.Item.Detail.Metadata>
                  {entryRows(current, shownMode).map((row) => (
                    <List.Item.Detail.Metadata.Label key={row.title} title={row.title} text={row.text} />
                  ))}
                </List.Item.Detail.Metadata>
              }
            />
          }
          actions={
            <ActionPanel>
              {entryActions(current).map((action) => entryAction(action, current))}
              <RefreshAction reload={load} />
            </ActionPanel>
          }
        />
      )}
    </List>
  );
}
