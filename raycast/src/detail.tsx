import {
  Action,
  ActionPanel,
  Alert,
  Detail,
  Icon,
  Toast,
  confirmAlert,
  open,
  showHUD,
  showToast,
  useNavigation,
} from "@raycast/api";
import fs from "node:fs";
import path from "node:path";
import { useEffect, useRef, useState } from "react";
import {
  DETAIL_REFRESH_SECONDS,
  PaperDetail,
  PaperMeta,
  PaperStatus,
  PaperSummary,
  PaperTarget,
  RUN_TITLES,
  Row,
  RunAction,
  START_REFRESH_DELAY_SECONDS,
  arxivUrl,
  arxivVersion,
  completedMarkdown,
  completedRows,
  initialOpened,
  needsAbstractZh,
  refreshSeconds,
  removableWorkdir,
  runMessage,
  runOptions,
  runningMarkdown,
  runningRows,
  startingMarkdown,
  statusTarget,
  unfinishedMarkdown,
  unfinishedRows,
  withAbstractZh,
  withOpened,
  withoutOpened,
  workdirName,
} from "./core";
import {
  installed,
  loadOpened,
  parseJson,
  readBase64,
  readJson,
  removePaperFiles,
  saveOpened,
  startRun,
  thumbnail,
  tongtu,
} from "./tongtu";

const CLOCK_MILLISECONDS = 1000;

export const feedback = { closesPanel: true };

export type Shared = { papers: PaperSummary[] | null; missing: boolean; error: string; opened: string[] | null };

let shared: Shared = { papers: null, missing: false, error: "", opened: null };
let refreshing: Promise<void> | null = null;
let refreshAgain = false;
let timer: ReturnType<typeof setTimeout> | null = null;
const listeners = new Set<() => void>();

function publish(next: Partial<Shared>): void {
  shared = { ...shared, ...next };
  for (const listener of listeners) listener();
}

async function loadListing(): Promise<void> {
  if (!installed()) {
    publish({ missing: true });
    return;
  }
  const result = await tongtu(["list", "--json"]);
  const listing = result.code === 0 ? parseJson<{ papers: PaperSummary[] }>(result.stdout) : null;
  if (!listing) {
    publish({ missing: false, error: result.stderr.trim() || "tongtu list --json 没有给出结果" });
    return;
  }
  let opened = shared.opened;
  if (opened === null) {
    const stored = await loadOpened();
    opened = stored ?? initialOpened(listing.papers);
    if (!stored) await saveOpened(opened);
  }
  publish({ papers: listing.papers, missing: false, error: "", opened });
}

function clearTimer(): void {
  if (timer !== null) clearTimeout(timer);
  timer = null;
}

function schedule(): void {
  clearTimer();
  if (listeners.size === 0) return;
  timer = setTimeout(refresh, refreshSeconds(shared.papers ?? []) * 1000);
}

async function refreshUntilSettled(): Promise<void> {
  try {
    do {
      refreshAgain = false;
      await loadListing();
    } while (refreshAgain);
  } catch (error) {
    publish({ error: error instanceof Error ? error.message : String(error) });
  } finally {
    refreshing = null;
    schedule();
  }
}

export function refresh(): Promise<void> {
  if (refreshing) refreshAgain = true;
  else refreshing = Promise.resolve().then(refreshUntilSettled);
  return refreshing;
}

export function useShared(): Shared {
  const [, setVersion] = useState(0);
  useEffect(() => {
    const listener = () => setVersion((version) => version + 1);
    listeners.add(listener);
    refresh();
    return () => {
      listeners.delete(listener);
      if (listeners.size === 0) clearTimer();
    };
  }, []);
  return shared;
}

export function useNow(active: boolean): number {
  const [, setTicks] = useState(0);
  useEffect(() => {
    if (!active) return;
    const clock = setInterval(() => setTicks((ticks) => ticks + 1), CLOCK_MILLISECONDS);
    return () => clearInterval(clock);
  }, [active]);
  return Date.now();
}

export function shownThumbnail(name: string, pdf: string | null): string | null {
  return thumbnail(name, pdf, () => publish({}));
}

async function setOpened(change: (opened: string[]) => string[]): Promise<void> {
  const current = shared.opened ?? (await loadOpened());
  if (current === null) return;
  const opened = change(current);
  publish({ opened });
  await saveOpened(opened);
}

async function fail(title: string, message: string): Promise<void> {
  await showToast({ style: Toast.Style.Failure, title, message });
}

async function announce(text: string): Promise<void> {
  if (feedback.closesPanel) await showHUD(text);
  else await showToast({ style: Toast.Style.Success, title: text });
}

