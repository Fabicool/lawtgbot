"""Завантаження офіційних правових сторінок і перевірка посилань Дослідника.

Сторінки відкриває сервер платформи, а не ШІ. Частина сайтів (zakon.rada.gov.ua,
reyestr.court.gov.ua) не відкривається з-за кордону, тому платформа має працювати в Україні;
інакше посилання позначаються «перевірте вручну».
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

# Піддомени теж дозволені (supreme.court.gov.ua, lpd.court.gov.ua тощо).
ALLOWED_DOMAINS = ("zakon.rada.gov.ua", "reyestr.court.gov.ua", "court.gov.ua")
TIMEOUT = 20.0
MAX_BYTES = 3_000_000
USER_AGENT = "Mozilla/5.0 (compatible; LawyerPlatform/1.0)"

_NOT_FOUND_MARKERS = ("документ не знайдено", "сторінку не знайдено", "page not found", "404 not found")

# Тестам дозволено підмінити транспорт (httpx.MockTransport).
transport: httpx.AsyncBaseTransport | None = None


def domain_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "head"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


@dataclass
class Page:
    url: str
    status: int = 0
    text: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error


async def fetch(url: str) -> Page:
    if not domain_allowed(url):
        return Page(url, error="domain")
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}, transport=transport
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return Page(url, error=type(exc).__name__)
    if not domain_allowed(str(response.url)):
        return Page(url, status=response.status_code, error="domain")
    body = response.content[:MAX_BYTES].decode(response.encoding or "utf-8", errors="replace")
    ctype = response.headers.get("content-type", "")
    text = html_to_text(body) if "html" in ctype or body.lstrip().startswith("<") else body
    return Page(url, status=response.status_code, text=text)


def excerpt(text: str, find: str = "", limit: int = 12000) -> str:
    """Фрагмент тексту навколо шуканого (напр. «Стаття 40»), щоб не передавати ШІ весь кодекс."""
    if len(text) <= limit:
        return text
    if find:
        idx = text.lower().find(find.lower())
        if idx >= 0:
            start = max(0, idx - 1500)
            return ("…" if start else "") + text[start : start + limit] + "…"
    return text[:limit] + "…"


def _norm_case_number(value: str) -> str:
    return re.sub(r"[^0-9/\-]", "", value)


@dataclass
class CheckResult:
    status: str  # ok | opened | not_found | unreachable | bad_domain
    note: str


async def check_link(url: str, case_number: str = "") -> CheckResult:
    if not url:
        return CheckResult("not_found", "Посилання відсутнє")
    if not domain_allowed(url):
        return CheckResult("bad_domain", "Не офіційне джерело — посилання не з дозволених сайтів")
    page = await fetch(url)
    if page.error == "domain":
        return CheckResult("bad_domain", "Сторінка переадресовує на інший сайт")
    if page.error:
        return CheckResult(
            "unreachable",
            "Сайт не відкрився з сервера платформи (можливо, обмеження доступу з-за кордону) — "
            "відкрийте посилання й перевірте вручну",
        )
    if page.status in (404, 410):
        return CheckResult("not_found", f"Сторінки не існує (HTTP {page.status})")
    if page.status != 200:
        return CheckResult("unreachable", f"Сайт відповів HTTP {page.status} — перевірте вручну")
    lowered = page.text.lower()
    if any(marker in lowered for marker in _NOT_FOUND_MARKERS):
        return CheckResult("not_found", "Сайт повідомляє, що документ не знайдено")
    if case_number:
        wanted = _norm_case_number(case_number)
        if wanted and wanted in _norm_case_number(page.text):
            return CheckResult("ok", f"Сторінка відкривається, номер справи {case_number} на ній є")
        if len(page.text) < 1500:
            return CheckResult(
                "opened", "Сторінка відкривається, але зміст не завантажився — перевірте номер справи"
            )
        return CheckResult(
            "not_found", f"Сторінка відкривається, але номера справи {case_number} на ній немає"
        )
    if len(page.text) < 300:
        return CheckResult("opened", "Сторінка відкривається, зміст перевірте вручну")
    return CheckResult("ok", "Сторінка відкривається")
