"""Race start lists: who starts in what order, read from the timing company's PDF or pasted text.

The upload form sorts a race's photos into one folder per racer: photos are grouped into bursts by
the time they were taken, and the bursts go to the racers in start order (a racer marked "didn't
race" is skipped). This reads the start list it works from.

Handles the usual timing printouts (e.g. Split Second "Condensed Start List": St #, Bib, Name
"Last, First", Class, Points, often two columns side by side) and plain pasted lists, one racer a
line: "12 Allmacher, Michael", "1, 12, Michael Allmacher", "12<tab>Michael Allmacher U16"."""
import re
from io import BytesIO

MAX_RACERS = 600

# One racer in a timing printout: start number, bib (a * marks a late entry), "Last, First", then
# the class (U14, U16, SR...) and points when given. Two columns on one line are two matches.
_PRINTOUT = re.compile(
    r"(?<![\d.])(\d{1,3})\s+\*?(\d{1,4})\s+"
    r"([^\W\d_][^\d,\n]*?,\s*[^\W\d_][^\d\n]*?)"
    r"(?:\s+(U\d{1,2}|[A-Z]{1,4}\d{0,2}))?"
    r"(?:\s+\d+\.\d+)?"
    r"(?=\s+\d{1,3}\s+\*?\d{1,4}\s+[^\W\d_]|\s*$)",
    re.MULTILINE)
_LINE = re.compile(r"^\s*(?:(\d{1,3})\s*[,;\t.)]?\s+)?\*?(\d{1,4})\s*[,;\t]?\s+(.+?)\s*$")
_CLASS_AT_END = re.compile(r"\s+(U\d{1,2}|[A-Z]{2,4}\d{0,2})(?:\s+\d+\.\d+)?$")


def pdf_text(data: bytes) -> str:
    """The text of a PDF (every page)."""
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def display_name(name: str) -> str:
    """'Allmacher, Michael' -> 'Michael Allmacher' (names without a comma stay as written)."""
    name = " ".join(name.split())
    if "," in name:
        last, first = (p.strip() for p in name.split(",", 1))
        return f"{first} {last}".strip()
    return name


def _clean(name: str) -> str:
    return " ".join(name.replace("’", "'").split()).strip(" ,;-")


def parse_start_list(text: str) -> list[dict]:
    """Racers in start order: [{"order", "bib", "name" (as printed), "display", "cls"}]."""
    text = text or ""
    printout = {}
    for m in _PRINTOUT.finditer(text):
        order, bib, name, cls = int(m.group(1)), m.group(2), _clean(m.group(3)), m.group(4) or ""
        printout.setdefault(order, {"order": order, "bib": bib, "name": name, "cls": cls})
    lines = {}                                           # a pasted list: one racer a line
    for line in (l for l in text.splitlines() if l.strip()):
        m = _LINE.match(line)
        if not m or not re.search(r"[^\W\d_]", m.group(3)):
            continue
        rest = m.group(3)
        cm = _CLASS_AT_END.search(rest)
        cls = cm.group(1) if cm else ""
        name = _clean(rest[:cm.start()] if cm else rest)
        if not name:
            continue
        order = int(m.group(1)) if m.group(1) else len(lines) + 1
        lines.setdefault(order, {"order": order, "bib": m.group(2), "name": name, "cls": cls})
    # A printout has two racers on some lines, so it finds more; a pasted list may have names
    # without a comma, which only the line reading finds
    found = printout if len(printout) >= len(lines) else lines
    racers = sorted(found.values(), key=lambda r: r["order"])[:MAX_RACERS]
    for r in racers:
        r["display"] = display_name(r["name"])
    return racers


def title_of(text: str) -> str:
    """The race's name from a printout (its first line that isn't a column heading), for showing."""
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if line and not _PRINTOUT.search(line) and "St #" not in line:
            return line[:80]
    return ""
