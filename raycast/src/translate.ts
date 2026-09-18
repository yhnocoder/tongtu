import { LaunchProps, showHUD } from "@raycast/api";
import { start } from "./tongtu";

export default async function Command(props: LaunchProps<{ arguments: { paper: string } }>) {
  const paper = props.arguments.paper.trim();
  start(paper);
  await showHUD(`Translating ${paper}`);
}
