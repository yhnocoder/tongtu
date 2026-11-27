import sys
from pathlib import Path

from tongtu import compiling, texlog
from tongtu.artifacts.reconstruct import RegionsFile
from tongtu.manifests import load_manifest
from tongtu.reconstruct import checks, review
from tongtu.reconstruct.rebuild import MAIN_FILENAME, REGIONS_FILENAME

SESSION = Path(__file__).resolve().parents[2]

ORIGINAL = "original.pdf"

ERROR_LINES = 20


def main() -> int:
    attempt = compiling.attempt_compile(SESSION, MAIN_FILENAME)
    if attempt.outcome.returncode != 0:
        print(f"latexmk exited with code {attempt.outcome.returncode}")
        print("\n".join(texlog.error_lines(attempt.log_text or "", ERROR_LINES)))
    regions = load_manifest(SESSION / REGIONS_FILENAME, RegionsFile) or RegionsFile()
    detail = checks.check(SESSION / ORIGINAL, SESSION, attempt.report, regions)
    print("\n".join(review.summary_lines(detail, checks.lost_glyphs(SESSION))))
    return 0 if detail.report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
