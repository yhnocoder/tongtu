import {
  Action,
  ActionPanel,
  Alert,
  Color,
  Detail,
  Grid,
  Icon,
  List,
  LocalStorage,
  Toast,
  confirmAlert,
  open,
  showHUD,
  showToast,
} from "@raycast/api";
import fs from "node:fs";
import path from "node:path";
import { useEffect, useState } from "react";
import {
  DEFAULT_RECENT_DAYS,
  DETAIL_REFRESH_SECONDS,
  FILTER_ALL,
  FILTER_UNREAD,
  NOT_INSTALLED,
  PaperDetail,
  PaperMeta,
  PaperStatus,
  PaperSummary,
  RUN_TITLES,
  Row,
  RunAction,
  START_REFRESH_DELAY_SECONDS,
  arxivUrl,
  categories,
  completedMarkdown,
  completedRows,
  initialOpened,
  isUnread,
  matches,
  paperTitle,
  refreshSeconds,
  removableWorkdir,
  runMessage,
  runOptions,
  runningMarkdown,
  runningRows,
  sections,
  stateLabel,
  unfinishedMarkdown,
  unfinishedRows,
  withOpened,
  withoutOpened,
} from "./core";
import {
  installed,
  loadOpened,
  loadRecentDays,
  parseJson,
  preview,
  readJson,
  removePaperFiles,
  saveOpened,
  startRun,
  thumbnail,
  tongtu,
} from "./tongtu";

const CLOCK_MILLISECONDS = 1000;
const GRID_COLUMNS = 5;
const UNREAD_ICON = { source: Icon.CircleFilled, tintColor: Color.Yellow };
const STATE_ICONS = {
  running: { source: Icon.CircleProgress50, tintColor: Color.Blue },
  ok: { source: Icon.CheckCircle, tintColor: Color.Green },
  failed: { source: Icon.XMarkCircle, tintColor: Color.Red },
  interrupted: { source: Icon.Circle, tintColor: Color.Orange },
};
const STATE_COLORS = { running: Color.Blue, ok: Color.Green, failed: Color.Red, interrupted: Color.Orange };

type Shared = { papers: PaperSummary[] | null; missing: boolean; error: string; opened: string[] | null };

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

function refresh(): Promise<void> {
  if (refreshing) refreshAgain = true;
  else refreshing = Promise.resolve().then(refreshUntilSettled);
  return refreshing;
}

