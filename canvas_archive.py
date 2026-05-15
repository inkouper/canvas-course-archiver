"""
Canvas Course Archiver
======================
Archives a Canvas course for long-term offline preservation.

Dry run:
    python canvas_archive.py --course-url https://canvas.example.edu/courses/12345 --dry-run

Archive:
    python canvas_archive.py --course-url https://canvas.example.edu/courses/12345 --apply

Default archive folders are timestamped as canvas_archive_<course_id>_<YYYYMMDD_HHMMSS>.
The script opens a visible browser for Canvas login when no saved session exists.
It prefers the Canvas API for a complete module inventory, then falls back to the
rendered /modules page if the API is unavailable.
"""

import argparse
import asyncio
from datetime import datetime
from html import escape
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from playwright.async_api import Download, Page, async_playwright


DEFAULT_OUT_DIR_PREFIX = "canvas_archive"
ARCHIVE_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


@dataclass
class Config:
    base_url: str
    course_id: str
    out_dir: Path
    dry_run: bool
    apply: bool
    archived_course: bool
    headless: bool
    timeout_ms: int

    @property
    def course_url(self) -> str:
        return f"{self.base_url}/courses/{self.course_id}"

    @property
    def session_file(self) -> Path:
        return self.out_dir / "session.json"


def sanitize(name: str, max_len: int = 80) -> str:
    """Strip characters illegal in filenames."""
    s = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", name).strip(" .")
    s = re.sub(r"\s+", " ", s)
    return s[:max_len] or "untitled"


def item_type_from_url(url: str | None) -> str:
    href = url or ""
    if "/files/" in href:
        return "file"
    if "/assignments/" in href:
        return "assignment"
    if "/quizzes/" in href:
        return "quiz"
    if "/discussion_topics/" in href:
        return "discussion"
    if "/pages/" in href:
        return "page"
    if not href or href.startswith("#"):
        return "header"
    return "external"


def item_type_from_api(canvas_type: str | None, url: str | None) -> str:
    mapping = {
        "Assignment": "assignment",
        "Discussion": "discussion",
        "DiscussionTopic": "discussion",
        "ExternalTool": "external_tool",
        "ExternalUrl": "external",
        "File": "file",
        "Page": "page",
        "Quiz": "quiz",
        "SubHeader": "header",
    }
    return mapping.get(canvas_type or "", item_type_from_url(url))


def is_same_origin(url: str, cfg: Config) -> bool:
    parsed_url = urlparse(url)
    parsed_base = urlparse(cfg.base_url)
    return parsed_url.netloc == parsed_base.netloc


IMAGE_EXTENSIONS = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}


def image_extension_from_content_type(content_type: str | None) -> str:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    return IMAGE_EXTENSIONS.get(mime, ".img")


def is_supported_image_content_type(content_type: str | None) -> bool:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    return mime.startswith("image/")


def canvas_file_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    match = re.search(r"/files/(\d+)", parsed.path)
    if match:
        return match.group(1)
    query = parse_qs(parsed.query)
    for key in ("file_id", "id"):
        if query.get(key):
            return query[key][0]
    return None


async def localize_html_images(page: Page, dest: Path, cfg: Config) -> dict[str, Any]:
    """Download Canvas-hosted content images beside an HTML snapshot and rewrite img src values."""
    result: dict[str, Any] = {"found": 0, "saved": 0, "reused": 0, "skipped": 0, "failed": []}
    try:
        images = await page.evaluate(
            """() => {
                const scopes = [
                    ".user_content",
                    "[data-resource-type]",
                    "#content",
                    ".assignment-description",
                    ".description"
                ];
                const inScope = (node) => scopes.some((selector) => node.closest(selector));
                const nodes = Array.from(document.querySelectorAll("img")).filter(inScope);
                const seen = new Set();
                return nodes.map((node, index) => {
                    const raw = node.currentSrc || node.getAttribute("src") || "";
                    if (!raw || raw.startsWith("data:") || raw.startsWith("blob:")) return null;
                    const absolute = new URL(raw, document.baseURI).href;
                    const url = new URL(absolute);
                    const sameOrigin = url.origin === location.origin;
                    const isCanvasFile = /\\/files\\/\\d+/.test(url.pathname) || /\\/file_ref\\//.test(url.pathname);
                    if (!sameOrigin && !isCanvasFile) return null;
                    if (seen.has(absolute)) return null;
                    seen.add(absolute);
                    return {
                        index,
                        raw,
                        absolute,
                        api: node.getAttribute("data-api-endpoint") || ""
                    };
                }).filter(Boolean);
            }"""
        )
    except Exception as exc:
        result["failed"].append({"url": "", "error": f"image scan failed: {exc}"})
        return result

    result["found"] = len(images)
    if not images:
        return result

    assets_dir = dest.with_suffix("")
    assets_dir = assets_dir.parent / f"{assets_dir.name}_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    rewrites: dict[str, str] = {}
    local_by_url: dict[str, str] = {}
    used_names: set[str] = set()
    for image in images:
        url = image["absolute"]
        try:
            if url in local_by_url:
                rewrites[image["raw"]] = local_by_url[url]
                rewrites[url] = local_by_url[url]
                result["reused"] += 1
                continue

            response = await page.context.request.get(url, timeout=cfg.timeout_ms)
            if not response.ok:
                result["failed"].append({"url": url, "error": f"HTTP {response.status}"})
                continue
            content_type = response.headers.get("content-type")
            if not is_supported_image_content_type(content_type):
                result["failed"].append({"url": url, "error": f"Unsupported content type: {content_type or 'unknown'}"})
                continue
            body = await response.body()
            ext = image_extension_from_content_type(content_type)
            file_id = canvas_file_id_from_url(image.get("api") or url)
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
            filename = f"image_{file_id or digest}{ext}"
            while filename.lower() in used_names:
                filename = f"{Path(filename).stem}_{digest}{ext}"
            used_names.add(filename.lower())
            asset_path = assets_dir / filename
            asset_path.write_bytes(body)
            local_path = f"{assets_dir.name}/{filename}"
            local_by_url[url] = local_path
            rewrites[image["raw"]] = local_path
            rewrites[url] = local_path
            result["saved"] += 1
        except Exception as exc:
            result["failed"].append({"url": url, "error": str(exc)})

    if rewrites:
        await page.evaluate(
            """rewrites => {
                const scopes = [
                    ".user_content",
                    "[data-resource-type]",
                    "#content",
                    ".assignment-description",
                    ".description"
                ];
                const inScope = (node) => scopes.some((selector) => node.closest(selector));
                for (const node of Array.from(document.querySelectorAll("img")).filter(inScope)) {
                    const raw = node.currentSrc || node.getAttribute("src") || "";
                    const absolute = raw ? new URL(raw, document.baseURI).href : "";
                    const local = rewrites[raw] || rewrites[absolute];
                    if (!local) continue;
                    node.setAttribute("src", local);
                    node.removeAttribute("srcset");
                    node.removeAttribute("data-api-endpoint");
                    node.removeAttribute("data-api-returntype");
                }
            }""",
            rewrites,
        )

    return result


def quiz_kind_from_url(url: str, cfg: Config) -> str | None:
    """Classify a resolved top-level Canvas quiz URL."""
    parsed = urlparse(url)
    path = parsed.path
    lowered = url.lower()

    if "/courses/" in path and "/assignments/" in path:
        return "new"
    if "/courses/" in path and "/quizzes/" in path:
        return "classic"
    if "external_tools" in lowered or "new_quiz" in lowered or "new-quizzes" in lowered:
        return "new"
    if parsed.netloc and not is_same_origin(url, cfg):
        return "new"
    return None


def is_new_quiz_url(url: str, cfg: Config) -> bool:
    """Return True only for explicit New Quiz/LTI top-level URLs."""
    return quiz_kind_from_url(url, cfg) == "new"


def classic_quiz_questions_url(url: str, cfg: Config) -> str:
    """Build the Canvas Classic Quiz edit/questions tab URL."""
    parsed = urlparse(url)
    match = re.search(r"/courses/(\d+)/quizzes/(\d+)", parsed.path)
    if match:
        course_id, quiz_id = match.groups()
        return f"{cfg.base_url}/courses/{course_id}/quizzes/{quiz_id}/edit#tab-questions"

    clean_path = parsed.path.rstrip("/")
    if clean_path.endswith("/edit"):
        return parsed._replace(path=clean_path, query="", fragment="tab-questions").geturl()
    return parsed._replace(path=f"{clean_path}/edit", query="", fragment="tab-questions").geturl()


