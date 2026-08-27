from __future__ import annotations

from .. import masking, pipeline
from ..artifacts.mask import (
    BlockRecord,
    BlocksFile,
    CaptionRecord,
    EnvironmentDecisionRecord,
    MaskManifest,
    MaskStatus,
)
from ..manifests import describe_error, write_manifest
from ..workdir import ENCODING, Workdir

STAGE_NAME = "mask"


def run(paper_workdir: Workdir) -> MaskManifest:
    paper_workdir.create()
    pipeline.clean(paper_workdir, STAGE_NAME)
    manifest = _execute(paper_workdir)
    write_manifest(paper_workdir.manifest_path(STAGE_NAME), manifest)
    return manifest


def _execute(paper_workdir: Workdir) -> MaskManifest:
    try:
        table = masking.parse_environment_table(masking.ENVIRONMENTS_TABLE_PATH.read_text(encoding=ENCODING))
        source = paper_workdir.precompile_tex.read_text(encoding=ENCODING)
        outcome = masking.mask_document(source, table)
        masking.verify_roundtrip(source, outcome)
    except (OSError, UnicodeDecodeError, masking.MaskError) as error:
        return MaskManifest(status=MaskStatus.MASK_FAILED, message=describe_error(error))
    paper_workdir.masked.write_text(outcome.masked, encoding=ENCODING)
    paper_workdir.blocks.write_text(_blocks_file(outcome).model_dump_json(indent=2) + "\n", encoding=ENCODING)
    return MaskManifest(
        status=MaskStatus.OK,
        environments=_environment_records(outcome),
        blocks_total=len(outcome.blocks),
        captions_total=len(outcome.captions),
        precompile_chars=len(source),
        masked_chars=len(outcome.masked),
        masked_chars_ratio=round(len(outcome.masked) / len(source), 4) if source else 0.0,
        warnings=list(outcome.warnings),
    )


def _blocks_file(outcome: masking.MaskOutcome) -> BlocksFile:
    return BlocksFile(
        blocks=[
            BlockRecord(
                id=block.id,
                category=block.category,
                environment=block.environment,
                decided_by=str(block.decided_by) if block.decided_by is not None else "",
                labels=list(block.labels),
                tex=block.tex,
                start=block.start,
                end=block.end,
                line=block.line,
            )
            for block in outcome.blocks
        ],
        captions=[
            CaptionRecord(
                id=caption.id,
                block_id=caption.block_id,
                kind=caption.kind,
                tex=caption.tex,
                masked_text=caption.masked_text,
            )
            for caption in outcome.captions
        ],
    )


def _environment_records(outcome: masking.MaskOutcome) -> dict[str, EnvironmentDecisionRecord]:
    return {
        name: EnvironmentDecisionRecord(
            classification=decision.classification,
            category=str(decision.category) if decision.category is not None else "",
            decided_by=decision.decided_by,
            occurrences=decision.occurrences,
            blocks=decision.blocks,
        )
        for name, decision in sorted(outcome.environments.items())
    }
