"""Run the five steps over a PDF from the command line.

    python -m pipeline.plan.cvdetect.cli input/sample_plan.pdf
    python -m pipeline.plan.cvdetect.cli input/plan.pdf --pages 4 5 --json out.json

Run from the ``backend`` folder, which is where the package's imports resolve
from. Prints one line per sheet and a total, and writes the full reading -
every wall with its centreline, every opening with its evidence - to a JSON
file when asked.
"""

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.plan.cvdetect.detector import read_document  # noqa: E402
from pipeline.plan.cvdetect.settings import load_settings  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read walls, doors, windows and scale from a construction PDF."
    )
    parser.add_argument("pdf", help="the plan set to read")
    parser.add_argument(
        "--pages", nargs="*", type=int, default=None,
        help="1-based page numbers; every page by default",
    )
    parser.add_argument("--json", dest="json_path", default=None, help="write the full reading here")
    parser.add_argument("--password", default="", help="password, if the PDF is locked")
    parser.add_argument("--dpi", type=float, default=None, help="override the render resolution")
    arguments = parser.parse_args(argv)

    if not Path(arguments.pdf).is_file():
        print(f"No such file: {arguments.pdf}")
        return 2

    settings = load_settings({"render_dpi": arguments.dpi} if arguments.dpi else None)
    result = read_document(
        arguments.pdf, settings=settings, pages=arguments.pages, password=arguments.password
    )

    if result.get("note"):
        print(result["note"])

    print(f"\n{Path(arguments.pdf).name} - {result['seconds']}s\n")
    header = f"{'page':>5}  {'scale mm/pt':>11}  {'walls':>5}  {'metres':>7}  {'doors':>5}  {'wins':>5}  {'placed':>6}"
    print(header)
    print("-" * len(header))
    for sheet in result["sheets"]:
        summary = sheet.get("summary") or {}
        scale = sheet.get("scale") or {}
        if not summary.get("walls") and not summary.get("openings"):
            continue
        print(
            f"{sheet['page_number']:>5}  "
            f"{(scale.get('mm_per_point') or 0):>11.3f}  "
            f"{summary.get('walls', 0):>5}  "
            f"{summary.get('wall_metres', 0):>7.1f}  "
            f"{summary.get('doors', 0):>5}  "
            f"{summary.get('windows', 0):>5}  "
            f"{summary.get('openings_placed_on_a_wall', 0):>6}"
        )
    totals = result.get("totals") or {}
    print("-" * len(header))
    print(
        f"  {totals.get('sheets', 0)} sheets, "
        f"{totals.get('sheets_with_a_measured_scale', 0)} with a scale established, "
        f"{totals.get('walls', 0)} walls, "
        f"{totals.get('openings', 0)} openings, "
        f"{totals.get('openings_placed_on_a_wall', 0)} of them placed on a wall"
    )

    if arguments.json_path:
        Path(arguments.json_path).write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8"
        )
        print(f"\nFull reading written to {arguments.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
