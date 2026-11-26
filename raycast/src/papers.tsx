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
  FILTER_ALL,
  FILTER_UNREAD,
  GlossaryFile,
  NOT_INSTALLED,
  NotesFile,
  PaperEntry,
  RUN_TITLES,
  Row,
  RunAction,
  arxivUrl,
  categories,
  completedMarkdown,
  completedRows,
  initialOpened,
  isUnread,
  matches,
  paperTitle,
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

const REFRESH_MILLISECONDS = 2000;
const GRID_COLUMNS = 5;
const UNREAD_ICON = { source: Icon.CircleFilled, tintColor: Color.Yellow };
const STATE_ICONS = {
  running: { source: Icon.CircleProgress50, tintColor: Color.Blue },
  ok: { source: Icon.CheckCircle, tintColor: Color.Green },
  failed: { source: Icon.XMarkCircle, tintColor: Color.Red },
  interrupted: { source: Icon.Circle, tintColor: Color.Orange },
};
const STATE_COLORS = { running: Color.Blue, ok: Color.Green, failed: Color.Red, interrupted: Color.Orange };

type Shared = { papers: PaperEntry[] | null; missing: boolean; error: string; opened: string[] | null };

let shared: Shared = { papers: null, missing: false, error: "", opened: null };
let refreshing = false;
let timer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function publish(next: Partial<Shared>): void {
  shared = { ...shared, ...next };
  for (const listener of listeners) listener();
}

async function refresh(): Promise<void> {
  if (refreshing) return;
  refreshing = true;
  try {
    if (!installed()) {
      publish({ missing: true });
      return;
    }
    const result = await tongtu(["list", "--json"]);
    const listing = result.code === 0 ? parseJson<{ papers: PaperEntry[] }>(result.stdout) : null;
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
  } finally {
    refreshing = false;
  }
}

function useShared(): Shared {
  const [, setVersion] = useState(0);
  useEffect(() => {
    const listener = () => setVersion((version) => version + 1);
    listeners.add(listener);
    if (timer === null) {
      refresh();
      timer = setInterval(refresh, REFRESH_MILLISECONDS);
    }
    return () => {
      listeners.delete(listener);
      if (listeners.size === 0 && timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    };
  }, []);
  return shared;
}

async function setOpened(change: (opened: string[]) => string[]): Promise<void> {
  const opened = change(shared.opened ?? (await loadOpened()) ?? []);
  publish({ opened });
  await saveOpened(opened);
}

async function openPdf(paper: PaperEntry): Promise<void> {
  if (!paper.status.pdf) return;
  await setOpened((opened) => withOpened(opened, paper.name));
  await open(paper.status.pdf);
}

async function previewPdf(paper: PaperEntry): Promise<void> {
  if (!paper.status.pdf) return;
  await setOpened((opened) => withOpened(opened, paper.name));
  preview(paper.status.pdf);
}

async function run(paper: PaperEntry, action: RunAction): Promise<void> {
  const { options, mode, redo } = runOptions(action, paper.status.workdir);
  if (redo) await setOpened((opened) => withoutOpened(opened, paper.name));
  const shown = await startRun(paper.name, options, mode);
  await showHUD(`开始翻译 ${paper.name}（${shown}）`);
}

async function stopRun(paper: PaperEntry): Promise<boolean> {
  const result = await tongtu(["stop", paper.name, "--workdir", paper.status.workdir]);
  if (result.code !== 0) {
    await showToast({ style: Toast.Style.Failure, title: `没有停止 ${paper.name}`, message: result.stderr.trim() });
  }
  return result.code === 0;
}

async function stop(paper: PaperEntry): Promise<void> {
  if (await stopRun(paper)) await showHUD(`已停止 ${paper.name}`);
}

async function remove(paper: PaperEntry): Promise<void> {
  const confirmed = await confirmAlert({
    title: `删除 ${paper.name}？`,
    message: `删除工作目录 ${paper.status.workdir}、缩略图缓存和已读记录。`,
    primaryAction: { title: "删除", style: Alert.ActionStyle.Destructive },
  });
  if (!confirmed) return;
  if (paper.state === "running" && !(await stopRun(paper))) return;
  removePaperFiles(paper.name, paper.status.workdir);
  await setOpened((opened) => withoutOpened(opened, paper.name));
  publish({ papers: (shared.papers ?? []).filter((other) => other.name !== paper.name) });
  await showToast({ style: Toast.Style.Success, title: `已删除 ${paper.name}` });
}

function RunItem({ paper, action, icon }: { paper: PaperEntry; action: RunAction; icon: Icon }) {
  return <Action title={RUN_TITLES[action]} icon={icon} onAction={() => run(paper, action)} />;
}

function PaperActions({ paper, toggleView }: { paper: PaperEntry; toggleView?: () => void }) {
  const workdir = paper.status.workdir;
  const original = path.join(workdir, "build/precompile.pdf");
  const arxiv = arxivUrl(paper.status);
  const details = toggleView && <Action.Push title="查看详情" icon={Icon.Sidebar} target={<PaperDetail name={paper.name} />} />;
  const finder = <Action.ShowInFinder title="在访达中显示" path={workdir} />;
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
            <RunItem paper={paper} action="redoFast" icon={Icon.ArrowClockwise} />
            <RunItem paper={paper} action="redoSmart" icon={Icon.ArrowClockwise} />
            <RunItem paper={paper} action="recompile" icon={Icon.Hammer} />
          </ActionPanel.Section>
        </>
      )}
      {paper.state === "running" && (
        <>
          {details}
          <Action title="停止" icon={Icon.Stop} style={Action.Style.Destructive} onAction={() => stop(paper)} />
          {finder}
        </>
      )}
      {(paper.state === "failed" || paper.state === "interrupted") && (
        <>
          {details}
          <RunItem paper={paper} action="retry" icon={Icon.Play} />
          <RunItem paper={paper} action="retrySmart" icon={Icon.Play} />
          <Action.ShowInFinder title="查看日志" icon={Icon.Text} path={path.join(workdir, "logs")} />
          {finder}
        </>
      )}
      <ActionPanel.Section>
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

