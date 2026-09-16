"""Shared text normalisation, tokenisation and quote verification.

These helpers used to exist twice — once in the lexical ranker as `_text` /
`_tokens` / `_mentions`, once in the semantic ranker as `normalize` — with
subtly different whitespace rules, which is how a quote could verify against
one module's view of a document and fail against the other's. One definition
each, used everywhere.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

STOP_WORDS = frozenset("""a an and are as at be been being by can co coop co-op company could did do does
each employer employment for from full have has had how i if in into is it its
job jobs join may more must of on one opportunity or our position relevant role
seeking some such team than that the their them then there these they this those
through to under up us using was we were what when where which who will with
would you your university waterloo resume email phone linkedin github apply about
able strong excellent including
""".split())

_TOKEN = re.compile(r"[^\W_][\w+#.-]*", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def clean_unicode(text: str) -> str:
    """Fold PDF ligatures and stray control characters without losing C++ or C#."""
    return unicodedata.normalize("NFKC", text).replace("\x00", " ").replace("­", "")


def flatten(value: object) -> str:
    """Render any job field — string, list or None — as comparable text."""
    if isinstance(value, str):
        return clean_unicode(value)
    if isinstance(value, (list, tuple)):
        return " ".join(flatten(item) for item in value)
    if isinstance(value, dict):
        return " ".join(f"{key}: {flatten(item)}" for key, item in value.items())
    return "" if value is None else str(value)


def collapse(text: str) -> str:
    """Single-space form used for every quote comparison in the project."""
    return _WHITESPACE.sub(" ", text or "").strip()


def tokens(text: str) -> list[str]:
    """Content tokens: lowercase, no stop words, no bare numbers, length > 1."""
    result = []
    for raw in _TOKEN.findall(clean_unicode(text).casefold()):
        token = raw.strip(".-")
        if len(token) > 1 and token not in STOP_WORDS and not token.isdigit():
            result.append(token)
    return result


def mentions(text: str, phrase: str) -> bool:
    """Whole-phrase, case-insensitive containment. `go` must not match `going`."""
    phrase = (phrase or "").casefold().strip()
    if not phrase:
        return False
    return bool(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", (text or "").casefold()))


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "sixteen": 16,
}
_WORD_DURATION = re.compile(
    r"(?<![\w-])(" + "|".join(_WORD_NUMBERS) + r")(\s*[-\u2013]?\s*)(month|week|term)", re.I)


def normalise_durations(text: str) -> str:
    """Rewrite "four-month" as "4-month" so one numeric rule covers both forms.

    WaterlooWorks postings and resumes use the spelled-out form at least as
    often as digits; matching only digits silently missed every mandatory
    eight-month posting.
    """
    return _WORD_DURATION.sub(
        lambda match: f"{_WORD_NUMBERS[match.group(1).casefold()]}{match.group(2)}{match.group(3)}", text)


def quotes_verbatim(quote: str, source: str, minimum: int = 8) -> bool:
    """Whether a model-supplied quote really occurs in the document it cites.

    Whitespace-insensitive but otherwise exact: no ellipses, no paraphrase. A
    quote shorter than `minimum` characters is rejected because short strings
    match by accident, which would let an unsupported claim pass verification.
    """
    needle = collapse(quote)
    return len(needle) >= minimum and needle in collapse(source)


def unique(values: Iterable[str]) -> list[str]:
    """Order-preserving de-duplication, used wherever warnings accumulate."""
    return list(dict.fromkeys(value for value in values if value))
