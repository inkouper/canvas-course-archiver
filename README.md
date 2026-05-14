# Canvas Course Archiver

Archive a Canvas course for long-term offline preservation.

The script logs into Canvas with Playwright, inventories the course modules, saves rendered HTML snapshots for module pages, activities, and announcements, records external links, and downloads Canvas files into a mirrored folder structure.
It also mirrors the full Canvas Files area so files that are not linked directly from modules are still preserved.

## Structure

- `structure.json`: module and module-item inventory from Canvas.
- `modules_index.html`: rendered snapshot of the Canvas Modules page.
- `manifest.json`: archive summary and per-item save status.
- `01_Module_Name/`, `02_Module_Name/`, etc.: module folders containing snapshots, files, section-header notes, and external-link notes in module order.
- `_announcements/`: text index plus individual announcement text files extracted from Canvas announcement bodies.
- `_course_files/`: mirrored Canvas Files area, including files not directly linked from modules.
- `_rubrics/`: downloaded rubric export, when the course Rubrics page provides the export button.
- `session.json`: local Playwright login state for re-runs.

## Requirements

- Python 3.10 or newer
- Access to the Canvas course in a browser
- Playwright

Install dependencies:

```bash
pip install playwright
playwright install chromium
```

## Where It Can Run

This is a normal Python command-line script. You can run it from:

- a terminal or command prompt;
- VS Code, PyCharm, or another Python IDE;
- a local Python virtual environment.

The only requirements are Python 3.10 or newer, Playwright, Chromium installed with `playwright install chromium`, and network/browser access to Canvas.

Because the script opens a browser for login, it works best in an environment with a visible desktop browser. Headless runs are only recommended after `session.json` has already been created by a successful login.

## Where to Put the Script

The script does not need to live inside the folder where you want to save a course archive. A good setup is to keep the GitHub repo in a normal tools or projects folder, then choose the archive destination with `--out-dir`.

For example:

```bash
git clone https://github.com/YOUR-USERNAME/canvas-course-archiver.git
cd canvas-course-archiver
```

Then run the script, pointing it at your course and choosing where to save the archive:

```bash
python canvas_archive.py \
  --course-url https://canvas.example.edu/courses/12345 \
  --apply \
  --out-dir "C:/Users/you/Documents/Course Archives/canvas_archive_12345"
```

If you omit `--out-dir`, the archive is created in the current working directory, using a timestamped folder name such as `canvas_archive_12345_20260513_143012/`.

Keeping the script/repo separate from generated archive folders is recommended, especially if you plan to publish the code on GitHub. Archive outputs and `session.json` should stay private unless you intentionally choose to share them.

## How to Use

> **Windows note:** The examples below use `\` to split long commands across lines (a bash/macOS convention). On Windows, type the command on a single line or use `` ` `` for line continuation in PowerShell.

> **Running from another directory:** Python must be able to find the script file. If your terminal is not in the same folder as `canvas_archive.py`, provide the full path to the script:
>
> ```bash
> python "C:/path/to/canvas-course-archiver/canvas_archive.py" --course-url ... --apply --out-dir ./my_archive
> ```
>
> Using just `python canvas_archive.py` from a different folder will fail with "can't open file" or "can't find `__main__` module" because Python looks for the script relative to your current working directory. The `--out-dir` flag controls where the archive is saved, independent of where the script lives.

Run a safe inventory first:

```bash
python canvas_archive.py --course-url https://canvas.example.edu/courses/12345 --dry-run
```

Then run the archive:

```bash
python canvas_archive.py --course-url https://canvas.example.edu/courses/12345 --apply
```

If the course is archived in Canvas, add `--archived-course` so Classic Quizzes are saved through Canvas's "See full quiz" view. 
New Quizzes are opened through Canvas's "Print Key (With Answers)" command and saved as standalone answer-key HTML:

```bash
python canvas_archive.py --course-url https://canvas.example.edu/courses/12345 --apply --archived-course
```

You can also provide the Canvas host and course id separately:

```bash
python canvas_archive.py --base-url https://canvas.example.edu --course-id 12345 --dry-run
python canvas_archive.py --base-url https://canvas.example.edu --course-id 12345 --apply
```

By default, output goes to a timestamped folder:

```text
canvas_archive_<course_id>_<YYYYMMDD_HHMMSS>/
```

For example, course `12345` might archive to `canvas_archive_12345_20260513_143012/`. This keeps reruns separate and avoids accidentally mixing an old archive with a new one.

Use `--out-dir` to choose a specific folder:

```bash
python canvas_archive.py --course-url https://canvas.example.edu/courses/12345 --apply --out-dir my_course_archive
```

If you reuse the same `--out-dir`, existing files with the same generated names may be overwritten. Use the default timestamped folder for the cleanest reruns.

## Login

The first run opens a visible Chromium browser. Complete your normal Canvas login, including SSO or multi-factor authentication. After login, the script saves `session.json` in the archive folder.

