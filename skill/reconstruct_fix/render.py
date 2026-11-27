import sys
from pathlib import Path

from tongtu.reconstruct import review

SESSION = Path(__file__).resolve().parents[2]


def main(pages: list[str]) -> int:
    for page in pages:
        path = review.render_comparison(SESSION / "original.pdf", SESSION / "main.pdf", int(page), SESSION)
        print(f"{path.name}  left=original right=rebuilt" if path else f"page {page} exists in neither PDF")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