def classic_quiz_edit_url(url: str, cfg: Config) -> str:
    """Build the Canvas Classic Quiz edit URL without relying on a tab fragment."""
    parsed = urlparse(url)
    match = re.search(r"/courses/(\d+)/quizzes/(\d+)", parsed.path)
    if match:
        course_id, quiz_id = match.groups()
        return f"{cfg.base_url}/courses/{course_id}/quizzes/{quiz_id}/edit"

    clean_path = parsed.path.rstrip("/")
    if clean_path.endswith("/edit"):
        return parsed._replace(path=clean_path, query="", fragment="").geturl()
    return parsed._replace(path=f"{clean_path}/edit", query="", fragment="").geturl()


def with_query_param(url: str, key: str, value: Any) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query[key] = [str(value)]
    return parsed._replace(query=urlencode(query, doseq=True)).geturl()


def parse_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' not in part:
            continue
        match = re.search(r"<([^>]+)>", part)
        if match:
            return match.group(1)
    return None


def parse_course_url(course_url: str) -> tuple[str, str]:
    """Extract Canvas base URL and course id from a /courses/<id> URL."""
    parsed = urlparse(course_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("course URL must include scheme and host, such as https://canvas.example.edu/courses/12345")

    match = re.search(r"/courses/(\d+)(?:/|$)", parsed.path)
    if not match:
        raise ValueError("course URL must contain /courses/<course_id>")

    return f"{parsed.scheme}://{parsed.netloc}", match.group(1)


def archive_timestamp() -> str:
    """Return a timestamp safe for archive folder names."""
    return datetime.now().strftime(ARCHIVE_TIMESTAMP_FORMAT)


def default_archive_dir(course_id: str) -> Path:
    """Return a timestamped default archive folder for repeatable reruns."""
    return Path(f"{DEFAULT_OUT_DIR_PREFIX}_{course_id}_{archive_timestamp()}")


def archive_dir_from_out_dir(out_dir: Path) -> Path:
    """Treat --out-dir as a parent folder and create a timestamped archive inside it."""
    return out_dir / f"course_archive_{archive_timestamp()}"


async def wait_for_sso(page: Page, cfg: Config) -> None:
    """Open Canvas and pause until the user completes SSO."""
    print("Opening browser. Complete SSO login in the browser window.")
    await page.goto(cfg.course_url)
    await page.wait_for_url(f"**/courses/{cfg.course_id}**", timeout=180_000)
    print("Login detected.\n")


async def expand_all_modules(page: Page) -> None:
    """Click any collapsed module toggles so all items are visible."""
    toggles = await page.query_selector_all(".expand_module_link")
    for toggle in toggles:
        try:
            await toggle.click()
        except Exception:
            pass
    if toggles:
        await page.wait_for_timeout(1000)


async def api_get_all(page: Page, cfg: Config, path: str, params: dict[str, Any]) -> list[Any]:
    """Fetch all pages from a Canvas API collection."""
    query = urlencode(params, doseq=True)
    url = f"{cfg.base_url}{path}?{query}"
    results: list[Any] = []

    while url:
        resp = await page.request.get(url, timeout=cfg.timeout_ms)
        if not resp.ok:
            raise RuntimeError(f"Canvas API request failed {resp.status}: {url}")
        data = await resp.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            results.append(data)
        url = parse_next_link(resp.headers.get("link"))

    return results


async def parse_modules_api(page: Page, cfg: Config) -> list[dict[str, Any]]:
    """Return module/item structure using Canvas API."""
    modules_raw = await api_get_all(
        page,
        cfg,
        f"/api/v1/courses/{cfg.course_id}/modules",
        {"include[]": ["items"], "per_page": 100},
    )

    modules: list[dict[str, Any]] = []
    for mod in modules_raw:
        items = []
        for raw in mod.get("items", []):
            url = raw.get("html_url") or raw.get("external_url") or raw.get("url")
            if url and not url.startswith("http"):
                url = urljoin(cfg.base_url, url)
            items.append(
                {
                    "title": raw.get("title") or "Untitled item",
                    "url": url,
                    "type": item_type_from_api(raw.get("type"), url),
                    "canvas_type": raw.get("type"),
                    "content_id": raw.get("content_id"),
                    "module_item_id": raw.get("id"),
                }
            )
        modules.append(
            {
                "name": mod.get("name") or "Unnamed Module",
                "id": mod.get("id"),
                "items": items,
            }
        )
    return modules


async def get_new_quiz_assignments(page: Page, cfg: Config) -> dict[int, dict[str, Any]]:
    """Return New Quiz assignment items keyed by assignment id."""
    try:
        assignments = await api_get_all(
            page,
            cfg,
            f"/api/v1/courses/{cfg.course_id}/assignments",
            {"include[]": ["module_ids"], "per_page": 100},
        )
    except Exception as exc:
        print(f"New Quiz assignment lookup unavailable: {exc}")
        return {}

    lookup: dict[int, dict[str, Any]] = {}
    for assignment in assignments:
        submission_types = assignment.get("submission_types") or []
        html_url = assignment.get("html_url") or ""
        is_new_quiz = (
            assignment.get("is_quiz_assignment")
            or assignment.get("quiz_lti")
            or "external_tool" in submission_types
            or "online_quiz" in submission_types
        )
        if not is_new_quiz:
            continue

        info = {
            "assignment_id": assignment.get("id"),
            "html_url": html_url,
            "name": assignment.get("name"),
            "submission_types": submission_types,
        }
        if assignment.get("id"):
            lookup[int(assignment["id"])] = info
    return lookup


async def enrich_new_quiz_module_items(page: Page, cfg: Config, modules: list[dict[str, Any]]) -> None:
    """Mark assignment module items that are New Quizzes; leave Classic quiz items alone."""
    lookup = await get_new_quiz_assignments(page, cfg)
    if not lookup:
        return

    for module in modules:
        for item in module["items"]:
            if item.get("type") != "assignment":
                continue
            content_id = item.get("content_id")
            match = lookup.get(content_id) if isinstance(content_id, int) else None
            if not match or not match.get("html_url"):
                continue
            item["original_url"] = item.get("url")
            item["url"] = with_query_param(match["html_url"], "module_item_id", item["module_item_id"])
            item["resolved_click_url"] = item["url"]
            item["type"] = "new_quiz"
            item["canvas_type"] = "NewQuiz"
            item["new_quiz_assignment_id"] = match.get("assignment_id")


async def parse_modules_dom(page: Page, cfg: Config) -> list[dict[str, Any]]:
    """Return module/item structure from the rendered /modules page."""
    await page.goto(f"{cfg.course_url}/modules", wait_until="networkidle", timeout=cfg.timeout_ms)
    await expand_all_modules(page)

    modules = []
    for mod_el in await page.query_selector_all(".context_module"):
        name_el = await mod_el.query_selector(".ig-header-title, .name")
        mod_name = (await name_el.inner_text()).strip() if name_el else "Unnamed Module"

        items = []
        for row in await mod_el.query_selector_all(".ig-row"):
            link_el = await row.query_selector("a.item_link, .ig-title a, .ig-title")
            if not link_el:
                continue

            title = (await link_el.inner_text()).strip()
            href = await link_el.get_attribute("href") or ""
            if href and not href.startswith("http"):
                href = urljoin(cfg.base_url, href)

            items.append({"title": title, "url": href or None, "type": item_type_from_url(href)})

        modules.append({"name": mod_name, "items": items})

    return modules


async def parse_modules(page: Page, cfg: Config) -> tuple[list[dict[str, Any]], str]:
    """Prefer API inventory, fall back to DOM scraping."""
    try:
        modules = await parse_modules_api(page, cfg)
        await enrich_new_quiz_module_items(page, cfg, modules)
        return modules, "api"
    except Exception as exc:
        print(f"API module inventory failed; falling back to rendered page: {exc}")
        modules = await parse_modules_dom(page, cfg)
        return modules, "dom"


async def get_course_files_inventory(page: Page, cfg: Config) -> list[dict[str, Any]]:
    """Fetch course-level files so module links can be checked against all files."""
    try:
        folders = await api_get_all(
            page,
            cfg,
            f"/api/v1/courses/{cfg.course_id}/folders",
            {"per_page": 100},
        )
        files = await api_get_all(
            page,
            cfg,
            f"/api/v1/courses/{cfg.course_id}/files",
            {"per_page": 100},
        )
    except Exception as exc:
        print(f"Course file inventory unavailable: {exc}")
        return []

    folder_paths = {
        folder.get("id"): folder.get("full_name") or folder.get("name") or "course files"
        for folder in folders
    }
    normalized = []
    for file_info in files:
        folder_id = file_info.get("folder_id")
        normalized.append(
            {
                "id": file_info.get("id"),
                "display_name": file_info.get("display_name") or file_info.get("filename"),
                "filename": file_info.get("filename"),
                "url": file_info.get("url"),
                "folder_id": folder_id,
                "folder_path": folder_paths.get(folder_id, "course files"),
                "content-type": file_info.get("content-type"),
                "size": file_info.get("size"),
            }
        )
    return normalized


async def get_announcements_from_rendered_page(page: Page, cfg: Config) -> list[dict[str, Any]]:
    """Collect announcement links from the rendered Announcements page."""
    announcements_url = f"{cfg.course_url}/announcements"
    try:
        await page.goto(announcements_url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=cfg.timeout_ms)
        except Exception:
            await page.wait_for_timeout(3000)

        links = await page.locator("a[href]").evaluate_all(
            """anchors => anchors.map((anchor) => ({
                title: (anchor.innerText || anchor.textContent || "").trim(),
                url: anchor.href
            }))"""
        )
    except Exception as exc:
        print(f"Rendered announcement inventory unavailable: {exc}")
        return []

    seen = set()
    results = []
    announcement_pattern = re.compile(
        rf"/courses/{re.escape(cfg.course_id)}/(?:discussion_topics|announcements)/\d+"
    )
    for link in links:
        url = link.get("url") or ""
        if not announcement_pattern.search(url) or url in seen:
            continue
        seen.add(url)
        results.append({"title": link.get("title") or f"Announcement {len(results) + 1}", "url": url})
    return results


async def get_announcements_inventory(page: Page, cfg: Config) -> list[dict[str, Any]]:
    """Fetch course announcements and merge with links on the Announcements page."""
    api_announcements: list[dict[str, Any]] = []
    try:
        announcements = await api_get_all(
            page,
            cfg,
            "/api/v1/announcements",
            {"context_codes[]": [f"course_{cfg.course_id}"], "per_page": 100},
        )
        normalized = []
        for raw in announcements:
            ann_id = raw.get("id")
            url = raw.get("html_url") or raw.get("url")
            if not url and ann_id:
                url = f"{cfg.course_url}/discussion_topics/{ann_id}"
            if url and not url.startswith("http"):
                url = urljoin(cfg.base_url, url)
            normalized.append(
                {
                    "id": ann_id,
                    "title": raw.get("title") or f"Announcement {ann_id}",
                    "url": url,
                    "posted_at": raw.get("posted_at") or raw.get("created_at"),
                    "delayed_post_at": raw.get("delayed_post_at"),
                }
            )
        api_announcements = [announcement for announcement in normalized if announcement.get("url")]
        if not api_announcements:
            print("Announcement API returned no rows; checking the rendered Announcements page.")
    except Exception as exc:
        print(f"Announcement API inventory unavailable; falling back to rendered page: {exc}")

    rendered_announcements = await get_announcements_from_rendered_page(page, cfg)
    if rendered_announcements and len(rendered_announcements) > len(api_announcements):
        print(
            f"Rendered Announcements page found {len(rendered_announcements)} links; "
            f"API found {len(api_announcements)}."
        )

    merged: dict[str, dict[str, Any]] = {}
    for announcement in api_announcements:
        url = announcement.get("url")
        if url:
            merged[url] = announcement
    for announcement in rendered_announcements:
        url = announcement.get("url")
        if url:
            merged[url] = {**merged.get(url, {}), **announcement}

    return list(merged.values())


async def save_html(
    page: Page,
    url: str,
    dest: Path,
    cfg: Config,
    localize_images: bool = True,
    image_report: dict[str, Any] | None = None,
) -> bool:
    """Navigate to url and save full rendered HTML."""
    try:
        try:
            await page.goto(url, wait_until="networkidle", timeout=cfg.timeout_ms)
        except Exception:
            await page.goto(url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
            await page.wait_for_timeout(3000)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if localize_images:
            image_result = await localize_html_images(page, dest, cfg)
            if image_report is not None:
                image_report.update(image_result)
            if image_result["found"]:
                print(
                    f"    Images localized: {image_result['saved']}/{image_result['found']}"
                    + (f" ({image_result['reused']} reused)" if image_result.get("reused") else "")
                    + (f" ({len(image_result['failed'])} failed)" if image_result["failed"] else "")
                )
        dest.write_text(await page.content(), encoding="utf-8")
        return True
    except Exception as exc:
        print(f"    HTML save failed: {exc}")
        return False


def render_discussion_prompt_html(data: dict[str, Any], source_url: str) -> str:
    """Render only the initial Canvas discussion prompt as a standalone HTML snapshot."""
    title = data.get("title") or "Discussion prompt"
    body_html = data.get("body_html") or "<p>Discussion prompt body was not found.</p>"
    posted = data.get("posted_text") or ""
    resource_id = data.get("resource_id") or ""
    metadata = [f"Source: {escape(source_url)}"]
    if posted:
        metadata.append(f"Posted: {escape(posted)}")
    if resource_id:
        metadata.append(f"Canvas resource id: {escape(resource_id)}")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{escape(title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; line-height: 1.5; margin: 32px; color: #1f2933; }}
    main {{ max-width: 900px; margin: 0 auto; }}
    .source {{ color: #52606d; font-size: 13px; overflow-wrap: anywhere; }}
    img {{ max-width: 100%; height: auto; }}
  </style>
</head>
<body>
  <main>
    <h1>{escape(title)}</h1>
    <p class="source">{' | '.join(metadata)}</p>
    <article>
      {body_html}
    </article>
  </main>
</body>
</html>
"""


async def save_discussion_prompt_html(page: Page, url: str, dest: Path, cfg: Config) -> tuple[bool, dict[str, Any]]:
    """Save only the initial discussion prompt, excluding replies/thread rendering."""
    image_report: dict[str, Any] = {}
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
        except Exception:
            await page.wait_for_timeout(3000)

        ready_selector = (
            '[data-resource-type="discussion_topic.body"], '
            '.discussion-topic-message .user_content, '
            '[data-testid="discussion-topic-title"], '
            '.discussion-title, '
            'h1'
        )
        try:
            await page.wait_for_selector(ready_selector, timeout=min(cfg.timeout_ms, 15000))
        except Exception:
            await page.wait_for_timeout(1000)

        image_report.update(await localize_html_images(page, dest, cfg))
        if image_report.get("found"):
            print(
                f"    Images localized: {image_report['saved']}/{image_report['found']}"
                + (f" ({image_report['reused']} reused)" if image_report.get("reused") else "")
                + (f" ({len(image_report['failed'])} failed)" if image_report["failed"] else "")
            )

        data = await page.evaluate(
            """() => {
                const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
                const titleNode =
                    document.querySelector('[data-testid="discussion-topic-title"]') ||
                    document.querySelector('[data-testid="message_title"]') ||
                    document.querySelector(".discussion-title") ||
                    document.querySelector("h1");
                const body =
                    document.querySelector('[data-resource-type="discussion_topic.body"]') ||
                    document.querySelector(".discussion-topic-message .user_content") ||
                    Array.from(document.querySelectorAll(".user_content.enhanced, .user_content"))
                        .find((node) => {
                            const type = node.getAttribute("data-resource-type") || "";
                            return !type.includes("reply") && clean(node.innerText || node.textContent).length > 20;
                        });
                const postedNode =
                    document.querySelector("time") ||
                    document.querySelector(".posted_at") ||
                    document.querySelector(".discussion-pubdate");
                return {
                    title: clean(titleNode?.innerText || titleNode?.textContent || document.title),
                    body_html: body ? body.innerHTML.trim() : "",
                    body_text: body ? clean(body.innerText || body.textContent || "") : "",
                    posted_text: clean(postedNode?.innerText || postedNode?.textContent || ""),
                    resource_id: body ? body.getAttribute("data-resource-id") : null,
                    resource_type: body ? body.getAttribute("data-resource-type") : null
                };
            }"""
        )
        if not data.get("body_html"):
            data["body_html"] = "<p>Discussion prompt body was not found in the rendered page.</p>"

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_discussion_prompt_html(data, page.url), encoding="utf-8")
        return True, {"image_assets": image_report, "prompt_extracted": bool(data.get("body_text"))}
    except Exception as exc:
        print(f"    Discussion prompt save failed: {exc}")
        return False, {"image_assets": image_report, "error": str(exc), "prompt_extracted": False}


async def check_view_question_details(page: Page) -> None:
    """Turn on Canvas Classic Quiz question details when the checkbox exists."""
    detail_selectors = (
        "input#show_question_details",
        "input[name='show_question_details']",
        "input[type='checkbox'][aria-label*='question details' i]",
    )
    for selector in detail_selectors:
        box = page.locator(selector).first
        try:
            if await box.count() and not await box.is_checked():
                try:
                    await box.check(force=True)
                except Exception:
                    await box.evaluate(
                        """input => {
                            input.checked = true;
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                        }"""
                    )
                await page.wait_for_timeout(1000)
                return
        except Exception:
            pass

    labels = await page.locator("label").all()
    for label in labels:
        try:
            text = (await label.inner_text()).strip().lower()
            if "view question details" not in text and "show question details" not in text:
                continue
            label_for = await label.get_attribute("for")
            if label_for:
                box = page.locator(f"#{label_for}").first
                if await box.count() and not await box.is_checked():
                    await box.check(force=True)
                    await page.wait_for_timeout(1000)
                return
            await label.click()
            await page.wait_for_timeout(1000)
            return
        except Exception:
            pass


async def open_classic_quiz_questions_tab(page: Page, url: str, cfg: Config) -> str:
    """Open an active Classic Quiz edit page and move to the Questions tab."""
    target_url = classic_quiz_edit_url(url, cfg)
    await page.goto(target_url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
    try:
        await page.wait_for_selector("#quiz_edit_tabs, a[href='#tab-questions']", timeout=10_000)
    except Exception:
        await page.wait_for_timeout(500)

    tab_selectors = (
        "#quiz_edit_tabs a[href='#tab-questions']",
        "a[href='#tab-questions']",
        "[role='tab']:has-text('Questions')",
        "a:has-text('Questions')",
        "button:has-text('Questions')",
    )
    for selector in tab_selectors:
        tab = page.locator(selector).first
        try:
            if await tab.count():
                await tab.click(force=True)
                await page.wait_for_timeout(500)
                break
        except Exception:
            pass

    try:
        await page.evaluate(
            """() => {
                if (location.hash !== '#tab-questions') {
                    location.hash = 'tab-questions';
                    window.dispatchEvent(new HashChangeEvent('hashchange'));
                }
                const tab = document.querySelector("#quiz_edit_tabs a[href='#tab-questions'], a[href='#tab-questions']");
                if (tab) tab.click();
            }"""
        )
        await page.wait_for_timeout(500)
    except Exception:
        pass

    return page.url


async def wait_for_classic_quiz_questions(page: Page, cfg: Config) -> None:
    """Wait for Classic Quiz questions to appear without failing empty quizzes."""
    question_selectors = (
        ".question",
        ".quiz_question",
        ".display_question",
        ".question_holder",
        "[id^='question_']",
        "#questions",
        "#quiz_edit_tabs",
    )
    try:
        await page.wait_for_selector(",".join(question_selectors), timeout=cfg.timeout_ms)
    except Exception:
        await page.wait_for_timeout(3000)


async def extract_classic_quiz_fragment(page: Page) -> dict[str, str]:
    """Extract the useful Classic Quiz questions area for archival HTML."""
    return await page.evaluate(
        """() => {
            const pick = (...selectors) => {
                for (const selector of selectors) {
                    const node = document.querySelector(selector);
                    if (node) return node;
                }
                return document.body;
            };
            const title =
                document.querySelector("h1")?.innerText?.trim() ||
                document.querySelector("#quiz_title")?.innerText?.trim() ||
                document.title ||
                "Classic Quiz";
            const content = pick("#questions", "#tab-questions", "#quiz_edit_tabs", "#content", "main", "body");
            return { title, html: content.outerHTML, url: location.href };
        }"""
    )


async def next_classic_quiz_questions_page(page: Page):
    """Return a locator for a next pagination control on Classic Quiz questions."""
    selectors = (
        "a[rel='next']",
        "a.next_page:not(.disabled)",
        ".pagination a:has-text('Next')",
        "a:has-text('Next')",
        "button:has-text('Next')",
        "a[aria-label*='Next' i]",
        "button[aria-label*='Next' i]",
    )
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible() and await locator.is_enabled():
                disabled = (await locator.get_attribute("aria-disabled")) or ""
                classes = (await locator.get_attribute("class")) or ""
                if disabled.lower() == "true" or "disabled" in classes.lower():
                    continue
                return locator
        except Exception:
            pass
    return None


async def collect_classic_quiz_question_pages(page: Page, cfg: Config) -> list[dict[str, str]]:
    """Collect all paginated active Classic Quiz question pages with details expanded."""
    fragments = []
    seen_urls = set()

    for _ in range(100):
        await wait_for_classic_quiz_questions(page, cfg)
        await check_view_question_details(page)
        await wait_for_classic_quiz_questions(page, cfg)

        current_url = page.url
        if current_url not in seen_urls:
            fragments.append(await extract_classic_quiz_fragment(page))
            seen_urls.add(current_url)

        next_link = await next_classic_quiz_questions_page(page)
        if not next_link:
            break

        try:
            await next_link.click(force=True)
            try:
                await page.wait_for_load_state("networkidle", timeout=cfg.timeout_ms)
            except Exception:
                await page.wait_for_timeout(2000)
        except Exception:
            break

        if page.url in seen_urls:
            break

    return fragments


def render_classic_quiz_pages_html(title: str, pages: list[dict[str, str]]) -> str:
    """Render one or more collected Classic Quiz question pages into one archive file."""
    sections = []
    for idx, fragment in enumerate(pages, 1):
        sections.append(
            f"""
            <section class="quiz-page">
              <h2>Questions Page {idx}</h2>
              <p class="source">Source: {escape(fragment.get("url") or "")}</p>
              <div class="canvas-fragment">{fragment.get("html") or ""}</div>
            </section>
            """
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ margin: 0; font-family: Arial, Helvetica, sans-serif; color: #1f2933; background: #f6f7f9; }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 32px 24px 56px; }}
    h1 {{ margin: 0 0 24px; font-size: 28px; line-height: 1.2; }}
    h2 {{ margin: 0 0 8px; font-size: 20px; }}
    .source {{ color: #52606d; font-size: 13px; overflow-wrap: anywhere; }}
    .quiz-page {{ background: #fff; border: 1px solid #d9e2ec; border-radius: 6px; padding: 20px; margin: 18px 0; }}
    .answer.correct_answer, .correct_answer {{ border-color: #2f855a !important; background: #f0fff4 !important; }}
    .answer.correct_answer::before {{ content: "Correct answer"; display: inline-block; margin: 0 0 8px; color: #276749; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .02em; }}
    .answer_arrow[aria-label="Correct Answer"]::after {{ content: "Correct answer"; color: #276749; font-size: 12px; font-weight: 700; margin-right: 8px; }}
    img, video {{ max-width: 100%; height: auto; }}
    table {{ border-collapse: collapse; max-width: 100%; }}
    th, td {{ border: 1px solid #d9e2ec; padding: 6px 8px; }}
    @media print {{ body {{ background: #fff; }} main {{ max-width: none; padding: 0; }} .quiz-page {{ border: 0; break-inside: avoid; }} }}
  </style>
</head>
<body>
  <main>
    <h1>{escape(title)}</h1>
    {''.join(sections)}
  </main>
</body>
</html>
"""


async def save_classic_quiz(page: Page, url: str, dest: Path, cfg: Config) -> bool:
    """Save a Classic Quiz from the edit/questions tab with details expanded."""
    try:
        await open_classic_quiz_questions_tab(page, url, cfg)
        pages = await collect_classic_quiz_question_pages(page, cfg)
        if not pages:
            pages = [await extract_classic_quiz_fragment(page)]
        title = pages[0].get("title") or "Classic Quiz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_classic_quiz_pages_html(title, pages), encoding="utf-8")
        return True
    except Exception as exc:
        print(f"    Classic quiz save failed: {exc}")
        return False


async def open_archived_full_quiz(page: Page, cfg: Config) -> str:
    """Click Canvas's See full quiz control when available and return the final URL."""
    full_quiz = page.get_by_role("link", name=re.compile(r"see full quiz", re.I)).first
    if not await full_quiz.count():
        full_quiz = page.get_by_role("button", name=re.compile(r"see full quiz", re.I)).first
    if not await full_quiz.count():
        full_quiz = page.get_by_text(re.compile(r"see full quiz", re.I)).first

    if await full_quiz.count():
        await full_quiz.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=cfg.timeout_ms)
        except Exception:
            await page.wait_for_timeout(3000)
    else:
        print("    See full quiz control was not found; saving current quiz page.")

    return page.url


async def save_current_page_html(page: Page, dest: Path, cfg: Config) -> bool:
    """Save the currently loaded page as rendered HTML."""
    try:
        await wait_for_classic_quiz_questions(page, cfg)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(await page.content(), encoding="utf-8")
        return True
    except Exception as exc:
        print(f"    HTML save failed: {exc}")
        return False


async def save_page_mhtml(page: Page, url: str, dest: Path, cfg: Config) -> bool:
    """Navigate to a dynamic page and save it as a single-file MHTML snapshot."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
        await page.wait_for_load_state("networkidle", timeout=cfg.timeout_ms)
    except Exception:
        await page.wait_for_timeout(5000)

    return await save_loaded_page_mhtml(page, dest)


async def save_loaded_page_mhtml(page: Page, dest: Path) -> bool:
    """Save the currently loaded browser page as a single MHTML file."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        session = await page.context.new_cdp_session(page)
        snapshot = await session.send("Page.captureSnapshot", {"format": "mhtml"})
        dest.write_text(snapshot["data"], encoding="utf-8")
        return True
    except Exception as exc:
        print(f"    MHTML save failed: {exc}")
        return False


async def find_new_quiz_frame(page: Page):
    """Find the embedded New Quiz/LTI frame with meaningful quiz content."""
    for frame in page.frames:
        frame_url = frame.url.lower()
        if "quiz-lti" in frame_url or "new_quiz" in frame_url or "new-quizzes" in frame_url:
            return frame

    for frame in page.frames:
        try:
            text = (await frame.locator("body").inner_text(timeout=2000)).lower()
            if "new quizzes" in text or "show question navigator" in text:
                return frame
        except Exception:
            pass
    return None


async def open_new_quiz_print_key(page: Page, cfg: Config):
    """Open the New Quizzes print-key view and return its LTI frame."""
    try:
        try:
            await page.wait_for_load_state("networkidle", timeout=cfg.timeout_ms)
        except Exception:
            await page.wait_for_timeout(5000)

        quiz_frame = await find_new_quiz_frame(page)
        if not quiz_frame:
            print("    New Quiz frame not found.")
            return None

        try:
            await quiz_frame.evaluate(
                """() => {
                    window.__canvasArchivePrintEvents = [];
                    window.print = () => window.__canvasArchivePrintEvents.push("print");
                }"""
            )
        except Exception:
            pass

        more = quiz_frame.locator('[data-automation="sdk-more-quiz-actions-button"]').first
        if await more.count():
            await more.click(timeout=cfg.timeout_ms)
            await page.wait_for_timeout(1000)

        print_key = quiz_frame.locator('[data-automation="sdk-print-key-menuitem"]').first
        if await print_key.count():
            await print_key.click(timeout=cfg.timeout_ms, force=True)
            await page.wait_for_timeout(1500)
        else:
            print("    Print Key (With Answers) control was not found; saving visible New Quiz content.")

        return quiz_frame
    except Exception as exc:
        print(f"    New Quiz print-key setup failed: {exc}")
        return None


async def extract_new_quiz_answer_key(quiz_frame) -> dict[str, Any]:
    """Extract a stable, script-free answer key from a rendered New Quiz frame."""
    return await quiz_frame.evaluate(
        """() => {
            const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
            const htmlOf = (root, selector) => {
                const node = root.querySelector(selector);
                return node ? node.innerHTML.trim() : "";
            };
            const textOf = (root, selector) => clean(root.querySelector(selector)?.innerText || root.querySelector(selector)?.textContent || "");
            const labelHtml = (input) => {
                const id = input.getAttribute("id");
                const label = id ? input.ownerDocument.querySelector(`label[for="${CSS.escape(id)}"]`) : input.closest("label");
                if (!label) return clean(input.getAttribute("aria-label") || input.value);
                const userContent = label.querySelector(".user_content");
                return (userContent || label).innerHTML.trim();
            };
            const questions = Array.from(document.querySelectorAll('[data-automation="sdk-quiz-entry-show"]')).map((entry, index) => {
                const item = entry.querySelector('[data-automation="sdk-item-wrapper"]') || entry;
                const inputs = Array.from(item.querySelectorAll('input[type="checkbox"], input[type="radio"]'));
                return {
                    number: textOf(entry, '[data-automation="sdk-position-box-text"] [aria-hidden="true"]') || String(index + 1),
                    interaction_type: textOf(entry, '[data-automation="sdk-interaction-type-name-div"]'),
                    points: textOf(entry, '[data-automation="sdk-points-possible-div"]'),
                    prompt_html: htmlOf(item, 'legend .user_content') || htmlOf(item, '.user_content') || item.innerHTML.trim(),
                    answers: inputs.map((input) => ({
                        html: labelHtml(input),
                        checked: input.checked || input.hasAttribute("checked"),
                        type: input.type || ""
                    })),
                    fallback_html: inputs.length ? "" : item.innerHTML.trim()
                };
            });
            return {
                title: clean(document.querySelector('[data-automation="sdk-quiz-title-show-title"]')?.innerText || document.querySelector('[data-automation="sdk-quiz-title-show-wrapper"]')?.innerText || document.title || "New Quiz"),
                points: clean(document.querySelector('[data-automation="sdk-sidebar-closed"]')?.innerText || ""),
                source_frame_url: location.href,
                print_events: window.__canvasArchivePrintEvents || [],
                questions
            };
        }"""
    )


def render_new_quiz_answer_key_html(data: dict[str, Any], source_url: str) -> str:
    """Render extracted New Quiz data as standalone archival HTML."""
    title = data.get("title") or "New Quiz"
    question_blocks: list[str] = []
    for question in data.get("questions", []):
        answers = question.get("answers") or []
        if answers:
            answer_items = "\n".join(
                "<li class=\"answer{correct}\"><span class=\"marker\">{marker}</span><div>{answer}</div></li>".format(
                    correct=" correct" if answer.get("checked") else "",
                    marker="Correct answer" if answer.get("checked") else "Answer",
                    answer=answer.get("html") or "",
                )
                for answer in answers
            )
            answers_html = f"<ol class=\"answers\">{answer_items}</ol>"
        else:
            answers_html = f"<div class=\"fallback\">{question.get('fallback_html') or ''}</div>"

        meta = " | ".join(part for part in [question.get("interaction_type"), question.get("points")] if part)
        question_blocks.append(
            f"""
            <section class="question">
              <div class="question-meta">Question {escape(str(question.get("number") or ""))}{(" | " + escape(meta)) if meta else ""}</div>
              <div class="prompt">{question.get("prompt_html") or ""}</div>
              {answers_html}
            </section>
            """
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} - New Quiz Answer Key</title>
  <style>
    :root {{ color-scheme: light; font-family: Arial, Helvetica, sans-serif; }}
    body {{ margin: 0; background: #f6f7f9; color: #1f2933; }}
    main {{ max-width: 920px; margin: 0 auto; padding: 32px 24px 56px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; line-height: 1.2; }}
    .source {{ margin: 0 0 28px; color: #52606d; font-size: 13px; overflow-wrap: anywhere; }}
    .question {{ background: #fff; border: 1px solid #d9e2ec; border-radius: 6px; padding: 20px; margin: 18px 0; }}
    .question-meta {{ color: #52606d; font-size: 13px; font-weight: 700; margin-bottom: 12px; }}
    .prompt {{ font-size: 16px; line-height: 1.5; }}
    .answers {{ list-style: none; margin: 16px 0 0; padding: 0; display: grid; gap: 10px; }}
    .answer {{ display: grid; grid-template-columns: 112px 1fr; gap: 12px; border: 1px solid #d9e2ec; border-radius: 4px; padding: 10px 12px; }}
    .answer.correct {{ border-color: #2f855a; background: #f0fff4; }}
    .marker {{ color: #52606d; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .02em; }}
    .correct .marker {{ color: #276749; }}
    p {{ margin: 0 0 8px; }}
    p:last-child {{ margin-bottom: 0; }}
    img, video {{ max-width: 100%; height: auto; }}
    @media print {{
      body {{ background: #fff; }}
      main {{ max-width: none; padding: 0; }}
      .question {{ break-inside: avoid; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>{escape(title)}</h1>
    <p class="source">New Quiz answer-key archive. Source: {escape(source_url)} | Frame: {escape(data.get("source_frame_url") or "")}</p>
    {''.join(question_blocks)}
  </main>
</body>
</html>
"""


async def save_new_quiz_answer_key_html(page: Page, dest: Path, cfg: Config) -> bool:
    """Save a New Quiz as a standalone answer-key HTML file."""
    try:
        quiz_frame = await open_new_quiz_print_key(page, cfg)
        if not quiz_frame:
            return False

        data = await extract_new_quiz_answer_key(quiz_frame)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_new_quiz_answer_key_html(data, page.url), encoding="utf-8")
        raw_dest = dest.with_suffix(".raw.html")
        raw_dest.write_text(await quiz_frame.content(), encoding="utf-8")
        return True
    except Exception as exc:
        print(f"    New Quiz answer-key save failed: {exc}")
        return False


async def resolve_module_item_page(page: Page, url: str, cfg: Config) -> tuple[str, list[str]]:
    """Open a module item and return the resolved page URL plus frame URLs."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=cfg.timeout_ms)
        except Exception:
            await page.wait_for_timeout(3000)
    except Exception:
        await page.wait_for_timeout(3000)
    return page.url, [frame.url for frame in page.frames if frame.url]


async def archive_quiz(page: Page, item: dict[str, Any], dest_dir: Path, prefix: str, cfg: Config) -> dict[str, Any]:
    """Archive Classic and New Quizzes with course-state-aware handling."""
    title = item["title"]
    url = item.get("url")
    status = {"title": title, "url": url, "type": item["type"], "saved": False, "path": None}

    if not url:
        status["error"] = "Quiz item had no URL"
        return status

    resolved_url, frame_urls = await resolve_module_item_page(page, url, cfg)
    quiz_kind = quiz_kind_from_url(resolved_url, cfg) or quiz_kind_from_url(url, cfg) or "unknown"
    status["resolved_url"] = resolved_url
    status["frame_urls"] = frame_urls
    status["quiz_kind"] = quiz_kind

    if cfg.archived_course:
        if quiz_kind == "new":
            dest = dest_dir / f"{prefix}.html"
            saved = await save_new_quiz_answer_key_html(page, dest, cfg)
            status.update(
                {
                    "saved": saved,
                    "path": str(dest),
                    "quiz_kind": "new",
                    "quiz_archive_mode": "new_quiz_print_key_html",
                    "final_saved_url": page.url,
                    "final_frame_urls": [frame.url for frame in page.frames if frame.url],
                }
            )
            return status

        final_url = await open_archived_full_quiz(page, cfg)
        final_frame_urls = [frame.url for frame in page.frames if frame.url]
        final_kind = quiz_kind_from_url(final_url, cfg) or quiz_kind
        status["final_saved_url"] = final_url
        status["final_frame_urls"] = final_frame_urls

        if final_kind == "new":
            dest = dest_dir / f"{prefix}.html"
            saved = await save_new_quiz_answer_key_html(page, dest, cfg)
            status.update(
                {
                    "saved": saved,
                    "path": str(dest),
                    "quiz_kind": "new",
                    "quiz_archive_mode": "new_quiz_print_key_html",
                    "final_saved_url": page.url,
                }
            )
            return status

        dest = dest_dir / f"{prefix}.html"
        saved = await save_current_page_html(page, dest, cfg)
        status.update(
            {
                "saved": saved,
                "path": str(dest),
                "quiz_kind": "classic",
                "quiz_archive_mode": "archived_course_full_quiz_html",
                "final_saved_url": page.url,
            }
        )
        return status

    if quiz_kind == "new":
        dest = dest_dir / f"{prefix}.html"
        saved = await save_new_quiz_answer_key_html(page, dest, cfg)
        status.update({"saved": saved, "path": str(dest), "quiz_archive_mode": "new_quiz_print_key_html", "final_saved_url": page.url})
        return status

    dest = dest_dir / f"{prefix}.html"
    saved = await save_classic_quiz(page, resolved_url, dest, cfg)
    status.update({"saved": saved, "path": str(dest), "quiz_archive_mode": "classic_quiz_questions_html"})
    return status


async def download_canvas_file(page: Page, url: str, dest: Path, cfg: Config) -> bool:
    """Navigate to a Canvas file page and trigger its download."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)

        dl_sel = "a.download-btn, a[download], a[href*='download=1'], a[href*='/download']"
        dl_link = await page.query_selector(dl_sel)

        if dl_link:
            dl_href = await dl_link.get_attribute("href") or ""
            target_url = urljoin(cfg.base_url, dl_href) if dl_href else url
        else:
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            query["download_frd"] = ["1"]
            target_url = parsed._replace(query=urlencode(query, doseq=True)).geturl()

        async with page.expect_download(timeout=cfg.timeout_ms) as dl_info:
            await page.evaluate(f"window.location.href = {json.dumps(target_url)}")

        download: Download = await dl_info.value
        suggested = sanitize(download.suggested_filename or "")
        if suggested:
            dest = dest.with_name(dest.stem + Path(suggested).suffix)

        await download.save_as(dest)
        return True

    except Exception as first_exc:
        try:
            resp = await page.request.get(url, timeout=cfg.timeout_ms)
            if resp.ok:
                dest.write_bytes(await resp.body())
                return True
            print(f"    Download request failed: HTTP {resp.status}")
        except Exception as second_exc:
            print(f"    Download failed: {first_exc}; fallback failed: {second_exc}")
        return False


async def download_course_file(page: Page, file_info: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """Download a file from the Canvas Files inventory into the root course files folder."""
    filename = sanitize(file_info.get("display_name") or file_info.get("filename") or f"file_{file_info.get('id')}")
    dest = cfg.out_dir / "_course_files" / filename
    if dest.exists() and file_info.get("id"):
        dest = dest.with_name(f"{dest.stem}_{file_info.get('id')}{dest.suffix}")
    result = {
        "id": file_info.get("id"),
        "display_name": file_info.get("display_name"),
        "folder_path": file_info.get("folder_path"),
        "saved": False,
        "path": str(dest),
    }

    if cfg.dry_run:
        return result

    dest.parent.mkdir(parents=True, exist_ok=True)
    url = file_info.get("url")
    if not url:
        result["error"] = "Canvas file had no download URL"
        return result

    try:
        resp = await page.request.get(url, timeout=cfg.timeout_ms)
        if resp.ok:
            dest.write_bytes(await resp.body())
            result["saved"] = True
        else:
            result["error"] = f"HTTP {resp.status}"
    except Exception as exc:
        result["error"] = str(exc)

    return result


async def archive_announcements(page: Page, announcements: list[dict[str, Any]], cfg: Config) -> dict[str, Any]:
    """Save an announcement index and each individual announcement as extracted text."""
    announcements_url = f"{cfg.course_url}/announcements"
    dest_dir = cfg.out_dir / "_announcements"
    index_dest = dest_dir / "announcements_index.txt"
    result = {
        "url": announcements_url,
        "index_saved": False,
        "index_path": str(index_dest),
        "count": len(announcements),
        "announcements": [],
    }

    if cfg.dry_run:
        result["planned"] = True
        return result

    dest_dir.mkdir(parents=True, exist_ok=True)
    index_lines = [
        "Canvas announcements",
        f"Source: {announcements_url}",
        f"Count: {len(announcements)}",
        "",
    ]
    for idx, announcement in enumerate(announcements, 1):
        index_lines.extend(
            [
                f"{idx:02d}. {announcement.get('title') or f'Announcement {idx}'}",
                f"    URL: {announcement.get('url') or ''}",
            ]
        )
        if announcement.get("posted_at"):
            index_lines.append(f"    Posted: {announcement.get('posted_at')}")
    index_dest.write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    result["index_saved"] = True

    for idx, announcement in enumerate(announcements, 1):
        title = announcement.get("title") or f"Announcement {idx}"
        print(f"  Announcement {idx:02d}/{len(announcements)}: {title}", flush=True)
        dest = dest_dir / f"{idx:02d}_{sanitize(title)}.txt"
        item_result = await save_announcement_text(page, announcement, dest, cfg)
        result["announcements"].append(item_result)
        if not item_result.get("body_extracted"):
            print(f"    Announcement text fallback used: {item_result.get('error', 'body not found')}", flush=True)

    return result


async def save_announcement_text(page: Page, announcement: dict[str, Any], dest: Path, cfg: Config) -> dict[str, Any]:
    """Save a single announcement as extracted plain text."""
    title = announcement.get("title") or dest.stem
    url = announcement.get("url")
    result = {
        "title": title,
        "url": url,
        "posted_at": announcement.get("posted_at"),
        "saved": False,
        "path": str(dest),
        "body_extracted": False,
    }

    if not url:
        result["error"] = "Announcement had no URL"
        return result

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)

        body_selector = '[data-resource-type="announcement.body"]'
        announcement_ready_selector = (
            f"{body_selector}, "
            "[data-testid='message_title'], "
            "[data-testid='discussion-topic-title'], "
            ".discussion-title, "
            "h1"
        )
        targeted_timeout = min(cfg.timeout_ms, 12000)
        try:
            await page.wait_for_selector(announcement_ready_selector, timeout=targeted_timeout)
        except Exception:
            await page.wait_for_timeout(750)

        extracted = await page.evaluate(
            """() => {
                const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
                const body =
                    document.querySelector('span.user_content.enhanced[data-resource-type="announcement.body"]') ||
                    document.querySelector('[data-resource-type="announcement.body"]') ||
                    Array.from(document.querySelectorAll(".user_content.enhanced"))
                        .find((node) => clean(node.innerText || node.textContent).length > 20);
                const titleNode =
                    document.querySelector('[data-testid="message_title"] [aria-hidden="true"]') ||
                    document.querySelector('[data-testid="message_title"]') ||
                    document.querySelector("h1") ||
                    document.querySelector(".discussion-title") ||
                    document.querySelector("[data-testid='discussion-topic-title']");
                const postedNode =
                    document.querySelector(".discussion-entry-reply-area time") ||
                    document.querySelector("time") ||
                    document.querySelector(".posted_at") ||
                    document.querySelector(".discussion-pubdate");
                return {
                    page_title: clean(titleNode?.innerText || titleNode?.textContent || document.title),
                    posted_text: clean(postedNode?.innerText || postedNode?.textContent || ""),
                    body_html: body ? body.innerHTML.trim() : "",
                    body_text: body ? clean(body.innerText || body.textContent || "") : "",
                    resource_id: body ? body.getAttribute("data-resource-id") : null,
                    resource_type: body ? body.getAttribute("data-resource-type") : null
                };
            }"""
        )

        body_text = extracted.get("body_text") or ""
        if not body_text:
            body_text = "Announcement body was not found in the rendered page."
            result["error"] = "Announcement body selector was not found; saved fallback notice."

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            render_announcement_text(
                title=extracted.get("page_title") or title,
                source_url=url,
                body_text=body_text,
                posted_at=announcement.get("posted_at") or extracted.get("posted_text"),
                resource_id=extracted.get("resource_id"),
            ),
            encoding="utf-8",
        )
        result.update(
            {
                "saved": True,
                "body_extracted": bool(extracted.get("body_text")),
                "resource_id": extracted.get("resource_id"),
                "resource_type": extracted.get("resource_type"),
            }
        )
    except Exception as exc:
        result["error"] = str(exc)

    return result


def render_announcement_text(
    title: str,
    source_url: str,
    body_text: str,
    posted_at: str | None = None,
    resource_id: str | None = None,
) -> str:
    """Render extracted announcement content as plain archival text."""
    lines = [
        title,
        "=" * len(title),
        f"Source: {source_url}",
    ]
    if posted_at:
        lines.append(f"Posted: {posted_at}")
    if resource_id:
        lines.append(f"Canvas resource id: {resource_id}")
    lines.extend(["", body_text.strip(), ""])
    return "\n".join(lines)


async def export_rubrics(page: Page, cfg: Config) -> dict[str, Any]:
    """Export selected Canvas rubrics through the course Rubrics page."""
    rubrics_url = f"{cfg.course_url}/rubrics"
    result = {
        "url": rubrics_url,
        "saved": False,
        "path": None,
        "selected_count": 0,
    }

    if cfg.dry_run:
        result["planned"] = True
        return result

    try:
        await page.goto(rubrics_url, wait_until="networkidle", timeout=cfg.timeout_ms)
    except Exception:
        await page.goto(rubrics_url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
        await page.wait_for_timeout(3000)

    try:
        await page.wait_for_selector("[data-testid='saved-rubrics-table']", timeout=cfg.timeout_ms)
    except Exception:
        try:
            await page.wait_for_selector("input[data-testid^='rubric-select-checkbox-']", timeout=cfg.timeout_ms)
        except Exception:
            pass

    try:
        select_all = page.get_by_label(re.compile(r"select all", re.I)).first
        if await select_all.count() and await select_all.is_visible():
            await select_all.check()
            await page.wait_for_timeout(1000)
    except Exception:
        pass

    rubric_boxes = page.locator("input[data-testid^='rubric-select-checkbox-']")
    result["rubric_checkbox_count"] = await rubric_boxes.count()
    if not await rubric_boxes.count():
        rubric_boxes = page.locator("[data-testid='saved-rubrics-table'] input[type='checkbox']")
    if not await rubric_boxes.count():
        rubric_boxes = page.locator("table input[type='checkbox']")

    count = await rubric_boxes.count()
    selected = 0
    for idx in range(count):
        box = rubric_boxes.nth(idx)
        try:
            if not await box.is_enabled():
                continue
            if not await box.is_checked():
                try:
                    await box.check(force=True)
                except Exception:
                    await box.evaluate(
                        """input => {
                            input.checked = true;
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                        }"""
                    )
            if await box.is_checked():
                selected += 1
        except Exception:
            pass

    result["selected_count"] = selected
    if selected == 0:
        result["skipped"] = True
        result["error"] = "No selectable rubrics were found."
        return result

    await page.wait_for_timeout(1000)
    button_selectors = (
        "button:has-text('Download selected rubrics')",
        "[role='button']:has-text('Download selected rubrics')",
        "text=/Download selected rubrics/i",
    )
    button = page.get_by_role("button", name=re.compile(r"download selected rubrics", re.I)).first
    for selector in button_selectors:
        if await button.count():
            break
        button = page.locator(selector).first

    if not await button.count():
        result["error"] = "Could not find the Download selected rubrics button."
        return result

    dest_dir = cfg.out_dir / "_rubrics"
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        async with page.expect_download(timeout=cfg.timeout_ms) as dl_info:
            await button.click()
        download: Download = await dl_info.value
        filename = sanitize(download.suggested_filename or "rubrics")
        if "." not in filename:
            filename = f"{filename}.zip"
        dest = dest_dir / filename
        await download.save_as(dest)
        result.update({"saved": True, "path": str(dest)})
    except Exception as exc:
        result["error"] = str(exc)

    return result


async def archive_item(page: Page, item: dict[str, Any], dest_dir: Path, idx: int, cfg: Config) -> dict[str, Any]:
    """Archive a single module item."""
    title = item["title"]
    url = item.get("url")
    itype = item["type"]
    prefix = f"{idx:02d}_{sanitize(title)}"
    status = {"title": title, "url": url, "type": itype, "saved": False, "path": None}

    print(f"  {idx:02d}. [{itype:13s}] {title}")

    if cfg.dry_run:
        return status

    if itype == "header" or not url:
        note = dest_dir / f"{prefix}.txt"
        note.write_text(f"Section header: {title}\n", encoding="utf-8")
        status.update({"saved": True, "path": str(note)})
        return status

    if itype in {"quiz", "new_quiz"} or (itype == "external_tool" and url and is_new_quiz_url(url, cfg)):
        return await archive_quiz(page, item, dest_dir, prefix, cfg)

    if itype in {"external", "external_tool"}:
        note = dest_dir / f"{prefix}.txt"
        note.write_text(f"External link\nTitle: {title}\nURL:   {url}\n", encoding="utf-8")
        status.update({"saved": True, "path": str(note)})
        return status

    if itype == "file":
        ext = Path(urlparse(url).path).suffix or ".bin"
        dest = dest_dir / f"{prefix}{ext}"
        saved = await download_canvas_file(page, url, dest, cfg)
        status.update({"saved": saved, "path": str(dest)})
        return status

    if itype == "discussion":
        dest = dest_dir / f"{prefix}.html"
        saved, discussion_report = await save_discussion_prompt_html(page, url, dest, cfg)
        status.update({"saved": saved, "path": str(dest), "discussion_archive_mode": "prompt_only_html"})
        if discussion_report.get("image_assets"):
            status["image_assets"] = discussion_report["image_assets"]
        if "prompt_extracted" in discussion_report:
            status["prompt_extracted"] = discussion_report["prompt_extracted"]
        if discussion_report.get("error"):
            status["error"] = discussion_report["error"]
        return status

    dest = dest_dir / f"{prefix}.html"
    image_report: dict[str, Any] = {}
    saved = await save_html(page, url, dest, cfg, image_report=image_report)
    status.update({"saved": saved, "path": str(dest)})
    if image_report:
        status["image_assets"] = image_report
    return status


def summarize_inventory(modules: list[dict[str, Any]], files: list[dict[str, Any]]) -> dict[str, Any]:
    type_counts: dict[str, int] = {}
    module_file_ids = set()

    for module in modules:
        for item in module["items"]:
            type_counts[item["type"]] = type_counts.get(item["type"], 0) + 1
            if item["type"] == "file" and item.get("content_id"):
                module_file_ids.add(item["content_id"])

    all_file_ids = {f["id"] for f in files if f.get("id")}
    return {
        "modules": len(modules),
        "module_items": sum(len(m["items"]) for m in modules),
        "item_types": type_counts,
        "course_files": len(files),
        "course_files_not_linked_from_modules": len(all_file_ids - module_file_ids),
    }


async def run(cfg: Config) -> None:
    if not cfg.dry_run:
        cfg.out_dir.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=cfg.headless)

        ctx_opts = {}
        if cfg.session_file.exists():
            print("Resuming saved session. Delete session.json to force re-login.")
            ctx_opts["storage_state"] = str(cfg.session_file)

        context = await browser.new_context(accept_downloads=True, **ctx_opts)
        page = await context.new_page()

        if not cfg.session_file.exists():
            await wait_for_sso(page, cfg)
        else:
            await page.goto(cfg.course_url, wait_until="networkidle", timeout=cfg.timeout_ms)

        print("Building module inventory...")
        modules, source = await parse_modules(page, cfg)
        print(f"Found {len(modules)} modules via {source}.")

        print("Checking course-level file inventory...")
        files = await get_course_files_inventory(page, cfg)
        summary = summarize_inventory(modules, files)

        print("Checking announcements...")
        announcements = await get_announcements_inventory(page, cfg)
        summary["announcements"] = len(announcements)
        print(json.dumps(summary, indent=2))

        if cfg.dry_run:
            print("\nDry run archive plan:")
            for m_idx, module in enumerate(modules, 1):
                print(f"[{m_idx:02d}] {module['name']} ({len(module['items'])} items)")
                for i_idx, item in enumerate(module["items"], 1):
                    url_marker = " URL" if item.get("url") else " no-url"
                    print(f"  {i_idx:02d}. [{item['type']}] {item['title']}{url_marker}")
                print()
            module_file_ids = {
                item.get("content_id")
                for module in modules
                for item in module["items"]
                if item.get("type") == "file" and item.get("content_id")
            }
            unlinked_files = [file_info for file_info in files if file_info.get("id") not in module_file_ids]
            print("Course Files mirror plan:")
            print(f"  {len(files)} total course files under _course_files/")
            print(f"  {len(unlinked_files)} files are not direct module file items and would be missed by module-only crawling.")
            for file_info in unlinked_files:
                print(f"  - {file_info.get('folder_path')}/{file_info.get('display_name')}")
            print("\nRubrics export plan:")
            print("  Rubrics will be selected from the course Rubrics page and downloaded under _rubrics/.")
            print("\nAnnouncements archive plan:")
            print(f"  {len(announcements)} announcements will be saved under _announcements/.")
            print("\nQuiz archive mode:")
            if cfg.archived_course:
                print("  Archived course mode: Classic Quizzes use See full quiz HTML; New Quizzes use Print Key answer-key HTML.")
            else:
                print("  Active course mode: Classic Quizzes use edit/questions HTML; New Quizzes use Print Key answer-key HTML.")
            await browser.close()
            print("\nDry run complete. No archive files were written.")
            return

        manifest = {
            "course_url": cfg.course_url,
            "module_source": source,
            "summary": summary,
            "modules": modules,
            "course_files": files,
            "announcements": announcements,
            "archive_results": [],
        }
        (cfg.out_dir / "structure.json").write_text(
            json.dumps(modules, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        await save_html(page, f"{cfg.course_url}/modules", cfg.out_dir / "modules_index.html", cfg)
        print("Saved modules index page.\n")

        for m_idx, module in enumerate(modules, 1):
            mod_dir = cfg.out_dir / f"{m_idx:02d}_{sanitize(module['name'])}"
            mod_dir.mkdir(parents=True, exist_ok=True)
            print(f"[{m_idx}/{len(modules)}] {module['name']}")

            module_result = {"name": module["name"], "items": []}
            for i_idx, item in enumerate(module["items"], 1):
                try:
                    result = await archive_item(page, item, mod_dir, i_idx, cfg)
                except Exception as exc:
                    print(f"    Unexpected error: {exc}")
                    result = {**item, "saved": False, "error": str(exc)}
                module_result["items"].append(result)

            manifest["archive_results"].append(module_result)
            print()

        print("Archiving announcements...", flush=True)
        manifest["announcements_export"] = await archive_announcements(page, announcements, cfg)
        saved_announcements = [
            item for item in manifest["announcements_export"].get("announcements", []) if item.get("saved")
        ]
        print(
            f"  Saved announcements index and {len(saved_announcements)}/{len(announcements)} announcements under _announcements/."
        )

        print(
            f"Archiving full course Files area ({len(files)} files) before rubric export...",
            flush=True,
        )
        manifest["course_file_results"] = []
        for file_idx, file_info in enumerate(files, 1):
            print(f"  {file_idx:03d}/{len(files):03d}. {file_info.get('display_name')}", flush=True)
            manifest["course_file_results"].append(await download_course_file(page, file_info, cfg))

        print("Exporting rubrics...", flush=True)
        manifest["rubrics_export"] = await export_rubrics(page, cfg)
        if manifest["rubrics_export"].get("saved"):
            print(f"  Saved rubrics to {manifest['rubrics_export']['path']}")
        else:
            print(f"  Rubrics export did not save a file: {manifest['rubrics_export'].get('error', 'unknown reason')}")

        (cfg.out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        await context.storage_state(path=str(cfg.session_file))
        print(f"Session saved to {cfg.session_file}")

        await browser.close()

    print(f"\nArchive complete: {cfg.out_dir.resolve()}")


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Archive Canvas course modules and files.",
        epilog=(
            "The script can be run from any folder. If --out-dir is omitted, "
            "the timestamped archive folder is created in the current working directory. "
            "If --out-dir is provided, it is treated as a parent folder and a "
            "course_archive_<YYYYMMDD_HHMMSS> subfolder is created inside it."
        ),
    )
    target = parser.add_argument_group("course target")
    target.add_argument(
        "--course-url",
        help="Full Canvas course URL, for example https://canvas.example.edu/courses/12345.",
    )
    target.add_argument(
        "--base-url",
        help="Canvas host URL, for example https://canvas.example.edu. Use with --course-id.",
    )
    target.add_argument("--course-id", help="Canvas numeric course id. Use with --base-url.")

    parser.add_argument(
        "--out-dir",
        type=Path,
        help=(
            "Parent directory for the archive. The script creates a "
            "course_archive_<YYYYMMDD_HHMMSS> subfolder inside it. If omitted, "
            "defaults to canvas_archive_<course_id>_<YYYYMMDD_HHMMSS> in the current working directory."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Inventory only; do not write archive files.")
    parser.add_argument("--apply", action="store_true", help="Write the archive to disk.")
    parser.add_argument(
        "--archived-course",
        action="store_true",
        help="Use archived-course quiz handling: Classic Quizzes use 'See full quiz'; New Quizzes use Print Key answer-key HTML.",
    )
    parser.add_argument("--headless", action="store_true", help="Run browser headlessly when a saved session exists.")
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    args = parser.parse_args()

    if args.dry_run and args.apply:
        parser.error("--dry-run and --apply cannot be used together.")
    if not args.dry_run and not args.apply:
        parser.error("choose either --dry-run or --apply.")

    if args.course_url:
        try:
            base_url, course_id = parse_course_url(args.course_url)
        except ValueError as exc:
            parser.error(str(exc))
        if args.base_url or args.course_id:
            parser.error("--course-url cannot be combined with --base-url or --course-id.")
    else:
        if not args.base_url or not args.course_id:
            parser.error("provide --course-url, or provide both --base-url and --course-id.")
        base_url = args.base_url.rstrip("/")
        course_id = args.course_id

    out_dir = archive_dir_from_out_dir(args.out_dir) if args.out_dir else default_archive_dir(course_id)
    return Config(
        base_url=base_url,
        course_id=course_id,
        out_dir=out_dir,
        dry_run=args.dry_run,
        apply=args.apply,
        archived_course=args.archived_course,
        headless=args.headless,
        timeout_ms=args.timeout_ms,
    )


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
