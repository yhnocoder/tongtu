import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SESSION = HERE.parents[1]
GLOSSARIES = ("glossary.json", "glossary.fixed.json")

sys.path.insert(0, str(HERE))

import validate  # noqa: E402


def main() -> int:
    os.chdir(SESSION)
    arguments = ["en.tex", "zh.tex"]
    for name in GLOSSARIES:
        if Path(name).is_file():
            arguments += ["--glossary", name]
    return validate.main(arguments)


if __name__ == "__main__":
    sys.exit(main())
