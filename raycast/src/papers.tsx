import { Action, ActionPanel, Color, Icon, List, showHUD, showInFinder, open } from "@raycast/api";
import { useEffect, useState } from "react";
import { Paper, STAGES, fetchTitles, scan, start, stop, thumbnail } from "./tongtu";

const ICONS = {
  running: { source: Icon.CircleProgress50, tintColor: Color.Blue },
  ok: { source: Icon.CheckCircle, tintColor: Color.Green },
  failed: { source: Icon.XMarkCircle, tintColor: Color.Red },
  partial: { source: Icon.Circle, tintColor: Color.SecondaryText },
};

const TAG_COLORS = { running: Color.Blue, ok: Color.Green, failed: Color.Red, partial: Color.SecondaryText };

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
  const parts: string[] = [];
  const png = p.status === "ok" ? thumbnail(p) : null;
  if (png) parts.push(`![](file://${png})`);
  for (const s of p.stages) {
    if (s.status !== "ok") parts.push(`### ${s.name}: ${s.status}\n\n${s.message}`);
    if (s.warnings.length) parts.push(`### ${s.name} warnings\n\n${s.warnings.map((w) => `- ${w}`).join("\n")}`);
  }
  if (p.events.length) parts.push(`### ${p.stage} session\n\n${p.events.map((e) => "- " + e.replace(/\s+/g, " ").slice(0, 200)).join("\n")}`);
  if (!p.stages.find((s) => s.name === p.stage) && !p.events.length && p.status !== "ok")
    parts.push(`_${p.stage} has not run: no manifest and no session log._`);
  return parts.join("\n\n");
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
          keywords={[p.id]}
          accessories={[{ tag: { value: tag(p), color: TAG_COLORS[p.status] } }]}
          icon={ICONS[p.status]}
          quickLook={p.status === "ok" ? { path: p.pdf, name: p.id } : undefined}
          detail={
            <List.Item.Detail
              markdown={markdown(p)}
              metadata={
                <List.Item.Detail.Metadata>
                  <List.Item.Detail.Metadata.Link title="arXiv" text={p.id} target={`https://arxiv.org/abs/${p.id}`} />
                  <List.Item.Detail.Metadata.Separator />
                  {STAGES.map((name) => (
                    <List.Item.Detail.Metadata.Label key={name} title={name} text={stageText(p, name)} />
                  ))}
                </List.Item.Detail.Metadata>
              }
            />
          }
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
