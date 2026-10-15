from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from tongtu import chunks, pipeline, validation
from tongtu.artifacts.mask import BlockCategory, BlocksFile
from tongtu.artifacts.survey import BriefFile, DecidedBy
from tongtu.assets import asset_path
from tongtu.model.work import skill_dir, work
from tongtu.stages import compile as compile_stage
from tongtu.workdir import ENCODING, Workdir

ROLE = "translate_session"

SUFFIX = "-session"

READ_ONLY_MODE = 0o444

REPORT_FILENAME = "session-report.json"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="translate a whole paper in one agent session")
    parser.add_argument("workdir", type=Path, help="a paper workdir that has passed survey")
    parser.add_argument("--no-compile", action="store_true")
    args = parser.parse_args(argv)

    source = Workdir(args.workdir.expanduser().absolute())
    target = Workdir(source.path.with_name(source.path.name + SUFFIX))
    if not source.brief.is_file():
        print(f"{source.brief} does not exist; run tongtu up to survey first.")
        return 1
    if target.path.exists():
        print(f"{target.path} already exists; delete it to rerun the experiment.")
        return 1
    shutil.copytree(source.path, target.path, symlinks=True)
    pipeline.clean_from(target, "translate")

    brief = BriefFile.model_validate_json(target.brief.read_text(encoding=ENCODING))
    blocks = BlocksFile.model_validate_json(target.blocks.read_text(encoding=ENCODING))
    site = target.sandbox(ROLE)
    stage_site(site, target, brief, blocks)

    print(f"workdir {target.path}")
    print(f"session {site}")
    started = time.monotonic()
    outcome = work(
        ROLE,
        site,
        trace_path=target.logs / f"{ROLE}.jsonl",
        report=lambda action: print(f"  {action}", flush=True),
    )
    elapsed = time.monotonic() - started

    records = judge(site, target, brief)
    counts: dict[str, int] = {}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    report = {
        "stop_reason": str(outcome.stop_reason),
        "detail": outcome.detail,
        "model": outcome.model,
        "elapsed_seconds": round(elapsed),
        "counts": counts,
        "glossary": glossary_summary(site / "glossary.json"),
        "chunks": records,
    }
    (target.logs / REPORT_FILENAME).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding=ENCODING)
    print(f"session {outcome.stop_reason} in {round(elapsed)} s; {counts}; glossary {report['glossary']}")
    print(f"report  {target.logs / REPORT_FILENAME}")

    if args.no_compile:
        return 0
    manifest = compile_stage.run(target, report=lambda status, summary: print(f"  {status} {summary}", flush=True))
    print(f"compile {manifest.status}; {target.zh_pdf}")
    return 0


def stage_site(site: Path, target: Workdir, brief: BriefFile, blocks: BlocksFile) -> None:
    (site / "chunks").mkdir(parents=True)
    (site / "translated").mkdir()
    for record in brief.chunks:
        destination = site / "chunks" / f"{record.id}.tex"
        shutil.copyfile(target.chunks / f"{record.id}.tex", destination)
        destination.chmod(READ_ONLY_MODE)
    paper = {
        "abstract": brief.abstract,
        "heading_tree": [heading.model_dump() for heading in brief.heading_tree],
        "chunks": [
            {
                "id": record.id,
                "part": str(record.part),
                "headings": [heading.argument for heading in record.headings],
                "translatable_chars": record.translatable_chars,
            }
            for record in brief.chunks
        ],
    }
    (site / "paper.json").write_text(json.dumps(paper, ensure_ascii=False, indent=2) + "\n", encoding=ENCODING)
    tables = [f"% {block.id}\n{block.tex}\n" for block in blocks.blocks if block.category is BlockCategory.TABLE]
    (site / "tables.tex").write_text("\n".join(tables), encoding=ENCODING)
    glossary = {
        "terms": [
            {"en": entry.word, "variants": [], "zh": entry.translation, "annotate_first": True, "source": "user"}
            for entry in brief.terms
            if entry.decided_by is not DecidedBy.SURVEY
        ],
        "keep_english": [
            {"word": entry.word, "source": "user"}
            for entry in brief.do_not_translate
            if entry.decided_by is not DecidedBy.SURVEY
        ],
    }
    (site / "glossary.json").write_text(json.dumps(glossary, ensure_ascii=False, indent=2) + "\n", encoding=ENCODING)
    (site / "progress.md").write_text("", encoding=ENCODING)
    skill_path = site / skill_dir(ROLE)
    skill_path.mkdir(parents=True)
    shutil.copyfile(asset_path("skill") / "translate" / "SKILL.md", skill_path / "rules.md")
    shutil.copyfile(Path(validation.__file__), skill_path / "validate.py")


def judge(site: Path, target: Workdir, brief: BriefFile) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    target.translated.mkdir(parents=True, exist_ok=True)
    target.reviewed.mkdir(parents=True, exist_ok=True)
    for record in brief.chunks:
        source = (target.chunks / f"{record.id}.tex").read_text(encoding=ENCODING)
        status, failures, body = verdict(source, site / "translated" / f"{record.id}.tex", record.translatable_chars)
        content = chunks.restore_padding(source, body)
        for directory in (target.translated, target.reviewed):
            (directory / f"{record.id}.tex").write_text(content, encoding=ENCODING)
        records.append({"id": record.id, "status": status, "failures": failures})
    return records


def verdict(source: str, output: Path, translatable_chars: int) -> tuple[str, list[str], str]:
    if not translatable_chars:
        return "skipped", [], source
    try:
        translation = output.read_text(encoding=ENCODING)
    except (OSError, UnicodeDecodeError):
        return "missing", [], source
    if translation.strip() == source.strip():
        return "untranslated", [], source
    result = validation.validate(source.strip(), translation.strip())
    if not result.ok:
        return "failed", [f"{failure.check}: {failure.message}" for failure in result.failures], source
    return "translated", [], translation


def glossary_summary(path: Path) -> dict[str, object]:
    try:
        glossary = json.loads(path.read_text(encoding=ENCODING))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return {"error": f"{type(error).__name__}: {error}"}
    return {
        "terms": len(glossary.get("terms", [])),
        "keep_english": len(glossary.get("keep_english", [])),
        "annotate_first": sum(1 for entry in glossary.get("terms", []) if entry.get("annotate_first")),
    }


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
