import sys
from pathlib import Path

from tongtu.manifests import load_manifest
from tongtu.reconstruct import formula, recognition

SESSION = Path(__file__).resolve().parents[2]


def main(arguments: list[str]) -> int:
    hints = load_manifest(SESSION / recognition.HINTS_FILENAME, formula.FormulaHints)
    if hints is None:
        print(f"cannot read {recognition.HINTS_FILENAME}")
        return 1
    transcriptions, problem = recognition.read_transcriptions(SESSION, "reconstruct_formula")
    if problem:
        print(problem)
        return 1
    keys = {key: hint.kind for key, hint in hints.root.items() if not arguments or key in arguments}
    verdicts = formula.verify_all(SESSION, transcriptions, keys)
    print("\n".join(formula.failure_lines(verdicts)))
    return 0 if all(verdict.passed for verdict in verdicts.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
