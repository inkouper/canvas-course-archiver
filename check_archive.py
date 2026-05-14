"""
Quick integrity check for a Canvas course archive.

Usage:
    python check_archive.py <archive_folder>

Example:
    python check_archive.py canvas_archive_12345_20260513_143012
"""

import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python check_archive.py <archive_folder>")
        sys.exit(1)

    folder = Path(sys.argv[1])
    manifest_path = folder / "manifest.json"

    if not manifest_path.exists():
        print(f"Error: {manifest_path} not found.")
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    module_items = [item for module in manifest.get("archive_results", []) for item in module.get("items", [])]
    module_failures = [item for item in module_items if not item.get("saved")]
    file_failures = [item for item in manifest.get("course_file_results", []) if not item.get("saved")]
    announcement_failures = [
        item for item in manifest.get("announcements_export", {}).get("announcements", [])
        if not item.get("saved")
    ]

    print(f"Archive folder: {folder}")
    print(f"  Module items:           {len(module_items)}")
    print(f"  Module item failures:   {len(module_failures)}")
    print(f"  Course file failures:   {len(file_failures)}")
    print(f"  Announcement failures:  {len(announcement_failures)}")

    if module_failures:
        print("\nFailed module items:")
        for item in module_failures:
            print(f"  - [{item.get('type', '?')}] {item.get('title', '?')}: {item.get('error', 'unknown')}")

    if file_failures:
        print("\nFailed course files:")
        for item in file_failures:
            print(f"  - {item.get('folder_path', '?')}/{item.get('display_name', '?')}: {item.get('error', 'unknown')}")

    if announcement_failures:
        print("\nFailed announcements:")
        for item in announcement_failures:
            print(f"  - {item.get('title', '?')}: {item.get('error', 'unknown')}")

    if not module_failures and not file_failures and not announcement_failures:
        print("\nAll items archived successfully.")


if __name__ == "__main__":
    main()