export async function openPdf(paper: PaperTarget): Promise<void> {
  if (!paper.pdf) return;
  await setOpened((opened) => withOpened(opened, paper.name));
  await open(paper.pdf);
}

export async function run(paper: PaperTarget, action: RunAction, reload: () => Promise<void>): Promise<void> {
  const { options, redo } = runOptions(action, paper.workdir);
  if (redo) await setOpened((opened) => withoutOpened(opened, paper.name));
  try {
    startRun(paper.name, options, paper.workdir);
  } catch (error) {
    await fail(`没有启动 ${paper.name}`, error instanceof Error ? error.message : String(error));
    return;
  }
  await reload();
  setTimeout(reload, START_REFRESH_DELAY_SECONDS * 1000);
  await announce(runMessage(action, paper.name));
}

async function stopRun(paper: PaperTarget): Promise<boolean> {
  const result = await tongtu(["stop", paper.name, "--workdir", paper.workdir]);
  if (result.code !== 0) await fail(`没有停止 ${paper.name}`, result.stderr.trim());
  return result.code === 0;
}

export async function stop(paper: PaperTarget, reload: () => Promise<void>): Promise<void> {
  const stopped = await stopRun(paper);
  await reload();
  if (stopped) await announce(`已停止 ${paper.name}`);
}

async function remove(paper: PaperTarget, removed: () => void): Promise<void> {
  const confirmed = await confirmAlert({
    title: `删除 ${paper.name}？`,
    message: `删除工作目录 ${paper.workdir}、缩略图缓存和已读记录。`,
    primaryAction: { title: "删除", style: Alert.ActionStyle.Destructive },
  });
  if (!confirmed) return;
  if (!removableWorkdir(paper.name, paper.workdir)) {
    await fail(`没有删除 ${paper.name}`, `工作目录 ${paper.workdir} 与目录名不符`);
    return;
  }
  if (!(await stopRun(paper))) return;
  try {
    removePaperFiles(paper.name, paper.workdir);
  } catch (error) {
    await fail(`没有删除 ${paper.name}`, error instanceof Error ? error.message : String(error));
    await refresh();
    return;
  }
  await setOpened((opened) => withoutOpened(opened, paper.name));
  await refresh();
  removed();
  await showToast({ style: Toast.Style.Success, title: `已删除 ${paper.name}` });
}

type Reload = () => Promise<void>;

export function RefreshAction({ reload }: { reload: Reload }) {
  return <Action title="刷新" icon={Icon.ArrowClockwise} shortcut={{ modifiers: ["cmd"], key: "r" }} onAction={reload} />;
}

function RunItem({ paper, action, icon, reload }: { paper: PaperTarget; action: RunAction; icon: Icon; reload: Reload }) {
  return <Action title={RUN_TITLES[action]} icon={icon} onAction={() => run(paper, action, reload)} />;
}

type ActionsProps = { paper: PaperTarget; reload: Reload; toggleView?: () => void; removed?: () => void };

export function PaperActions({ paper, reload, toggleView, removed }: ActionsProps) {
  const original = path.join(paper.workdir, "build/precompile.pdf");
  const arxiv = arxivUrl(paper);
  const details = toggleView && (
    <Action.Push title="查看详情" icon={Icon.Sidebar} target={<PaperDetailView paper={paper.name} workdir={paper.workdir} />} />
  );
  const finder = <Action.ShowInFinder title="在访达中显示" path={paper.workdir} />;
  return (
    <ActionPanel>
      {paper.state === "ok" && (
        <>
          {details}
          <Action title="打开中文 PDF" icon={Icon.Document} onAction={() => openPdf(paper)} />
          {fs.existsSync(original) && <Action title="打开英文原文" icon={Icon.Document} onAction={() => open(original)} />}
          {arxiv && <Action.OpenInBrowser title="打开 arXiv 页面" url={arxiv} />}
          {finder}
          <ActionPanel.Section title="重做">
            <RunItem paper={paper} action="redoFast" icon={Icon.ArrowClockwise} reload={reload} />
            <RunItem paper={paper} action="redoSmart" icon={Icon.ArrowClockwise} reload={reload} />
            <RunItem paper={paper} action="recompile" icon={Icon.Hammer} reload={reload} />
          </ActionPanel.Section>
        </>
      )}
      {paper.state === "running" && (
        <>
          {details}
          <Action title="停止" icon={Icon.Stop} style={Action.Style.Destructive} onAction={() => stop(paper, reload)} />
          {finder}
        </>
      )}
      {(paper.state === "failed" || paper.state === "interrupted") && (
        <>
          {details}
          <RunItem paper={paper} action="retry" icon={Icon.Play} reload={reload} />
          <RunItem paper={paper} action="retrySmart" icon={Icon.Play} reload={reload} />
          <Action.ShowInFinder title="查看日志" icon={Icon.Text} path={path.join(paper.workdir, "logs")} />
          {finder}
        </>
      )}
      <ActionPanel.Section>
        <RefreshAction reload={reload} />
        {toggleView && (
          <Action
            title="切换列表和网格"
            icon={Icon.AppWindowGrid2x2}
            shortcut={{ modifiers: ["cmd", "shift"], key: "v" }}
            onAction={toggleView}
          />
        )}
        <Action
          title="删除论文"
          icon={Icon.Trash}
          style={Action.Style.Destructive}
          shortcut={{ modifiers: ["ctrl"], key: "x" }}
          onAction={() => remove(paper, removed ?? (() => undefined))}
        />
      </ActionPanel.Section>
    </ActionPanel>
  );
}

