from __future__ import annotations

from .. import masking, pipeline
from ..artifacts.mask import BlocksFile, MaskManifest, MaskStatus
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
    blocks_file = BlocksFile(blocks=list(outcome.blocks), captions=list(outcome.captions))
    paper_workdir.blocks.write_text(blocks_file.model_dump_json(indent=2) + "\n", encoding=ENCODING)
    return MaskManifest(
        status=MaskStatus.OK,
        environments=dict(sorted(outcome.environments.items())),
        blocks_total=len(outcome.blocks),
        captions_total=len(outcome.captions),
        precompile_chars=len(source),
        masked_chars=len(outcome.masked),
        masked_chars_ratio=round(len(outcome.masked) / len(source), 4) if source else 0.0,
        warnings=list(outcome.warnings),
    )
