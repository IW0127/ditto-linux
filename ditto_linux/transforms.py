"""Text transformations for "Special Paste" (like Ditto's Special Paste menu)."""
from __future__ import annotations

import re
import unicodedata


def _sentence(text: str) -> str:
    out, start = [], True
    for ch in text.lower():
        if start and ch.isalpha():
            out.append(ch.upper())
            start = False
        else:
            out.append(ch)
        if ch in ".!?\n":
            start = True
    return "".join(out)


def _title(text: str) -> str:
    # str.title() turns "don't" into "Don'T"; capitalise only the first letter of each word
    return re.sub(r"[^\W\d_]+(?:['’][^\W\d_]+)*", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), text)


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _squeeze(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip("\n")


# key -> (menu label, shortcut letter, function); order = menu order
TRANSFORMS: dict[str, tuple[str, str, object]] = {
    "upper": ("UPPERCASE", "U", str.upper),
    "lower": ("lowercase", "L", str.lower),
    "title": ("Capitalize Each Word", "C", _title),
    "sentence": ("Sentence case", "S", _sentence),
    "invert": ("iNVERT cASE", "I", str.swapcase),
    "trim": ("Trim spaces", "T", lambda t: "\n".join(line.strip() for line in t.strip().splitlines())),
    "one_line": ("Remove line breaks", "R", lambda t: " ".join(line.strip() for line in t.splitlines() if line.strip())),
    "squeeze": ("Remove extra spaces and blank lines", "E", _squeeze),
    "slug": ("slugify-text", "G", _slug),
}


def apply(key: str, text: str) -> str:
    if not key:
        return text
    if key not in TRANSFORMS:
        raise ValueError(f"unknown transform {key!r} (choose from {', '.join(TRANSFORMS)})")
    return TRANSFORMS[key][2](text)


SEPARATORS = {"newline": "\n", "space": " ", "comma": ", ", "tab": "\t", "none": ""}