function detailMarkdown(detail: PaperDetail): string {
  if (detail.status.state === "running") return runningMarkdown(detail);
  if (detail.status.state !== "ok") return unfinishedMarkdown(detail);
  return completedMarkdown(detail, readBase64(shownThumbnail(detail.name, detail.status.pdf)));
}

function detailRows(detail: PaperDetail, now: number): Row[] {
  if (detail.status.state === "running") return runningRows(detail, now);
  return detail.status.state === "ok" ? completedRows(detail) : unfinishedRows(detail);
}

type DetailProps = { paper: string; workdir?: string; starting?: boolean };

export function PaperDetailView({ paper, workdir, starting }: DetailProps) {
  const { papers } = useShared();
  const { pop } = useNavigation();
  const knownWorkdir = useRef(workdir);
  const [status, setStatus] = useState<PaperStatus | null>(null);
  const [failure, setFailure] = useState("");
  const [settling, setSettling] = useState(starting === true);
  const [translated, setTranslated] = useState<PaperMeta | null>(null);
  const askedAbstractZh = useRef(false);
  const running = status?.state === "running";
  const now = useNow(running);

  async function loadStatus(): Promise<void> {
    const known = knownWorkdir.current;
    const location = known ? [workdirName(known), "--workdir", known] : [paper];
    const result = await tongtu(["status", ...location, "--json"]);
    const next = result.code === 0 ? parseJson<PaperStatus>(result.stdout) : null;
    if (next) {
      knownWorkdir.current = next.workdir;
      setStatus(next);
    }
    setFailure(next ? "" : result.stderr.trim() || "tongtu status --json 没有给出结果");
  }

  async function reload(): Promise<void> {
    await Promise.all([loadStatus(), refresh()]);
  }

  useEffect(() => {
    loadStatus();
  }, []);

  useEffect(() => {
    if (!status || status.state !== "ok" || askedAbstractZh.current) return;
    const id = arxivVersion(status);
    if (!id || !needsAbstractZh(readJson<PaperMeta>(status.meta))) return;
    askedAbstractZh.current = true;
    tongtu(["info", id, "--json", "--zh"]).then((result) => {
      if (result.code === 0) setTranslated(parseJson<PaperMeta>(result.stdout));
    });
  }, [status]);

  useEffect(() => {
    if (!settling) return;
    const settle = setTimeout(() => reload().then(() => setSettling(false)), START_REFRESH_DELAY_SECONDS * 1000);
    return () => clearTimeout(settle);
  }, [settling]);

  useEffect(() => {
    if (!running) return;
    let busy = false;
    const poll = setInterval(async () => {
      if (busy) return;
      busy = true;
      await loadStatus();
      busy = false;
    }, DETAIL_REFRESH_SECONDS * 1000);
    return () => {
      clearInterval(poll);
      refresh();
    };
  }, [running]);

  const refreshOnly = (
    <ActionPanel>
      <RefreshAction reload={reload} />
    </ActionPanel>
  );
  if (settling && !running) return <Detail isLoading markdown={startingMarkdown(paper)} actions={refreshOnly} />;
  if (!status) return <Detail isLoading={failure === ""} markdown={failure} actions={refreshOnly} />;
  const target = statusTarget(status);
  const detail = {
    name: target.name,
    addedAt: papers?.find((other) => other.name === target.name)?.added_at ?? null,
    status,
    meta: withAbstractZh(readJson<PaperMeta>(status.meta), translated),
  };
  return (
    <Detail
      markdown={detailMarkdown(detail)}
      actions={<PaperActions paper={target} reload={reload} removed={pop} />}
      metadata={
        <Detail.Metadata>
          {detailRows(detail, now).map((row) =>
            row.link ? (
              <Detail.Metadata.Link key={row.title} title={row.title} text={row.text} target={row.link} />
            ) : (
              <Detail.Metadata.Label key={row.title} title={row.title} text={row.text} />
            ),
          )}
        </Detail.Metadata>
      }
    />
  );
}