function useShared(): Shared {
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

function useNow(active: boolean): number {
  const [, setTicks] = useState(0);
  useEffect(() => {
    if (!active) return;
    const clock = setInterval(() => setTicks((ticks) => ticks + 1), CLOCK_MILLISECONDS);
    return () => clearInterval(clock);
  }, [active]);
  return Date.now();
}

async function setOpened(change: (opened: string[]) => string[]): Promise<void> {
  const opened = change(shared.opened ?? (await loadOpened()) ?? []);
  publish({ opened });
  await saveOpened(opened);
}

async function openPdf(paper: PaperSummary): Promise<void> {
  if (!paper.pdf) return;
  await setOpened((opened) => withOpened(opened, paper.name));
  await open(paper.pdf);
}

async function previewPdf(paper: PaperSummary): Promise<void> {
  if (!paper.pdf) return;
  await setOpened((opened) => withOpened(opened, paper.name));
  preview(paper.pdf);
}

async function run(paper: PaperSummary, action: RunAction, reload: () => Promise<void>): Promise<void> {
  const { options, redo } = runOptions(action, paper.workdir);
  if (redo) await setOpened((opened) => withoutOpened(opened, paper.name));
  startRun(paper.name, options, paper.workdir);
  await reload();
  setTimeout(reload, START_REFRESH_DELAY_SECONDS * 1000);
  await showHUD(runMessage(action, paper.name));
}

async function stopRun(paper: PaperSummary): Promise<boolean> {
  const result = await tongtu(["stop", paper.name, "--workdir", paper.workdir]);
  if (result.code !== 0) {
    await showToast({ style: Toast.Style.Failure, title: `没有停止 ${paper.name}`, message: result.stderr.trim() });
  }
  return result.code === 0;
}

async function stop(paper: PaperSummary, reload: () => Promise<void>): Promise<void> {
  const stopped = await stopRun(paper);
  await reload();
  if (stopped) await showHUD(`已停止 ${paper.name}`);
}

async function remove(paper: PaperSummary): Promise<void> {
  const confirmed = await confirmAlert({
    title: `删除 ${paper.name}？`,
    message: `删除工作目录 ${paper.workdir}、缩略图缓存和已读记录。`,
    primaryAction: { title: "删除", style: Alert.ActionStyle.Destructive },
  });
  if (!confirmed) return;
  if (!removableWorkdir(paper.name, paper.workdir)) {
    await showToast({ style: Toast.Style.Failure, title: `没有删除 ${paper.name}`, message: `工作目录 ${paper.workdir} 与目录名不符` });
    return;
  }
  if (!(await stopRun(paper))) return;
  try {
    removePaperFiles(paper.name, paper.workdir);
  } catch (error) {
    await showToast({
      style: Toast.Style.Failure,
      title: `没有删除 ${paper.name}`,
      message: error instanceof Error ? error.message : String(error),
    });
    await refresh();
    return;
  }
  await setOpened((opened) => withoutOpened(opened, paper.name));
  await refresh();
  await showToast({ style: Toast.Style.Success, title: `已删除 ${paper.name}` });
}

type ActionsProps = { paper: PaperSummary; reload: () => Promise<void>; toggleView?: () => void };

function RunItem({ paper, action, icon, reload }: { paper: PaperSummary; action: RunAction; icon: Icon; reload: () => Promise<void> }) {
  return <Action title={RUN_TITLES[action]} icon={icon} onAction={() => run(paper, action, reload)} />;
}

function PaperActions({ paper, reload, toggleView }: ActionsProps) {
  const original = path.join(paper.workdir, "build/precompile.pdf");
  const arxiv = arxivUrl(paper);
  const details = toggleView && <Action.Push title="查看详情" icon={Icon.Sidebar} target={<PaperDetailView name={paper.name} />} />;
  const finder = <Action.ShowInFinder title="在访达中显示" path={paper.workdir} />;
  return (
    <ActionPanel>
      {paper.state === "ok" && (
        <>
          <Action title="打开中文 PDF" icon={Icon.Document} onAction={() => openPdf(paper)} />
          <Action title="预览" icon={Icon.Eye} onAction={() => previewPdf(paper)} />
          {details}
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
        <Action title="刷新" icon={Icon.ArrowClockwise} shortcut={{ modifiers: ["cmd"], key: "r" }} onAction={reload} />
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
          onAction={() => remove(paper)}
        />
      </ActionPanel.Section>
    </ActionPanel>
  );
}

function shownThumbnail(name: string, pdf: string | null): string | null {
  return thumbnail(name, pdf, () => publish({}));
}

function detailMarkdown(detail: PaperDetail): string {
  if (detail.status.state === "running") return runningMarkdown(detail);
  if (detail.status.state !== "ok") return unfinishedMarkdown(detail);
  return completedMarkdown(detail, shownThumbnail(detail.summary.name, detail.status.pdf));
}

function detailRows(detail: PaperDetail, now: number): Row[] {
  if (detail.status.state === "running") return runningRows(detail, now);
  return detail.status.state === "ok" ? completedRows(detail) : unfinishedRows(detail);
}

function PaperDetailView({ name }: { name: string }) {
  const summary = useShared().papers?.find((other) => other.name === name);
  const workdir = summary?.workdir;
  const [status, setStatus] = useState<PaperStatus | null>(null);
  const [failure, setFailure] = useState("");
  const running = status?.state === "running";
  const now = useNow(running);

  async function loadStatus(): Promise<void> {
    if (!workdir) return;
    const result = await tongtu(["status", name, "--workdir", workdir, "--json"]);
    const next = result.code === 0 ? parseJson<PaperStatus>(result.stdout) : null;
    if (next) setStatus(next);
    setFailure(next ? "" : result.stderr.trim() || "tongtu status --json 没有给出结果");
  }

  async function reload(): Promise<void> {
    await Promise.all([loadStatus(), refresh()]);
  }

  useEffect(() => {
    loadStatus();
  }, [workdir]);

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
  }, [running, workdir]);

  if (!summary) return <Detail markdown={`没有 ${name} 这篇论文。`} />;
  if (!status) return <Detail isLoading={failure === ""} markdown={failure} actions={<PaperActions paper={summary} reload={reload} />} />;
  const detail = { summary, status, meta: readJson<PaperMeta>(status.meta) };
  const current = { ...summary, state: status.state, stage: status.stage, started_at: status.started_at, pdf: status.pdf };
  return (
    <Detail
      markdown={detailMarkdown(detail)}
      actions={<PaperActions paper={current} reload={reload} />}
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

function listAccessories(paper: PaperSummary, opened: string[] | null, now: number): List.Item.Accessory[] {
  if (paper.state !== "ok") return [{ tag: { value: stateLabel(paper, now), color: STATE_COLORS[paper.state] } }];
  const label = { text: stateLabel(paper, now) };
  return isUnread(paper, opened) ? [{ icon: UNREAD_ICON, tooltip: "未读" }, label] : [label];
}

export default function Papers() {
  const { papers, missing, error, opened } = useShared();
  const [view, setView] = useState<"list" | "grid">("list");
  const [recentDays, setRecentDays] = useState(DEFAULT_RECENT_DAYS);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState(FILTER_ALL);
  const now = useNow((papers ?? []).some((paper) => paper.state === "running"));
  useEffect(() => {
    LocalStorage.getItem<string>("view").then((stored) => setView(stored === "grid" ? "grid" : "list"));
    loadRecentDays().then(setRecentDays);
  }, []);
  const toggleView = () => {
    const next = view === "list" ? "grid" : "list";
    setView(next);
    LocalStorage.setItem("view", next);
  };
  const shown = sections(
    (papers ?? []).filter((paper) => matches(paper, search, filter, opened)),
    recentDays,
    now,
  );
  const empty = missing ? NOT_INSTALLED : error || (papers === null ? "" : "没有论文");
  const emptyActions = (
    <ActionPanel>
      <Action title="刷新" icon={Icon.ArrowClockwise} shortcut={{ modifiers: ["cmd"], key: "r" }} onAction={refresh} />
    </ActionPanel>
  );
  const filters = [
    { value: FILTER_ALL, title: "全部" },
    { value: FILTER_UNREAD, title: "未读" },
    ...categories(papers ?? []).map((category) => ({ value: category, title: category })),
  ];
  const common = {
    isLoading: papers === null && !missing && error === "",
    filtering: false,
    onSearchTextChange: setSearch,
    searchBarPlaceholder: "搜索标题或编号",
  };
  if (view === "grid") {
    return (
      <Grid
        {...common}
        columns={GRID_COLUMNS}
        aspectRatio="3/2"
        fit={Grid.Fit.Fill}
        inset={Grid.Inset.Small}
        searchBarAccessory={
          <Grid.Dropdown tooltip="筛选" value={filter} onChange={setFilter}>
            {filters.map((item) => (
              <Grid.Dropdown.Item key={item.value} title={item.title} value={item.value} />
            ))}
          </Grid.Dropdown>
        }
      >
        {shown.length === 0 && <Grid.EmptyView title={empty} actions={emptyActions} />}
        {shown.map((section) => (
          <Grid.Section key={section.title} title={section.title} subtitle={`${section.papers.length}`}>
            {section.papers.map((paper) => (
              <Grid.Item
                key={paper.name}
                content={shownThumbnail(paper.name, paper.pdf) ?? "placeholder.png"}
                title={paperTitle(paper)}
                subtitle={stateLabel(paper, now)}
                keywords={[paper.name]}
                accessory={isUnread(paper, opened) ? { icon: UNREAD_ICON, tooltip: "未读" } : undefined}
                actions={<PaperActions paper={paper} reload={refresh} toggleView={toggleView} />}
              />
            ))}
          </Grid.Section>
        ))}
      </Grid>
    );
  }
  return (
    <List
      {...common}
      searchBarAccessory={
        <List.Dropdown tooltip="筛选" value={filter} onChange={setFilter}>
          {filters.map((item) => (
            <List.Dropdown.Item key={item.value} title={item.title} value={item.value} />
          ))}
        </List.Dropdown>
      }
    >
      {shown.length === 0 && <List.EmptyView title={empty} actions={emptyActions} />}
      {shown.map((section) => (
        <List.Section key={section.title} title={section.title} subtitle={`${section.papers.length}`}>
          {section.papers.map((paper) => (
            <List.Item
              key={paper.name}
              icon={STATE_ICONS[paper.state]}
              title={paperTitle(paper)}
              keywords={[paper.name]}
              accessories={listAccessories(paper, opened, now)}
              actions={<PaperActions paper={paper} reload={refresh} toggleView={toggleView} />}
            />
          ))}
        </List.Section>
      ))}
    </List>
  );
}
