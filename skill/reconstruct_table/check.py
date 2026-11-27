import sys
from pathlib import Path

from tongtu.manifests import load_manifest
from tongtu.reconstruct import recognition, table

SESSION = Path(__file__).resolve().parents[2]


def main(arguments: list[str]) -> int:
    hints = load_manifest(SESSION / recognition.HINTS_FILENAME, table.TableHints)
    if hints is None:
        print(f"cannot read {recognition.HINTS_FILENAME}")
        return 1
    transcriptions, problem = recognition.read_transcriptions(SESSION, "reconstruct_table")
    if problem:
        print(problem)
        return 1
    keys = [key for key in hints.root if not arguments or key in arguments]
    verdicts = table.verify_all(SESSION, transcriptions, keys)
    print("\n".join(table.failure_lines(verdicts)))
    return 0 if all(verdict.passed for verdict in verdicts.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
