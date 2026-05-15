# Canvas Course Archiver

Archive a Canvas course for long-term offline preservation.

The script logs into Canvas with Playwright, inventories the course modules, saves rendered HTML snapshots for module pages and activities, localizes Canvas-hosted images used in those HTML pages, records external links, and downloads Canvas files.
It also downloads the full Canvas Files area so files that are not linked directly from modules are still preserved.

## Structure

- `structure.json`: module and module-item inventory from Canvas.
- `modules_index.html`: rendered snapshot of the Canvas Modules page.
- `manifest.json`: archive summary and per-item save status.
- `01_Module_Name/`, `02_Module_Name/`, etc.: module folders containing snapshots, files, section-header notes, external-link notes, and any per-page image asset folders in module order.
- `*_assets/`: image folders created next to saved HTML pages when Canvas-hosted content images are found. The HTML is rewritten to use these local files instead of authenticated Canvas preview URLs.
- `_announcements/`: text index plus individual announcement text files extracted from Canvas announcement bodies.
- `_course_files/`: Canvas Files downloads, stored directly in this folder, including files not directly linked from modules.
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
  --out-dir "C:/Users/you/Documents/Course Archives"
```

When `--out-dir` is provided, it is treated as a parent destination folder. The script creates a timestamped subfolder inside it, such as `course_archive_20260514_143012/`.

If you omit `--out-dir`, the archive is created in the current working directory, using a timestamped folder name such as `canvas_archive_12345_20260514_143012/`.

Keeping the script/repo separate from generated archive folders is recommended, especially if you plan to publish the code on GitHub. Archive outputs and `session.json` should stay private unless you intentionally choose to share them.

## How to Use

> **Windows note:** The examples below use `\` to split long commands across lines (a bash/macOS convention). On Windows, type the command on a single line or use `` ` `` for line continuation in PowerShell.