Because the default archive folder is timestamped, each default run starts with a fresh output folder and may ask you to log in again. To reuse a previous browser session, run with the same `--out-dir` as a previous successful run or copy that run's `session.json` into the new archive folder before running.

If login stops working or you need to switch accounts, delete `session.json` and run again.

## Dry Run

`--dry-run` does not write archive files. It logs:

- number of modules and module items;
- counts by item type;
- number of files in the Canvas Files area;
- number of announcements;
- files that are not direct module file items and would be missed by module-only crawling;
- a note that announcements will be saved during `--apply`;
- a note that rubrics will be exported during `--apply`;
- the planned archive order.

Use this before `--apply`, especially when archiving a large course.

## Applying the Archive

`--apply` writes the archive to disk. After it finishes, check `manifest.json` for failures. A healthy run should have no failed module items and no failed course files.

Quiz items get special handling:

- In active courses, Classic Quizzes are identified by `/quizzes/...`, opened on `/edit#tab-questions`, the script tries to enable "view question details", and the expanded questions page is saved as HTML.
- New Quizzes are identified by `/assignments/...` or explicit LTI URLs. The script opens the New Quiz iframe, uses Canvas's "Print Key (With Answers)" action to reveal checked correct answers, and saves a clean standalone answer-key HTML file. It also writes a `.raw.html` sidecar with the rendered iframe DOM for troubleshooting.
- In archived courses, use `--archived-course`. The script opens each quiz and clicks "See full quiz" when available. Classic Quizzes are saved from that view as HTML; New Quizzes are saved through the Print Key answer-key HTML path.

Announcements are archived after module items. The script writes `_announcements/announcements_index.txt`, then opens each individual announcement link and extracts plain text from Canvas's `announcement.body` user-content element. Each announcement is saved as a `.txt` file. The script does not save raw full-page announcement HTML sidecars.

Rubrics are exported after module items and course files. The script opens the course Rubrics page, waits for the saved-rubrics table, selects rubric row checkboxes matching `rubric-select-checkbox-*`, and clicks "Download selected rubrics". The result is saved under `_rubrics/` and recorded in `manifest.json`.

A quick integrity check (replace the folder name with your actual archive folder):

```bash
python check_archive.py canvas_archive_12345_20260513_143012
```

Or manually in Python:

```python
import json, sys
from pathlib import Path

folder = sys.argv[1] if len(sys.argv) > 1 else "canvas_archive_12345_20260513_143012"
manifest = json.loads(Path(folder, "manifest.json").read_text(encoding="utf-8"))

module_items = [item for module in manifest["archive_results"] for item in module["items"]]
module_failures = [item for item in module_items if not item.get("saved")]
file_failures = [item for item in manifest.get("course_file_results", []) if not item.get("saved")]
announcement_failures = [
    item for item in manifest.get("announcements_export", {}).get("announcements", [])
    if not item.get("saved")
]

print("module items:", len(module_items))
print("module failures:", len(module_failures))
print("course file failures:", len(file_failures))
print("announcement failures:", len(announcement_failures))
```

## Security And Privacy

Do not commit archive outputs unless you are sure you have the right to share the course materials.

Never commit `session.json`. It contains browser session state that may grant access to Canvas. The included `.gitignore` excludes common archive outputs, session files, and Python cache files.

Also review archived HTML before sharing. Course pages may contain student names, assignment details, hidden comments, links, analytics IDs, or institution-specific markup.

## Limitations

- The archive is a preservation snapshot, not a full Canvas replacement.
- Assignment, quiz, and discussion pages are saved as rendered HTML. Dynamic interactions, submissions, grading tools, comments, and some embedded apps may not work offline.
- Announcement export depends on access to the course Announcements page or Canvas announcements API, and on Canvas rendering the `announcement.body` user-content element.
- Classic Quiz question export requires an account that can access the quiz edit/questions tab.
- Archived Canvas courses should be run with `--archived-course`; otherwise the active-course quiz edit route may fail or save incomplete quiz pages.
- New Quiz output depends on the LTI iframe rendering and exposing the "Print Key (With Answers)" menu item to your Canvas account.
- Rubric export depends on the course Rubrics page exposing selectable rubrics and the "Download selected rubrics" button.
- External links are recorded as text files but are not downloaded.
- Embedded media hosted outside Canvas may require separate preservation.
- Access is limited to what your Canvas account can see.

## Recommended Workflow

1. Run `--dry-run`.
2. Review item counts and the course-files mirror plan.
3. Run `--apply`; let the default timestamped folder keep the run separate.
4. Inspect `manifest.json` for failures.
5. Open `modules_index.html`, `_announcements/announcements_index.txt`, and a few saved pages locally.
6. Store the archive in long-term storage.
7. Keep `session.json` private or remove it after archiving.

## License

MIT
