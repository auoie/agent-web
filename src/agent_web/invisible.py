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
# After a black flag, tag characters spell a subdivision flag (England is "gbeng"): a region and
# subdivision code of 3-6 tag digits and lowercase letters, then the cancel tag. Any other run
# after a black flag is labeled, so the flag can't carry hidden text.
_BLACK_FLAG = "\U0001f3f4"
_SUBDIVISION = re.compile("[\U000e0030-\U000e0039\U000e0061-\U000e007a]{3,6}\U000e007f")
# Halves of UTF-16 pairs on their own (from a UTF-7 page or a "\ud83d" escape in JSON) can't be
# written out as UTF-8.
_SURROGATE = re.compile("[\ud800-\udfff]")


def without_surrogates(text: str) -> str:
    return _SURROGATE.sub("�", text)


def reveal(text: str) -> tuple[str, int]:
    """`text` with control characters removed, lone surrogates replaced and runs of tag
    characters decoded into a visible `⟦hidden on page: …⟧` label. Returns the text and the
    number of labeled runs."""
    labeled = 0

    def decode(match: re.Match[str]) -> str:
        nonlocal labeled
        run = match.group(0)
        start = match.start()
        if start and match.string[start - 1] == _BLACK_FLAG and _SUBDIVISION.fullmatch(run):
            return run
        labeled += 1
        ascii_text = "".join(chr(ord(c) - 0xE0000) for c in run if 0x20 <= ord(c) - 0xE0000 < 0x7F)
        return f"⟦{HIDDEN_LABEL}: {ascii_text}⟧"

    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    return _TAGS.sub(decode, without_surrogates(text)), labeled
