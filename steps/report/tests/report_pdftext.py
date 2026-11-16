"""Text-level checks of the report PDF (never byte-level: PDF bytes carry ids and dates).

The text of every page is extracted with pypdf. The running banner and the page counter
are removed from the comparison text (they are checked separately, page by page), and
whitespace is ignored, so that a different line or page break does not matter.

    python steps/report/tests/report_pdftext.py PDF GOLDEN_TXT LANG [--update]
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

from pypdf import PdfReader

BANNERS = {
    "en": "FOR RESEARCH USE ONLY - NOT FOR CLINICAL DECISIONS",
    "es": "SOLO PARA USO EN INVESTIGACIÓN - NO PARA DECISIONES CLÍNICAS",
}
FOOTER = re.compile(r"(Page|Página)\s*\d+\s*(of|de)\s*\d+")


def squash(text: str) -> str:
    return "".join(text.split())


def page_texts(pdf: Path) -> list[str]:
    return [page.extract_text() or "" for page in PdfReader(str(pdf)).pages]


def pages_without_banner(pdf: Path, lang: str) -> list[str]:
    """Pages whose text lacks the research-use banner (empty list when all carry it)."""
    banner = squash(BANNERS[lang])
    return [str(n) for n, t in enumerate(page_texts(pdf), start=1) if banner not in squash(t)]


def content_text(pdf: Path, lang: str) -> str:
    """Whitespace-normalised text of all pages without banner and page counter."""
    banner = squash(BANNERS[lang])
    parts = []
    for text in page_texts(pdf):
        flat = FOOTER.sub("", squash(text)).replace(banner, "")
        parts.append(flat)
    return "".join(parts)


def readable(pdf: Path, lang: str) -> str:
    """The same text with spaces kept, wrapped for a readable golden file."""
    banner = BANNERS[lang]
    words = []
    for text in page_texts(pdf):
        line = " ".join(text.split())
        line = FOOTER.sub("", line).replace(banner, "")
        words.append(" ".join(line.split()))
    return textwrap.fill(" ".join(words), width=110) + "\n"


def compare(pdf: Path, golden: Path, lang: str) -> str | None:
    """None when the PDF text equals the golden file, else a description of the difference."""
    expected = squash(golden.read_text(encoding="utf-8"))
    actual = content_text(pdf, lang)
    if actual == expected:
        return None
    n = next((i for i, (a, b) in enumerate(zip(actual, expected)) if a != b), min(len(actual), len(expected)))
    return (
        f"PDF text differs from {golden.name} at character {n}: "
        f"actual ...{actual[max(0, n - 40) : n + 40]}... "
        f"expected ...{expected[max(0, n - 40) : n + 40]}..."
    )


def main() -> int:
    pdf, golden, lang = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    if "--update" in sys.argv:
        golden.write_text(readable(pdf, lang), encoding="utf-8")
        return 0
    missing = pages_without_banner(pdf, lang)
    if missing:
        print("banner missing on pages: " + ", ".join(missing))
        return 1
    problem = compare(pdf, golden, lang)
    if problem:
        print(problem)
        return 1
    print("PDF text matches", golden.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