function detailMarkdown(paper: PaperEntry): string {
  if (paper.state === "running") return runningMarkdown(paper, Date.now());
  if (paper.state !== "ok") return unfinishedMarkdown(paper);
  return completedMarkdown(
    paper,
    thumbnail(paper.name, paper.status.pdf),
    readJson<NotesFile>(paper.status.notes),
    readJson<GlossaryFile>(paper.status.glossary),
  );
}

function detailRows(paper: PaperEntry): Row[] {
  if (paper.state === "running") return runningRows(paper, Date.now());
  return paper.state === "ok" ? completedRows(paper) : unfinishedRows(paper);
}

function PaperDetail({ name }: { name: string }) {
  const paper = useShared().papers?.find((other) => other.name === name);
  if (!paper) return <Detail markdown={`没有 ${name} 这篇论文。`} />;
  return (
    <Detail
      markdown={detailMarkdown(paper)}
      actions={<PaperActions paper={paper} />}
      metadata={
        <Detail.Metadata>
          {detailRows(paper).map((row) =>
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

function keywords(paper: PaperEntry): string[] {
  return [paper.name, ...(paper.info?.authors ?? [])];
}

function listAccessories(paper: PaperEntry, opened: string[] | null): List.Item.Accessory[] {
  if (paper.state !== "ok") return [{ tag: { value: stateLabel(paper, Date.now()), color: STATE_COLORS[paper.state] } }];
  const label = { text: stateLabel(paper, Date.now()) };
  return isUnread(paper, opened) ? [{ icon: UNREAD_ICON, tooltip: "未读" }, label] : [label];
}

export default function Papers() {
  const { papers, missing, error, opened } = useShared();
  const [view, setView] = useState<"list" | "grid">("list");
  const [recentDays, setRecentDays] = useState(DEFAULT_RECENT_DAYS);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState(FILTER_ALL);
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
    Date.now(),
  );
  const empty = missing ? NOT_INSTALLED : error || (papers === null ? "" : "没有论文");
  const filters = [
    { value: FILTER_ALL, title: "全部" },
    { value: FILTER_UNREAD, title: "未读" },
    ...categories(papers ?? []).map((category) => ({ value: category, title: category })),
  ];
  const common = {
    isLoading: papers === null && !missing && error === "",
    filtering: false,
    onSearchTextChange: setSearch,
    searchBarPlaceholder: "搜索标题、作者或编号",
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
        {shown.length === 0 && <Grid.EmptyView title={empty} />}
        {shown.map((section) => (
          <Grid.Section key={section.title} title={section.title} subtitle={`${section.papers.length}`}>
            {section.papers.map((paper) => (
              <Grid.Item
                key={paper.name}
                content={thumbnail(paper.name, paper.status.pdf) ?? "placeholder.png"}
                title={paperTitle(paper)}
                subtitle={stateLabel(paper, Date.now())}
                keywords={keywords(paper)}
                accessory={isUnread(paper, opened) ? { icon: UNREAD_ICON, tooltip: "未读" } : undefined}
                actions={<PaperActions paper={paper} toggleView={toggleView} />}
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
      {shown.length === 0 && <List.EmptyView title={empty} />}
      {shown.map((section) => (
        <List.Section key={section.title} title={section.title} subtitle={`${section.papers.length}`}>
          {section.papers.map((paper) => (
            <List.Item
              key={paper.name}
              icon={STATE_ICONS[paper.state]}
              title={paperTitle(paper)}
              keywords={keywords(paper)}
              accessories={listAccessories(paper, opened)}
              actions={<PaperActions paper={paper} toggleView={toggleView} />}
            />
          ))}
        </List.Section>
      ))}
    </List>
  );
}
