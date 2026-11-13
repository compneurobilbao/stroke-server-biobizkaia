import argparse
import time
from pathlib import Path


MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]/Contents 4 0 R>>endobj\n4 0 obj<</Length 76>>stream\nBT /F1 12 Tf 40 100 Td (Dummy report - no clinical validity) Tj ET\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Dummy processor for development. It has no clinical validity.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--study-id", required=True)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--fail", action="store_true")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    if not input_dir.exists():
        raise SystemExit("Input directory does not exist")
    if args.fail:
        raise SystemExit("Forced dummy processor failure")

    output_dir.mkdir(parents=True, exist_ok=True)
    time.sleep(args.sleep)
    (output_dir / f"informe-{args.study_id}.pdf").write_bytes(MINIMAL_PDF)
    print(f"Dummy PDF generated for study {args.study_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
