"""Conversion between Linux MIME clipboard data and Ditto's Windows clipboard format names.

Clips are handled in memory as ``{mime_type: bytes}``. When stored they use the same
format names as Windows Ditto where a sensible equivalent exists, so a ``Ditto.db``
copied from Windows can be opened here and vice versa.
"""
from __future__ import annotations

import re
import struct

TEXT = "text/plain"
HTML = "text/html"
PNG = "image/png"
BMP = "image/bmp"
URIS = "text/uri-list"

# Linux MIME -> Ditto format name
_TO_DITTO = {TEXT: "CF_UNICODETEXT", HTML: "HTML Format", PNG: "PNG", URIS: "text/uri-list"}
_FROM_DITTO = {v: k for k, v in _TO_DITTO.items()}

# MIME types captured from the clipboard, in priority order.
CAPTURE_TYPES = (TEXT, HTML, PNG, URIS)


def encode_unicode_text(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\x00\x00"


def decode_unicode_text(data: bytes) -> str:
    text = data.decode("utf-16-le", errors="replace")
    return text.split("\x00", 1)[0]


def encode_cf_html(html: str) -> bytes:
    """Wrap an HTML fragment in the Windows "HTML Format" (CF_HTML) header."""
    body = html.encode("utf-8")
    header_tpl = (
        "Version:0.9\r\nStartHTML:{0:010d}\r\nEndHTML:{1:010d}\r\n"
        "StartFragment:{2:010d}\r\nEndFragment:{3:010d}\r\n"
    )
    prefix = b"<html><body>\r\n<!--StartFragment-->"
    suffix = b"<!--EndFragment-->\r\n</body></html>"
    header_len = len(header_tpl.format(0, 0, 0, 0).encode("ascii"))
    start_html = header_len
    start_frag = start_html + len(prefix)
    end_frag = start_frag + len(body)
    end_html = end_frag + len(suffix)
    header = header_tpl.format(start_html, end_html, start_frag, end_frag).encode("ascii")
    return header + prefix + body + suffix


def decode_cf_html(data: bytes) -> str:
    """Extract the fragment from CF_HTML data; tolerate plain HTML too."""
    head = data[:400].decode("ascii", errors="ignore")
    m1 = re.search(r"StartFragment:(-?\d+)", head)
    m2 = re.search(r"EndFragment:(-?\d+)", head)
    if m1 and m2:
        start, end = int(m1.group(1)), int(m2.group(1))
        if 0 <= start <= end <= len(data):
            return data[start:end].decode("utf-8", errors="replace")
    return data.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def dib_to_bmp(dib: bytes) -> bytes:
    """Prefix a CF_DIB payload with a BITMAPFILEHEADER so standard decoders can read it."""
    if len(dib) < 40:
        return b""
    header_size, _w, _h, _planes, bpp, compression, _size, _xr, _yr, clr_used = struct.unpack_from(
        "<IiiHHIIiiI", dib, 0
    )
    colors = clr_used or (1 << bpp if bpp <= 8 else 0)
    masks = 12 if compression == 3 and header_size == 40 else 0
    offset = 14 + header_size + masks + colors * 4
    return b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, offset) + dib


def to_storage(formats: dict[str, bytes]) -> list[tuple[str, bytes]]:
    out = []
    for mime, data in formats.items():
        if mime == TEXT:
            out.append(("CF_UNICODETEXT", encode_unicode_text(data.decode("utf-8", errors="replace"))))
        elif mime == HTML:
            out.append(("HTML Format", encode_cf_html(data.decode("utf-8", errors="replace"))))
        else:
            out.append((_TO_DITTO.get(mime, mime), data))
    return out


def from_storage(rows: list[tuple[str, bytes]]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for name, data in rows:
        data = bytes(data or b"")
        if name == "CF_UNICODETEXT":
            out[TEXT] = decode_unicode_text(data).encode("utf-8")
        elif name in ("CF_TEXT", "CF_OEMTEXT"):
            out.setdefault(TEXT, data.split(b"\x00", 1)[0].decode("cp1252", errors="replace").encode("utf-8"))
        elif name == "HTML Format":
            out[HTML] = decode_cf_html(data).encode("utf-8")
        elif name == "CF_DIB":
            bmp = dib_to_bmp(data)
            if bmp:
                out.setdefault(BMP, bmp)
        elif "/" in name or name in _FROM_DITTO:
            out[_FROM_DITTO.get(name, name)] = data
        # other Windows-only formats (CF_HDROP, Rich Text Format, ...) are ignored
    return out


def describe(formats: dict[str, bytes]) -> str:
    """The text shown in the list (Ditto's ``mText`` column)."""
    if TEXT in formats:
        return formats[TEXT].decode("utf-8", errors="replace")
    if URIS in formats:
        return formats[URIS].decode("utf-8", errors="replace").strip()
    if HTML in formats:
        return re.sub(r"<[^>]+>", "", formats[HTML].decode("utf-8", errors="replace")).strip() or "HTML"
    if PNG in formats or BMP in formats:
        return "[Image]"
    return ", ".join(sorted(formats)) or ""
