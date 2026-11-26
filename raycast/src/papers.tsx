import { ActionPanel, Color, Grid, Icon, List, LocalStorage } from "@raycast/api";
import { useEffect, useState } from "react";
import {
  DEFAULT_RECENT_DAYS,
  FILTER_ALL,
  FILTER_UNREAD,
  NOT_INSTALLED,
  PaperSummary,
  categories,
  isUnread,
  matches,
  paperTitle,
  sections,
  stateLabel,
} from "./core";
import { PaperActions, RefreshAction, refresh, shownThumbnail, useNow, useShared } from "./detail";
import { loadRecentDays } from "./tongtu";

const GRID_COLUMNS = 5;
const UNREAD_ICON = { source: Icon.CircleFilled, tintColor: Color.Yellow };
const STATE_ICONS = {
  running: { source: Icon.CircleProgress50, tintColor: Color.Blue },
  ok: { source: Icon.CheckCircle, tintColor: Color.Green },
  failed: { source: Icon.XMarkCircle, tintColor: Color.Red },
  interrupted: { source: Icon.Circle, tintColor: Color.Orange },
};
const STATE_COLORS = { running: Color.Blue, ok: Color.Green, failed: Color.Red, interrupted: Color.Orange };

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
      <RefreshAction reload={refresh} />
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