> **Running from another directory:** Python must be able to find the script file. If your terminal is not in the same folder as `canvas_archive.py`, provide the full path to the script:
>
> ```bash
> python "C:/path/to/canvas-course-archiver/canvas_archive.py" --course-url ... --apply --out-dir ./archives
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

By default, output goes to a timestamped folder in the current working directory:

```text
canvas_archive_<course_id>_<YYYYMMDD_HHMMSS>/
```

For example, course `12345` might archive to `canvas_archive_12345_20260514_143012/`.

Use `--out-dir` to choose a parent folder. The script creates a timestamped `course_archive_<YYYYMMDD_HHMMSS>/` subfolder inside that parent:

```bash
python canvas_archive.py --course-url https://canvas.example.edu/courses/12345 --apply --out-dir "C:/Users/you/Documents/Course Archives"
```

This keeps reruns separate and avoids accidentally mixing an old archive with a new one.

## Using An AI Assistant

You can use a local AI coding assistant, such as OpenAI Codex, Claude Cowork, or a similar tool, to help set up, run, inspect, and troubleshoot this project. The assistant should work in a local workspace where it can see `canvas_archive.py`, `README.md`, and `check_archive.py`.

A good assistant workflow is:

1. Ask the assistant to inspect the project first:

```text
Review this Canvas archival project. Confirm the main script, README, and check script are present, and summarize how to run it.
```

2. Ask it to check the environment before archiving:

```text
Check whether Python 3.10+ and Playwright are available. If Playwright is missing, tell me the install commands before running anything.
```

3. Install dependencies if needed:

```bash
pip install playwright
playwright install chromium
```

4. Run a dry run first:

```text
Run a dry run for this Canvas course:
python canvas_archive.py --course-url https://canvas.example.edu/courses/12345 --dry-run
```

5. Ask the assistant to summarize the dry-run output. Confirm the module count, item types, file count, announcements, and quiz handling look reasonable.

6. Run the full archive:

```text
Run the archive with --apply and save it under this parent folder:
python canvas_archive.py --course-url https://canvas.example.edu/courses/12345 --apply --out-dir "C:/path/to/archive parent"
```

7. Complete Canvas login in the browser window when it opens. Leave the browser and terminal running until the final `Archive complete` message appears.

8. Ask the assistant to check the finished archive:

```text
Run check_archive.py on the new archive folder and summarize any failures.
```

Here is a compact prompt you can reuse:

```text
I want to archive a Canvas course using this project. First inspect the README and script. Then help me run a dry run. If the dry run looks good, run the archive with --apply. Do not delete or overwrite previous archives. Use the timestamped output folder created by the script. After the run, check manifest.json with check_archive.py and summarize any failures.
```

Important assistant-use notes:

- Keep archive folders separate from the code repository.
- Do not share, upload, or commit `session.json`.
- Do not ask the assistant to upload archived course materials unless you are sure you have permission.
- If the run appears slow near the end, check whether it is downloading `_course_files/`; that phase is expected.
- The assistant may need permission to open a browser, access Canvas, or write to the selected archive folder.

## Login

The first run opens a visible Chromium browser. Complete your normal Canvas login, including SSO or multi-factor authentication. After login, the script saves `session.json` in the archive folder.

Because each run creates a fresh timestamped archive folder, it may ask you to log in again. To reuse a previous browser session, copy a trusted `session.json` from an earlier archive into the new archive folder before running, or run once interactively and let the script save a new session.

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

Expect a full course archive to take roughly 20 minutes, depending on course size, network speed, Canvas response time, and the number of files. Near the end, the script may appear to spend a long time before exporting rubrics because it is downloading the full Canvas Files inventory into `_course_files/`. This is normal. The console prints progress like `001/431`, `002/431`, and so on during that phase. Rubrics are exported only after the course-files phase finishes.

Quiz items get special handling:

- In active courses, Classic Quizzes are identified by `/quizzes/...`, opened on the edit page, moved to the Questions tab by clicking the tab instead of relying only on the URL fragment, and saved as HTML. The script tries to enable "view question details" on each questions page, follows quiz-question pagination so quizzes with more than 25 questions are captured across pages, and adds archive CSS so Canvas `correct_answer` markers are visibly highlighted.
- New Quizzes are identified by `/assignments/...` or explicit LTI URLs. The script opens the New Quiz iframe, uses Canvas's "Print Key (With Answers)" action to reveal checked correct answers, and saves a clean standalone answer-key HTML file. It also writes a `.raw.html` sidecar with the rendered iframe DOM for troubleshooting.
- In archived courses, use `--archived-course`. The script opens each quiz and clicks "See full quiz" when available. Classic Quizzes are saved from that view as HTML; New Quizzes are saved through the Print Key answer-key HTML path.

Announcements are archived after module items. The script checks both the Canvas announcements API and the rendered Announcements page, because active courses may show more announcements in the clickable page list than the API returns. It does not use Canvas's bulk selection/download controls. Instead, it writes `_announcements/announcements_index.txt`, clicks through each individual announcement link, and extracts plain text from Canvas's `announcement.body` user-content element. Each announcement is saved as a `.txt` file. The script does not save raw full-page announcement HTML sidecars.

For normal HTML snapshots such as assignments, discussions, and pages, the script looks for Canvas-hosted content images inside user-content areas. It downloads those images into a neighboring `*_assets/` folder and rewrites the HTML to use relative local paths. This is meant to preserve images whose original URLs look like `/courses/<course_id>/files/<file_id>/preview` and would otherwise require an active Canvas login.

The neighboring `*_assets/` folders intentionally duplicate some image files that may also appear in `_course_files/`. The two locations serve different archival purposes: `_course_files/` preserves the full Canvas Files inventory, while page-local `*_assets/` folders preserve offline viewing of each saved HTML snapshot without requiring fragile links into a global file bucket.

Discussion module items are saved as prompt-only HTML. The script extracts the initial discussion topic body and omits student replies and thread rendering. This keeps the course-material archive focused on instructor-provided prompts rather than participation records.

Rubrics are exported after module items, announcements, and the full course-files download. The script opens the course Rubrics page, waits for the saved-rubrics table, selects rubric row checkboxes matching `rubric-select-checkbox-*`, and clicks "Download selected rubrics". The result is saved under `_rubrics/` and recorded in `manifest.json`.

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
- Assignment, quiz, and discussion pages are saved as rendered HTML. Discussion pages are prompt-only snapshots. Canvas-hosted content images in normal HTML snapshots are downloaded beside the page and linked locally when possible. Dynamic interactions, submissions, grading tools, comments, and some embedded apps may not work offline.
- Announcement export depends on access to the course Announcements page or Canvas announcements API, and on Canvas rendering the `announcement.body` user-content element.
- Classic Quiz question export requires an account that can access the quiz edit/questions tab.
- Archived Canvas courses should be run with `--archived-course`; otherwise the active-course quiz edit route may fail or save incomplete quiz pages.
- New Quiz output depends on the LTI iframe rendering and exposing the "Print Key (With Answers)" menu item to your Canvas account.
- Rubric export depends on the course Rubrics page exposing selectable rubrics and the "Download selected rubrics" button.
- External links are recorded as text files but are not downloaded.
- Embedded media hosted inside or outside Canvas may require separate preservation. The image localization pass is focused on rendered `<img>` elements in Canvas user-content areas, not video/audio elements, script-loaded media objects, or external embeds.
- Access is limited to what your Canvas account can see.

## Recommended Workflow

1. Run `--dry-run`.
2. Review item counts and the course-files mirror plan.
3. Run `--apply`; let the default timestamped folder keep the run separate.
4. Inspect `manifest.json` for failures.
5. Open `modules_index.html`, `_announcements/announcements_index.txt`, and a few saved pages locally.
6. Store the archive in long-term storage.
7. Keep `session.json` private or remove it after archiving.

During `--apply`, let the browser and terminal continue running until the final `Archive complete` message appears. The last major work phase is usually the `_course_files/` download, which can take several minutes by itself on courses with many PDFs, images, and videos.

## License

MIT
