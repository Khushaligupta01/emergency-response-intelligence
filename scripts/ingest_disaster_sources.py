from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db.session import SessionLocal
from app.services.source_pipeline import default_query_window, ingest_sources


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Dates must include a timezone offset or Z.")
    return parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Poll official disaster sources and discover Copernicus metadata."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="GDACS look-back window in days (default: 7).",
    )
    parser.add_argument(
        "--from-date",
        type=_parse_datetime,
        help="GDACS window start (ISO timestamp with timezone).",
    )
    parser.add_argument(
        "--to-date",
        type=_parse_datetime,
        help="GDACS window end (ISO timestamp with timezone).",
    )
    parser.add_argument(
        "--skip-copernicus",
        action="store_true",
        help="Skip the event-specific Sentinel metadata discovery.",
    )
    args = parser.parse_args()

    window_start, window_end = default_query_window(args.days)
    from_date = args.from_date or window_start
    to_date = args.to_date or window_end
    if to_date < from_date:
        parser.error("--to-date must not precede --from-date.")

    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        with SessionLocal() as session:
            results = ingest_sources(
                session,
                client,
                from_date=from_date,
                to_date=to_date,
                include_copernicus=not args.skip_copernicus,
            )
    print(json.dumps(results, indent=2))
    return 1 if any(result["status"] in ("failed", "partial") for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
