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
  confirmAlert,
  openExtensionPreferences,
  showHUD,
  showInFinder,
  open,
} from "@raycast/api";
import { ReactNode, useEffect, useState } from "react";
import { ASPECT, Paper, Prefs, STAGES, fetchMeta, prefs, quickLook, remove, scan, start, stop, thumbnail } from "./tongtu";

const ICONS = {
  running: { source: Icon.CircleProgress50, tintColor: Color.Blue },
  ok: { source: Icon.CheckCircle, tintColor: Color.Green },
  failed: { source: Icon.XMarkCircle, tintColor: Color.Red },
  partial: { source: Icon.Circle, tintColor: Color.SecondaryText },
};

const TAG_COLORS = { running: Color.Blue, ok: Color.Green, failed: Color.Red, partial: Color.SecondaryText };

const oneLine = (text: string) => text.replace(/\s+/g, " ").trim();

function progress(p: Paper): string {
  if (p.status === "ok") return "done";
  const step = p.translated ? `translate ${p.translated[0]}/${p.translated[1]}` : `${p.stage} ${p.done}/${STAGES.length}`;
  return p.message ? `${step} · ${oneLine(p.message)}` : step;
}

function tag(p: Paper): string {
  if (p.status === "ok") return "done";
  return p.translated ? `translate ${p.translated[0]}/${p.translated[1]}` : p.stage;
}

function stageText(p: Paper, name: string): { value: string; color: Color } {
  const stage = p.stages.find((s) => s.name === name);
  if (!stage) return { value: p.status === "running" && p.stage === name ? "running" : "—", color: Color.SecondaryText };
  if (stage.status !== "ok") return { value: stage.status, color: Color.Red };
  return stage.warnings.length
    ? { value: `ok · ${stage.warnings.length} warning${stage.warnings.length > 1 ? "s" : ""}`, color: Color.Yellow }
    : { value: "ok", color: Color.Green };
}

function markdown(p: Paper): string {
  const parts: string[] = [`# ${p.title || p.id}`];
  const png = p.status === "ok" ? thumbnail(p) : null;
  if (png) parts.push(`![](file://${png})`);
  for (const s of p.stages) {
    if (s.status !== "ok") parts.push(`### ${s.name}: ${s.status}\n\n${oneLine(s.message)}`);
    if (s.warnings.length) parts.push(`### ${s.name} warnings\n\n${s.warnings.map((w) => `- ${oneLine(w)}`).join("\n")}`);
  }
  if (p.events.length) parts.push(`### ${p.stage} session\n\n${p.events.map((e) => "- " + oneLine(e).slice(0, 200)).join("\n")}`);
  if (!p.stages.find((s) => s.name === p.stage) && !p.events.length && p.status !== "ok")
    parts.push(`_${p.stage} has not run: no manifest and no session log._`);
  return parts.join("\n\n");
}

async function confirmRemove(p: Paper) {
  const ok = await confirmAlert({
    title: `Remove ${p.id}?`,
    message: `Deletes ${p.dir} and its cached metadata.`,
    primaryAction: { title: "Remove", style: Alert.ActionStyle.Destructive },
  });
  if (!ok) return;
  remove(p);
  showHUD(`Removed ${p.id}`);
}

function Actions({ p, detail, toggleView, extra }: { p: Paper; detail?: boolean; toggleView?: () => void; extra?: ReactNode }) {
  return (
    <ActionPanel>
      {p.status === "ok" &&
        (detail ? (
          <Action title="Preview PDF" icon={Icon.Eye} onAction={() => quickLook(p.pdf)} />
        ) : (
          <Action.ToggleQuickLook title="Preview PDF" />
        ))}
      {!detail && <Action.Push title="Show Details" icon={Icon.Sidebar} target={<PaperDetail id={p.id} />} />}
      {p.status === "ok" && <Action title="Open PDF" icon={Icon.Document} onAction={() => open(p.pdf)} />}
      <Action title="Show in Finder" icon={Icon.Finder} onAction={() => showInFinder(p.dir)} />
      {p.status === "running" ? (
        <Action
          title="Stop"
          icon={Icon.Stop}
          style={Action.Style.Destructive}
          onAction={() => {
            stop(p);
            showHUD(`Stopped ${p.id}`);
          }}
        />
      ) : (
        <Action
          title="Resume Translation"
          icon={Icon.Play}
          onAction={() => {
            start(p.id);
            showHUD(`Translating ${p.id}`);
          }}
        />
      )}
      <ActionPanel.Section>
        {extra}
        {toggleView && <Action title="Toggle Grid / List" icon={Icon.AppWindowGrid2x2} shortcut={{ modifiers: ["cmd", "shift"], key: "v" }} onAction={toggleView} />}
        <Action title="Open Preferences" icon={Icon.Gear} onAction={openExtensionPreferences} />
        <Action
          title="Remove Paper"
          icon={Icon.Trash}
          style={Action.Style.Destructive}
          shortcut={{ modifiers: ["ctrl"], key: "x" }}
          onAction={() => confirmRemove(p)}
        />
      </ActionPanel.Section>
    </ActionPanel>
  );
}

