import { Action, Icon } from "@raycast/api";
import { Browse, Section, usePapers } from "./browse";
import Library from "./library";
import { prefs } from "./tongtu";

export default function Recent() {
  const papers = usePapers();
  const cutoff = Date.now() - (Number(prefs().recentDays) || 7) * 86_400_000;
  const sections: Section[] = [
    { title: "Running", papers: papers.filter((p) => p.status === "running") },
    { title: "Recent", papers: papers.filter((p) => p.status !== "running" && p.mtime >= cutoff) },
  ].filter((s) => s.papers.length);
  return (
    <Browse
      sections={sections}
      extra={<Action.Push title="Open Library" icon={Icon.Book} shortcut={{ modifiers: ["cmd"], key: "l" }} target={<Library />} />}
    />
  );
}
