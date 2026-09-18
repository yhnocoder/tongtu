import { Grid } from "@raycast/api";
import { Browse, Section, usePapers, useStored } from "./browse";
import { Prefs, groupKey, prefs } from "./tongtu";

const GROUPS: { value: Prefs["groupBy"]; title: string }[] = [
  { value: "none", title: "No grouping" },
  { value: "added", title: "Month added" },
  { value: "submitted", title: "Month submitted" },
  { value: "category", title: "arXiv category" },
];

export default function Library() {
  const papers = usePapers();
  const [groupBy, setGroupBy] = useStored<Prefs["groupBy"]>("groupBy", prefs().groupBy);
  let sections: Section[] = [{ title: "Library", papers }];
  if (groupBy !== "none") {
    const groups = new Map<string, Section["papers"]>();
    for (const p of papers) groups.set(groupKey(p, groupBy), [...(groups.get(groupKey(p, groupBy)) ?? []), p]);
    sections = [...groups.keys()].sort().reverse().map((key) => ({ title: key, papers: groups.get(key)! }));
  }
  return (
    <Browse
      sections={sections}
      accessory={
        <Grid.Dropdown tooltip="Group by" value={groupBy} onChange={(value) => setGroupBy(value as Prefs["groupBy"])}>
          {GROUPS.map((g) => (
            <Grid.Dropdown.Item key={g.value} title={g.title} value={g.value} />
          ))}
        </Grid.Dropdown>
      }
    />
  );
}
