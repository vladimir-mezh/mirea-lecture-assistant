"""Reading webinar links out of the MIREA Moodle «Вебинары по дисциплине» page.

The page renders one table per discipline; each row carries machine-readable
values that are far more reliable than the visible text: the start and end cells
hold unix timestamps in `data-val`, and the groups cell holds one
`span.group-tag` per group. Matching therefore happens on timestamps and group
codes, and only falls back to comparing names.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import urljoin

log = logging.getLogger(__name__)

WEBINAR_MODULE_RE = re.compile(r"/mod/webinars/view\.php\?id=\d+")
GROUP_RE = re.compile(r"[A-ZА-ЯЁ]{2,6}-\d{2}-\d{2}", re.IGNORECASE)
SSO_LINK_RE = re.compile(r"auth/oidc|auth/oauth2|/sso|keycloak|oidc", re.IGNORECASE)
LOGIN_PAGE_MARKERS = (
    "loginform",
    "login/index.php",
    "вход в систему",
    "log in to the site",
    "max-account-config",
    "подтверждение через макс",
)
# A finished webinar offers its recording; joining a lesson must never land there.
RECORDING_RE = re.compile(r"запис|record|playback", re.IGNORECASE)
# Some disciplines keep every lecture in one module, others add a module per lecture.
MAX_MODULE_PAGES = 30
# Module group ranks: this group, no group named, other groups only.
OWN_GROUP, NO_GROUP, OTHER_GROUP = 0, 1, 2
COURSES_URL = "https://online-edu.mirea.ru/my/courses.php"
COURSE_LINK_RE = re.compile(r"/course/view\.php\?id=\d+")
# How far a webinar's scheduled start may sit from the lesson's start.
DEFAULT_TOLERANCE = timedelta(minutes=20)
# A webinar whose scheduled end passed this long ago is over, not the room to join.
FINISHED_GRACE = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class Webinar:
    title: str
    start_at: datetime
    end_at: datetime | None
    groups: tuple[str, ...]
    join_url: str | None
    action: str
    is_recording: bool = False
    actions_html: str = ""
    # The СДО numbers webinars as they are created: a larger id is a newer room.
    webinar_id: int | None = None

    @property
    def is_joinable(self) -> bool:
        return bool(self.join_url) and not self.is_recording


def normalize_group(value: str) -> str:
    return value.strip().upper().replace("—", "-").replace("–", "-").replace(" ", "")


def _normalize_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9a-zа-яё]+", " ", value.casefold())
    return " ".join(cleaned.split())


def _epoch(value: str | None) -> datetime | None:
    if not value or not value.strip().lstrip("-").isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(value)).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def parse_webinars(html: str, base_url: str = "") -> list[Webinar]:
    """Parse the rows of the webinars table. Unknown rows are skipped, not raised on."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("#wb2-table") or soup
    webinars: list[Webinar] = []
    for row in container.select("table.data tbody tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 4:
            continue
        start_at = _epoch(cells[1].get("data-val"))
        if start_at is None:
            continue
        groups = tuple(
            normalize_group(tag.get_text())
            for tag in cells[3].select("span.group-tag")
            if tag.get_text(strip=True)
        )
        actions = row.select_one("td.actions") or (cells[4] if len(cells) > 4 else None)
        control, raw_href = _action_control(actions)
        join_url = urljoin(base_url, raw_href) if raw_href else None
        classes = " ".join((control.get("class") or []) if control is not None else [])
        text = actions.get_text(" ", strip=True) if actions else ""
        raw_id = str(control.get("data-id") or "") if control is not None else ""
        recording = bool(
            RECORDING_RE.search(classes)
            or RECORDING_RE.search(raw_href or "")
            or RECORDING_RE.search(text)
        )
        webinars.append(
            Webinar(
                title=cells[0].get_text(" ", strip=True),
                start_at=start_at,
                end_at=_epoch(cells[2].get("data-val")),
                groups=groups,
                join_url=join_url,
                action=text,
                is_recording=recording,
                actions_html="" if (raw_href or actions is None) else str(actions),
                webinar_id=int(raw_id) if raw_id.isdigit() else None,
            )
        )
    return webinars


def _action_control(actions):
    """Find the element carrying the webinar address in the actions cell.

    The СДО renders the control as `<button data-href="…">`, not as a link, so
    looking only for an anchor finds nothing.
    """
    if actions is None:
        return None, None
    for candidate in actions.select("a[href], [data-href]"):
        href = (candidate.get("data-href") or candidate.get("href") or "").strip()
        if href and not href.startswith("#"):
            return candidate, href
    return None, None


def _subject_matches(webinar: Webinar, subject: str) -> bool:
    title = _normalize_name(webinar.title)
    wanted = _normalize_name(subject)
    if not wanted:
        return False
    if wanted in title:
        return True
    # Titles are sometimes shortened; require most of the subject's words instead.
    words = [word for word in wanted.split() if len(word) > 3]
    if not words:
        return False
    hits = sum(word in title for word in words)
    return hits / len(words) >= 0.8


def _name_matches(candidate: str, subject: str) -> bool:
    """Tolerate shortened course names without accepting a vaguely related course."""
    title = _normalize_name(candidate)
    wanted = _normalize_name(subject)
    if not title or not wanted:
        return False
    if wanted in title or title in wanted:
        return True
    title_words = {word for word in title.split() if len(word) > 3}
    wanted_words = {word for word in wanted.split() if len(word) > 3}
    common = title_words & wanted_words
    coverage = len(common) / max(1, min(len(title_words), len(wanted_words)))
    return (len(common) >= 2 and coverage >= 0.7) or SequenceMatcher(
        None, title, wanted
    ).ratio() >= 0.78


def _slot_matches(
    webinar: Webinar,
    *,
    start_at: datetime,
    end_at: datetime,
    group: str,
    tolerance: timedelta,
    now: datetime | None = None,
    excluded: frozenset[str] = frozenset(),
) -> bool:
    wanted_group = normalize_group(group)
    if webinar.is_recording:
        return False
    if webinar.join_url and webinar.join_url in excluded:
        return False  # this room already turned out to be over or wrong
    if now is not None and webinar.end_at is not None and webinar.end_at < now - FINISHED_GRACE:
        return False
    if webinar.groups and wanted_group and wanted_group not in webinar.groups:
        return False
    return start_at - tolerance <= webinar.start_at <= end_at


def webinar_candidates(
    webinars: list[Webinar],
    *,
    subject: str,
    start_at: datetime,
    end_at: datetime,
    group: str,
    tolerance: timedelta = DEFAULT_TOLERANCE,
    now: datetime | None = None,
    excluded: frozenset[str] = frozenset(),
) -> list[Webinar]:
    """Webinars that could belong to one lesson: same group, same slot, same subject."""
    candidates = []
    for webinar in webinars:
        if not _slot_matches(
            webinar,
            start_at=start_at,
            end_at=end_at,
            group=group,
            tolerance=tolerance,
            now=now,
            excluded=excluded,
        ):
            continue
        if not _subject_matches(webinar, subject):
            continue
        candidates.append(webinar)
    return candidates


def _best(candidates: list[Webinar], start_at: datetime, group: str) -> Webinar | None:
    """A room you can actually enter beats an exact time slot with no link.

    Among enterable rooms of this group the newest one wins: when a teacher
    closes a room minutes into the pair and opens another, the old row stays
    in the table, sits closer to the bell and used to be chosen again.
    """
    if not candidates:
        return None
    wanted_group = normalize_group(group)
    return min(
        candidates,
        key=lambda w: (
            not w.is_joinable,
            not (wanted_group and wanted_group in w.groups),
            -(w.webinar_id or 0),
            abs(w.start_at - start_at),
        ),
    )


def match_webinar(
    webinars: list[Webinar],
    *,
    subject: str,
    start_at: datetime,
    end_at: datetime,
    group: str,
    tolerance: timedelta = DEFAULT_TOLERANCE,
) -> Webinar | None:
    """Pick the webinar belonging to one lesson out of a single page."""
    return _best(
        webinar_candidates(
            webinars,
            subject=subject,
            start_at=start_at,
            end_at=end_at,
            group=group,
            tolerance=tolerance,
        ),
        start_at,
        group,
    )


def find_webinar_modules(html: str, base_url: str = "") -> list[tuple[str, str]]:
    """Find webinar module links on a course or section page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not WEBINAR_MODULE_RE.search(href):
            continue
        url = urljoin(base_url, href)
        title = link.get_text(" ", strip=True)
        # The same module is often linked twice; keep the variant that has a title.
        if url not in found or (title and not found[url]):
            found[url] = title
    return [(title, url) for url, title in found.items()]


def find_courses(html: str, base_url: str = "") -> list[tuple[str, str]]:
    """Read the student's own course list: (title, url) for every course."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for link in soup.find_all("a", href=True):
        if not COURSE_LINK_RE.search(link["href"]):
            continue
        url = urljoin(base_url, link["href"])
        title = link.get_text(" ", strip=True)
        if title and (url not in found or not found[url]):
            found[url] = title
    return [(title, url) for url, title in found.items()]


def discover_course_urls(fetch, subject: str, courses_url: str = COURSES_URL) -> list[str]:
    """Find the courses of one subject without the student pasting any address.

    Course titles carry suffixes such as «(часть 1/1) [I.26-27]», so the subject
    from the schedule is matched inside the title rather than against it.
    """
    courses = find_courses(fetch(courses_url), courses_url)
    matches = [url for title, url in courses if _name_matches(title, subject)]
    log.info("moodle_courses_scanned total=%s matched=%s", len(courses), len(matches))
    return matches


def is_group_code(value: str) -> bool:
    """Whether a string looks like a MIREA group, e.g. ИКБО-11-99."""
    return bool(GROUP_RE.fullmatch(normalize_group(value)))


def module_group_rank(title: str, group: str) -> int:
    """0 — the module names this group, 1 — it names none, 2 — it names other groups only."""
    named = {normalize_group(found) for found in GROUP_RE.findall(title)}
    if not named:
        return 1
    return 0 if normalize_group(group) in named else 2


def _teacher_rank(normalized_title: str, teacher: str | None) -> int:
    """Titles usually carry «Лектор - Фамилия Имя Отчество»; the surname is enough."""
    parts = _normalize_name(teacher or "").split()
    if not parts:
        return 1
    return 0 if parts[0] in normalized_title else 1


def _module_priority(
    title: str,
    subject: str,
    start_at: datetime,
    group: str = "",
    teacher: str | None = None,
) -> tuple[int, int, int, int]:
    """Order the modules so the one belonging to this student is opened first.

    Teachers name modules after the groups and the lecturer they are meant for,
    so those two say far more than the date does about which module is ours.
    """
    normalized = _normalize_name(title)
    dated = start_at.strftime("%d.%m.%Y") in title or start_at.strftime("%d.%m.%y") in title
    return (
        module_group_rank(title, group),
        _teacher_rank(normalized, teacher),
        0 if dated else 1,
        0 if _normalize_name(subject) in normalized else 1,
    )


def resolve_lecture_url(
    fetch,
    sources: list[str],
    *,
    subject: str,
    start_at: datetime,
    end_at: datetime,
    group: str,
    teacher: str | None = None,
    tolerance: timedelta = DEFAULT_TOLERANCE,
    max_pages: int = MAX_MODULE_PAGES,
    now: datetime | None = None,
    excluded: frozenset[str] = frozenset(),
) -> Webinar | None:
    """Walk every configured source and return the best room found for one lesson.

    `fetch` takes a URL and returns HTML. A source may be a webinar module page or
    a course/section page listing several of them; both shapes are followed. All
    reachable pages are read before choosing, because a teacher may create two
    webinars for one lesson — the one at the exact time carrying no link, and a
    later one that is the room actually used. Stopping at the first match would
    return the dead one. A page that fails to load never stops the others.

    With ``now`` given, webinars whose scheduled end has passed are skipped, and
    ``excluded`` rooms (ones that already turned out to be over) are never chosen.
    """
    visited: set[str] = set()
    queue = list(dict.fromkeys(sources))
    pending_modules: list[tuple[tuple[int, int], str]] = []
    pages_read = 0
    failed_pages = 0
    candidates: list[Webinar] = []
    slot_candidates: list[Webinar] = []

    while queue or pending_modules:
        if pages_read >= max_pages:
            log.info("moodle_page_budget_reached subject=%s", subject)
            break
        if queue:
            url = queue.pop(0)
        else:
            pending_modules.sort()
            priority, url = pending_modules.pop(0)
            if priority[0] == OTHER_GROUP and any(w.is_joinable for w in candidates):
                # A module addressed to other groups only cannot beat a room we
                # can already enter, and reading it just costs time.
                log.info("moodle_skipped_other_group url=%s", url)
                continue
        if not url or url in visited:
            continue
        visited.add(url)
        try:
            html = fetch(url)
        except Exception as exc:  # noqa: BLE001 - one broken source must not hide the rest
            log.warning("moodle_source_failed url=%s error=%s", url, exc)
            failed_pages += 1
            continue
        pages_read += 1
        parsed = parse_webinars(html, url)
        candidates.extend(
            webinar_candidates(
                parsed,
                subject=subject,
                start_at=start_at,
                end_at=end_at,
                group=group,
                tolerance=tolerance,
                now=now,
                excluded=excluded,
            )
        )
        slot_candidates.extend(
            webinar
            for webinar in parsed
            if _slot_matches(
                webinar,
                start_at=start_at,
                end_at=end_at,
                group=group,
                tolerance=tolerance,
                now=now,
                excluded=excluded,
            )
        )
        for title, module_url in find_webinar_modules(html, url):
            if module_url not in visited:
                pending_modules.append(
                    (_module_priority(title, subject, start_at, group, teacher), module_url)
                )

    if pages_read == 0 and failed_pages:
        raise RuntimeError("Ни одна страница СДО не ответила")
    best = _best(candidates, start_at, group)
    # Some subject-specific modules name rows only «Лекция» or «Вебинар».
    # Group and time still identify the room safely when there is exactly one option.
    unique_slot_candidates = {
        (item.start_at, item.join_url, item.title): item for item in slot_candidates
    }
    if best is None:
        wanted_group = normalize_group(group)
        explicit_group = [
            item for item in unique_slot_candidates.values() if wanted_group in item.groups
        ]
        fallback = explicit_group or list(unique_slot_candidates.values())
        if len(fallback) == 1:
            best = _best(fallback, start_at, group)
    if best is not None and not candidates:
        log.info("moodle_subject_fallback_used subject=%s", subject)
    log.info(
        "moodle_lookup_finished subject=%s pages=%s candidates=%s joinable=%s",
        subject,
        pages_read,
        len(candidates),
        bool(best and best.is_joinable),
    )
    return best


def looks_like_login_page(html: str) -> bool:
    lowered = html.casefold()
    return any(marker in lowered for marker in LOGIN_PAGE_MARKERS)


def find_sso_login_links(html: str, base_url: str = "") -> list[str]:
    """Collect the «войти через SSO» links of a Moodle login page.

    The СДО uses the same MIREA SSO as Pulse, so following one of these with an
    existing session is what turns a login page into a signed-in one.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if SSO_LINK_RE.search(href):
            url = urljoin(base_url, href)
            if url not in links:
                links.append(url)
    return links
