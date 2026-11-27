import sys
from pathlib import Path

import pymupdf

from tongtu.reconstruct import review

SESSION = Path(__file__).resolve().parents[2]


def main(arguments: list[str]) -> int:
    page, x0, y0, x1, y1, name = int(arguments[0]), *map(float, arguments[1:5]), arguments[5]
    print(review.crop_original(SESSION / "original.pdf", SESSION, page, pymupdf.Rect(x0, y0, x1, y1), name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
