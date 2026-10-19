"""Characters people can't see: Unicode tag characters and terminal control codes.

Tag characters (U+E0000-U+E007F) display as nothing but language models read them, so a page can
hide instructions in them ("ASCII smuggling"). They are decoded into a visible label, like other
hidden text. Control characters can drive a terminal (clear it, set the clipboard) when a person
runs `web` directly; they are removed.
"""

from __future__ import annotations

import re

HIDDEN_LABEL = "hidden on page"

_TAGS = re.compile("[\U000e0000-\U000e007f]+")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# After a black flag, tag characters spell a subdivision flag (England, Scotland, Wales).
_BLACK_FLAG = "\U0001f3f4"
_CANCEL_TAG = "\U000e007f"


def reveal(text: str) -> tuple[str, int]:
    """`text` with control characters removed and runs of tag characters decoded into a visible
    `⟦hidden on page: …⟧` label. Returns the text and the number of labeled runs."""
    labeled = 0

    def decode(match: re.Match[str]) -> str:
        nonlocal labeled
        run = match.group(0)
        start = match.start()
        if start and match.string[start - 1] == _BLACK_FLAG and run.endswith(_CANCEL_TAG):
            return run
        labeled += 1
        ascii_text = "".join(chr(ord(c) - 0xE0000) for c in run if 0x20 <= ord(c) - 0xE0000 < 0x7F)
        return f"⟦{HIDDEN_LABEL}: {ascii_text}⟧"

    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    return _TAGS.sub(decode, text), labeled