export function usePapers(): Paper[] {
  const [papers, setPapers] = useState<Paper[]>(scan);
  useEffect(() => {
    fetchMeta(papers).then((changed) => changed && setPapers(scan()));
    const timer = setInterval(() => setPapers(scan()), 2000);
    return () => clearInterval(timer);
  }, []);
  return papers;
}

export function useStored<T extends string>(key: string, fallback: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(fallback);
  useEffect(() => {
    LocalStorage.getItem<T>(key).then((stored) => stored && setValue(stored));
  }, []);
  return [
    value,
    (next: T) => {
      setValue(next);
      LocalStorage.setItem(key, next);
    },
  ];
}

function PaperDetail({ id }: { id: string }) {
  const p = usePapers().find((paper) => paper.id === id);
  if (!p) return <Detail markdown={`_${id} is gone._`} />;
  return (
    <Detail
      markdown={markdown(p)}
      actions={<Actions p={p} detail />}
      metadata={
        <Detail.Metadata>
          <Detail.Metadata.Link title="arXiv" text={p.id} target={`https://arxiv.org/abs/${p.id}`} />
          {p.authors.length > 0 && <Detail.Metadata.Label title="authors" text={p.authors.join(", ")} />}
          {p.category && <Detail.Metadata.Label title="category" text={p.category} />}
          {p.published && <Detail.Metadata.Label title="published" text={p.published} />}
          <Detail.Metadata.Label title="added" text={new Date(p.added).toLocaleDateString()} />
          <Detail.Metadata.Separator />
          <Detail.Metadata.Label title="progress" text={progress(p)} icon={ICONS[p.status]} />
          {STAGES.map((name) => (
            <Detail.Metadata.Label key={name} title={name} text={stageText(p, name)} />
          ))}
        </Detail.Metadata>
      }
    />
  );
}

export type Section = { title: string; papers: Paper[] };

export function Browse({ sections, accessory, extra }: { sections: Section[]; accessory?: ReactNode; extra?: ReactNode }) {
  const settings = prefs();
  const [view, setView] = useStored<Prefs["view"]>("view", settings.view);
  const toggleView = () => setView(view === "grid" ? "list" : "grid");
  if (view === "list") {
    return (
      <List searchBarPlaceholder="Filter papers" searchBarAccessory={accessory as List.Props["searchBarAccessory"]}>
        {sections.map((section) => (
          <List.Section key={section.title} title={section.title} subtitle={`${section.papers.length}`}>
            {section.papers.map((p) => (
              <List.Item
                key={`${section.title}/${p.id}`}
                icon={ICONS[p.status]}
                title={p.title || p.id}
                subtitle={p.id}
                keywords={[p.id]}
                accessories={[{ date: new Date(p.added) }, { tag: { value: tag(p), color: TAG_COLORS[p.status] } }]}
                quickLook={p.status === "ok" ? { path: p.pdf, name: p.id } : undefined}
                actions={<Actions p={p} toggleView={toggleView} extra={extra} />}
              />
            ))}
          </List.Section>
        ))}
      </List>
    );
  }
  return (
    <Grid
      columns={Number(settings.columns) || 5}
      aspectRatio={ASPECT[settings.thumbnail]}
      fit={Grid.Fit.Fill}
      inset={Grid.Inset.Small}
      searchBarPlaceholder="Filter papers"
      searchBarAccessory={accessory as Grid.Props["searchBarAccessory"]}
    >
      {sections.map((section) => (
        <Grid.Section key={section.title} title={section.title} subtitle={`${section.papers.length}`}>
          {section.papers.map((p) => (
            <Grid.Item
              key={`${section.title}/${p.id}`}
              content={(p.status === "ok" && thumbnail(p)) || "placeholder.png"}
              title={p.title || p.id}
              subtitle={p.status === "ok" ? p.id : progress(p)}
              keywords={[p.id]}
              accessory={p.status === "ok" ? undefined : { icon: ICONS[p.status], tooltip: progress(p) }}
              quickLook={p.status === "ok" ? { path: p.pdf, name: p.id } : undefined}
              actions={<Actions p={p} toggleView={toggleView} extra={extra} />}
            />
          ))}
        </Grid.Section>
      ))}
    </Grid>
  );
}
