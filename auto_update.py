from __future__ import annotations

import argparse
import json

from rera_intel.auto_sync import run_auto_update
from rera_intel.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Rajasthan RERA auto-update flow if it is due and internet is available."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately, ignoring the afternoon schedule window.",
    )
    args = parser.parse_args()

    settings = get_settings(require_api_key=True)
    result = run_auto_update(settings, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
