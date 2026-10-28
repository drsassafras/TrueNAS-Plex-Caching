"""Pages of a long list, told by their span: "A–Be", "Bf–Da"… — the shortest prefixes of the first
and last entry on each page that tell it from its neighbours, so a page can be opened by what's on
it rather than by Next, Next, Next. Shared by every paged list (the Library page's files, a job's
files on Processes); the page draws them with pager() in app.js.
"""

import itertools
import re

MAX_PREFIX = 3  # (as many letters as tell one page from the next, three at most)
_LEAD = re.compile(r"^[^\w]+")  # what a title starts with before its first letter or digit: quotes, brackets, dots…


def plain(label: str) -> str:
    """A title without the quote, apostrophe or bracket it opens with, for sorting and the page spans:
    “'Twas the Night Before Christmas” goes with the Ts, not before 10,000 BC. (The page's own copy: app.js plainTitle.)"""
    return _LEAD.sub("", str(label or ""))


def sort_key(label: str) -> str:
    """How every list of titles sorts: by the title's letters, case aside (plain(), casefolded)."""
    return plain(label).casefold()


def _common(a: str, b: str) -> int:
    a, b = a.casefold(), b.casefold()
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return n


def _cut(label: str, n: int) -> str:
    out = label[:max(1, min(n, MAX_PREFIX))].rstrip()
    return out or label[:1] or "…"


def page_ranges(labels: list[str], per_page: int) -> list[dict]:
    """[{"offset", "from", "to"}] for the pages of ``labels`` (in their listed order): each page's
    first and last label cut to the shortest prefix that tells it from the page before and after."""
    if per_page < 1 or not labels:
        return []
    pages = [{"offset": i * per_page, "first": plain(page[0]), "last": plain(page[-1])} for i, page in enumerate(itertools.batched(labels, per_page))]
    out = []
    for i, p in enumerate(pages):
        k_from = _common(pages[i - 1]["last"], p["first"]) + 1 if i else 1
        k_to = _common(p["last"], pages[i + 1]["first"]) + 1 if i + 1 < len(pages) else 1
        out.append({"offset": p["offset"], "from": _cut(p["first"], k_from), "to": _cut(p["last"], k_to)})
    return out
