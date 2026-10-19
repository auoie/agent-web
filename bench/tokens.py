"""Token cost of reading a page with different tools, on identical downloaded bytes.

    uv run --with tiktoken --with trafilatura --with html2text bench/tokens.py

Tokens are counted with tiktoken's o200k_base as a proxy (Claude's tokenizer isn't public):
absolute numbers differ, ratios between tools hold. Each page has a phrase from its real content;
an output without it is marked as having lost the content.

Optional: set DONSETCH to a DonSeTch binary to add its `fetch` and `fetch --focus` columns. It
downloads pages itself (not the shared bytes), with its update check off and its cache in a
temporary directory.

Optional: set TURNDOWN_DIR to a directory with node_modules/turndown to include what Claude
Code's WebFetch hands its Haiku summarizer (Turndown with script/style/noscript/iframe removed,
cut to 100,000 chars). Claude itself then only sees Haiku's short answer.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

import tiktoken
from curl_cffi import requests

from agent_web.convert.html import convert_html
from agent_web.convert.pandoc import Pandoc, find_pandoc
from agent_web.output import find_passages


@dataclass(frozen=True)
class Page:
    name: str
    url: str
    phrase: str  # must survive conversion
    find: str  # a targeted lookup an agent might do


PAGES = [
    Page(
        "Wikipedia table",
        "https://en.wikipedia.org/wiki/List_of_largest_companies_by_revenue",
        "1,026,301",  # a number from the table, not just a company name
        "Saudi Aramco",
    ),
    Page(
        "News article",
        "https://www.theregister.com/2026/03/31/anthropic_claude_code_source_code",
        "Chaofan Shou",
        "npm",
    ),
    Page(
        "Blog post",
        "https://mikhail.io/2025/10/claude-code-web-tools/",
        "Web page content",
        "Haiku",
    ),
    Page(
        "Link post",
        "https://simonwillison.net/2026/Jul/15/claude-web-fetch-exfiltration/",
        "letter by letter",
        "web_fetch",
    ),
    Page(
        "Python docs",
        "https://docs.python.org/3/library/argparse.html",
        "add_argument",
        "exit_on_error",
    ),
    Page("GitHub README", "https://github.com/adbar/trafilatura", "Trafilatura is", "license"),
    Page("Tiny page", "https://zolt.top/", "Nothing here yet", "Updated"),
]

ENCODING = tiktoken.get_encoding("o200k_base")


def tokens(text: str) -> int:
    return len(ENCODING.encode(text, disallowed_special=()))


def html2text_md(html: str, url: str) -> str:
    import html2text

    converter = html2text.HTML2Text(baseurl=url, bodywidth=0)
    return converter.handle(html)


def pandoc_plain(html: str, pandoc: Pandoc) -> str:
    result = subprocess.run(
        [pandoc.path, "-f", "html", "-t", "gfm", "--wrap=none"],
        input=html,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def trafilatura_md(html: str, url: str) -> str:
    import trafilatura

    return trafilatura.extract(html, url=url, output_format="markdown") or ""


def turndown_md(html: str) -> str | None:
    directory = os.environ.get("TURNDOWN_DIR")
    if not directory:
        return None
    script = (
        "const T=require('turndown');let h='';process.stdin.on('data',d=>h+=d).on('end',()=>{"
        "const t=new T();t.remove(['style','script','noscript','iframe']);"
        "process.stdout.write(t.turndown(h.slice(0,1048576)).slice(0,100000));});"
    )
    node = shutil.which("node")
    if node is None:
        return None
    result = subprocess.run(
        [node, "-e", script],
        input=html,
        capture_output=True,
        text=True,
        cwd=directory,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def donsetch(url: str, focus: str | None = None) -> str | None:
    binary = os.environ.get("DONSETCH")
    if not binary:
        return None
    args = [binary, "fetch", url, "--max-chars", "5000000", "--deadline-ms", "60000"]
    if focus:
        args += ["--focus", focus]
    with tempfile.TemporaryDirectory() as cache:
        env = {
            **os.environ,
            "DONSETCH_NO_UPDATE_CHECK": "1",
            "DONSETCH_CACHE_DIR": cache,
            "XDG_CACHE_HOME": cache,
            "XDG_CONFIG_HOME": cache,
            "XDG_DATA_HOME": cache,
            "XDG_STATE_HOME": cache,
        }
        result = subprocess.run(args, capture_output=True, text=True, env=env, check=False)
    return result.stdout if result.returncode == 0 else ""


def main() -> None:
    pandoc = find_pandoc()
    if not isinstance(pandoc, Pandoc):
        raise SystemExit(f"pandoc: {pandoc.hint}")
    tools: list[tuple[str, Callable[[Page, str, bytes, str], str | None]]] = [
        ("curl (raw HTML)", lambda p, html, data, ct: html),
        ("curl + html2text", lambda p, html, data, ct: html2text_md(html, p.url)),
        ("curl + pandoc", lambda p, html, data, ct: pandoc_plain(html, pandoc)),
        ("WebFetch → Haiku input", lambda p, html, data, ct: turndown_md(html)),
        ("trafilatura", lambda p, html, data, ct: trafilatura_md(html, p.url)),
        ("agent-web", lambda p, html, data, ct: convert_html(data, ct, p.url, pandoc).markdown),
    ]
    rows: list[dict[str, object]] = []
    for page in PAGES:
        response = requests.get(page.url, impersonate="chrome", timeout=30)
        data = response.content
        content_type = response.headers.get("content-type") or "text/html"
        html = response.text
        row: dict[str, object] = {"page": page.name}
        for name, tool in tools:
            out = tool(page, html, data, content_type)
            if out is None:
                continue
            row[name] = (tokens(out), page.phrase.lower() in out.lower())
        full = convert_html(data, content_type, page.url, pandoc).markdown
        passages, _ = find_passages(full, page.find, 25_000)
        found = "\n\n".join(p.text for p in passages)
        row["agent-web --find"] = (tokens(found), True)
        if (full_ds := donsetch(page.url)) is not None:
            row["donsetch"] = (tokens(full_ds), page.phrase.lower() in full_ds.lower())
            focused = donsetch(page.url, page.find) or ""
            row["donsetch --focus"] = (tokens(focused), True)
        rows.append(row)

    names = [name for name, _ in tools] + ["agent-web --find", "donsetch", "donsetch --focus"]
    names = [n for n in names if any(n in r for r in rows)]
    width = max(len(str(r["page"])) for r in rows) + 2
    print("tokens (o200k proxy); ✗ = the page's real content is missing from the output\n")
    print("page".ljust(width) + "".join(n.rjust(24) for n in names))
    totals = dict.fromkeys(names, 0)
    for row in rows:
        cells = []
        for n in names:
            value = row.get(n)
            if not isinstance(value, tuple):
                cells.append("—".rjust(24))
                continue
            count, ok = int(value[0]), bool(value[1])
            totals[n] += count
            cells.append((f"{count:,}" + ("" if ok else " ✗")).rjust(24))
        print(str(row["page"]).ljust(width) + "".join(cells))
    print("total".ljust(width) + "".join(f"{totals[n]:,}".rjust(24) for n in names))
    print("\n" + json.dumps(rows, default=list))


if __name__ == "__main__":
    main()
