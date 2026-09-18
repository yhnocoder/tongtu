import { Action, ActionPanel, Color, Icon, List, showHUD, showInFinder, open } from "@raycast/api";
import { useEffect, useState } from "react";
import { Paper, STAGES, fetchTitles, scan, start, stop, thumbnail } from "./tongtu";

const ICONS = {
  running: { source: Icon.CircleProgress50, tintColor: Color.Blue },
  ok: { source: Icon.CheckCircle, tintColor: Color.Green },
  failed: { source: Icon.XMarkCircle, tintColor: Color.Red },
  partial: { source: Icon.Circle, tintColor: Color.SecondaryText },
};

function subtitle(p: Paper): string {
  if (p.status === "ok") return "done";
  const step = `${p.stage} ${p.done}/${STAGES.length}`;
  if (p.translated) return `${step} · chunks ${p.translated[0]}/${p.translated[1]}`;
  return p.message ? `${step} · ${p.message}` : step;
}

function detail(p: Paper): string {
  const png = p.status === "ok" ? thumbnail(p) : null;
  const table = ["| stage | status | |", "|---|---|---|", ...p.stages.map((s) => `| ${s.name} | ${s.status} | ${s.message} |`)];
  return [
    `## ${p.title || p.id}`,
    png && `![](${encodeURI(`file://${png}`)})`,
    table.join("\n"),
  ]
    .filter(Boolean)
    .join("\n\n");
}

export default function Command() {
  const [papers, setPapers] = useState<Paper[]>(scan);
  useEffect(() => {
    fetchTitles(papers).then((changed) => changed && setPapers(scan()));
    const timer = setInterval(() => setPapers(scan()), 2000);
    return () => clearInterval(timer);
  }, []);
  return (
    <List isShowingDetail searchBarPlaceholder="Filter papers">
      {papers.map((p) => (
        <List.Item
          key={p.id}
          title={p.title || p.id}
          subtitle={subtitle(p)}
          keywords={[p.id]}
          accessories={[{ text: p.id }]}
          icon={ICONS[p.status]}
          quickLook={p.status === "ok" ? { path: p.pdf, name: p.id } : undefined}
          detail={<List.Item.Detail markdown={detail(p)} />}
          actions={
            <ActionPanel>
              {p.status === "ok" && <Action.ToggleQuickLook title="Preview PDF" />}
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
            </ActionPanel>
          }
        />
      ))}
    </List>
  );
}
