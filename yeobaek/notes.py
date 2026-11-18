"""Note search, Markdown imports, and reusable starting points."""

import re
import unicodedata
from datetime import date
from pathlib import Path

_QUERY_PART = re.compile(r'(?:[^\s"]|"[^"]*")+|[^\s]+')
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_TASK = re.compile(r"^( {0,3}[-+*] \[)([ xX])(\] )(.*)$")
_TITLE = re.compile(r"^ {0,3}#[ \t]+(.+)$")
_ATTACHMENT_LINK = re.compile(r"\[((?:\\.|[^\]\\\r\n])*)\]\(yeobaek-attachment:([0-9a-f]{64})\)")
SEARCH_FILTERS = frozenset(("title", "body", "tag", "book", "is"))


def folded(value):
    return unicodedata.normalize("NFKC", value).casefold()


def parse_query(value):
    query = []
    for token in _QUERY_PART.findall(value):
        excluded = token.startswith("-") and len(token) > 1
        token = token[1:] if excluded else token
        field, separator, term = token.partition(":")
        if not separator or field.lower() not in SEARCH_FILTERS or not term:
            field, term = "", token
        term = folded(term.strip('"'))
        if term:
            query.append((field.lower(), term, excluded))
    return query


def task_lines(body):
    """Yield top-level Markdown task lines outside fenced code blocks."""
    fence = None
    for index, line in enumerate(body.splitlines()):
        delimiter = _FENCE.match(line)
        if delimiter:
            marker, trailing = delimiter.groups()
            if fence is None:
                fence = marker
            elif marker.startswith(fence) and not trailing.strip():
                fence = None
            continue
        task = _TASK.match(line) if fence is None else None
        if task:
            yield index, task


def matches_query(note, book_name, query):
    title, body = folded(note["title"]), folded(note["body"])
    tags = tuple(folded(tag) for tag in note["tags"])
    book = folded(book_name)
    for field, term, excluded in query:
        if field == "tag":
            found = term.removeprefix("#") in tags
        elif field == "title":
            found = term in title
        elif field == "body":
            found = term in body
        elif field == "book":
            found = term in book
        elif field == "is" and term == "pinned":
            found = note["pinned"]
        elif field == "is" and term == "task":
            found = any(task[2] == " " for _, task in task_lines(note["body"]))
        elif field == "is" and term == "file":
            found = _ATTACHMENT_LINK.search(note["body"]) is not None
        else:
            found = term in title or term in body or term in book or any(term in tag for tag in tags)
        if found == excluded:
            return False
    return True


def attachment_link(name, digest):
    label = re.sub(r"([\\\[\]])", r"\\\1", name.replace("\r", " ").replace("\n", " "))
    return f"[{label}](yeobaek-attachment:{digest})"


def attachment_links(body):
    return [
        (re.sub(r"\\(.)", r"\1", match[1]), match[2], match.group(0))
        for match in _ATTACHMENT_LINK.finditer(body)
    ]


def without_attachment_links(body):
    return _ATTACHMENT_LINK.sub("", body)


def attachment_filename(name):
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip().rstrip(".")
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", base, re.IGNORECASE):
        base = "_" + base
    return base or "첨부 파일"


def read_note(name, raw):
    """Read UTF-8 or BOM-marked UTF-16 text, using a leading Markdown H1 as title."""
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        body = raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError as exc:
        raise ValueError(f"{name}의 문자 인코딩을 읽을 수 없어요. UTF-8 파일을 선택해 주세요.") from exc
    if "\x00" in body:
        raise ValueError(f"{name}은 텍스트 파일로 읽을 수 없어요.")
    title = Path(name).stem
    if Path(name).suffix.lower() in (".md", ".markdown"):
        first, _, rest = body.lstrip("\n").partition("\n")
        match = _TITLE.match(first)
        if match:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", match[1]).strip()
            body = rest.lstrip("\n")
    return {"title": title, "body": body}


TEMPLATES = {
    "daily": ("오늘의 기록", "## 오늘 한 일\n\n\n## 떠오른 생각\n\n\n## 다음 할 일\n\n- [ ] "),
    "meeting": ("회의록", "## 안건\n\n\n## 논의 내용\n\n\n## 결정 사항\n\n\n## 할 일\n\n- [ ] "),
    "research": ("실험 기록", "## 목적\n\n\n## 시료 및 조건\n\n\n## 관찰 결과\n\n\n## 다음 확인 사항\n\n- [ ] "),
    "tasks": ("할 일 목록", "- [ ] \n- [ ] \n- [ ] "),
}


def template_note(key):
    title, body = TEMPLATES[key]
    if key in ("daily", "meeting", "research"):
        title = f"{date.today().isoformat()} {title}"
    return {"title": title, "body": body}
