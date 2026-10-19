# agent-web: existing alternatives (research as of 2026-10-19)

> **Status:** written during the 2026-10-19 audit. Fixed since (`aac4e9a`): MathML (now TeX,
> with live tests for arXiv), `<script>` text in search snippets, `<time>` dates without a year,
> and hidden-text counts outside `<main>`. Still open or deliberately left: see `TODO.md`.

Method: primary sources (repos, PyPI/npm metadata, docs) fetched with `web`, and hands-on runs.
I ran several converters on one local test page (a scratch file, not kept). It has a table with
rowspan and colspan, `<sup>`/`<sub>`, `<del>`, a `display:none` "injection" paragraph, MathML,
nav/footer and a footnote. I also ran KaTeX output, an arXiv HTML paper, the Table 2 PDF from
"Attention Is All You Need", and a PDF with invisible, white and tiny text.

## Verdict

**Nothing I found makes agent-web redundant.** No tool combines (1) Google results through the user's
own session, (2) a full-page conversion with no article extraction that keeps merged-cell tables
and sup/sub and labels hidden text, and (3) PDFs and agent-oriented windowing, all locally with no
middleman. Every agent-facing fetch tool I found uses Readability/article extraction plus
Turndown/markdownify-class converters. Those flatten `10<sup>-3</sup>` to `10-3` and shift
merged-cell columns. Every keyless search tool scrapes DuckDuckGo, Bing or Google without cookies,
which this user's IP already gets blocked on.

## Hands-on fidelity matrix (t.html)

| Tool (version) | sup/sub | merged-cell table | `<del>` | hidden text | MathML | nav/footer |
|---|---|---|---|---|---|---|
| **agent-web** (current tree) | kept as HTML | HTML table, correct | `~~` | **labeled ⟦hidden⟧** | `$…$`* | dropped (`<main>`) |
| plain pandoc 3.x | kept | HTML table | `~~` | passed **unlabeled** | `$…$` | dropped (`<main>`) |
| defuddle 0.19.5 | kept | HTML table, correct | `~~` | **silently removed** | `$…$` (LaTeX) | dropped; also drops title H1 |
| mcp-server-fetch (readabilipy + markdownify) | `10-3`, `H2O` | **shifted** columns | `~~` | silently removed | text duplicated (`eiπ+1=0e^{i\pi}…`) | dropped (and the H1) |
| markitdown 0.1.8 | `10-3` | **shifted** | `~~` | unlabeled | duplicated | kept |
| crawl4ai 0.9.5 (DefaultMarkdownGenerator) | `10-3` | **shifted** | `~~` | unlabeled | duplicated | kept |
| html-to-markdown 3.18.4 (Rust) | `10-3` | padded with empty cells, aligned | `~~` | silently removed | duplicated | nav dropped, footer kept |
| mdream 2.0.3 | kept | **shifted** | `~~` | unlabeled | duplicated | kept |
| w3m -dump | `10^-3`, `H[2]O` | aligned as text | `[DEL:]` | unlabeled | duplicated | kept; **link URLs lost** |

\* When this session started, agent-web dropped every `<math>` element. KaTeX, arXiv HTML and
MathJax's assistive MathML all vanished silently: on arXiv's KAN paper, "width-(2n+1)" came out as
"width-()". The working tree was changed during this session to convert visible MathML. A re-run
now gives `width-($`2n+1`$)`, so that gap is closed. Keep a regression test for KaTeX and LaTeXML
pages.

## Fetch/convert tools (agent-facing)

- **Official MCP fetch server** (`mcp-server-fetch` 2026.10.19, MIT, very active).
  https://github.com/modelcontextprotocol/servers/blob/main/src/fetch/README.md , server.py on raw.githubusercontent.
  Local. httpx, then readabilipy (Readability), then markdownify. Default `max_length` 5000 with `start_index`
  paging. It now blocks private IPs and re-checks every redirect hop (README), and respects robots.txt.
  Non-HTML is returned raw: no PDF conversion and no search. Fidelity is poor (matrix).
  **(c) Ignore.** Its security model now matches agent-web's, which shows agent-web's design is
  sound; it isn't a replacement.
- **Defuddle** (kepano, MIT, npm 0.19.5 on 2026-10-10, active). https://github.com/kepano/defuddle
  It is the best converter in the test. Runs locally (Node, CLI `defuddle parse URL|file --markdown`).
  Turndown is optional and HTML tables are kept. It standardizes math (MathJax/KaTeX to LaTeX),
  footnotes, code blocks (strips line numbers, keeps the language) and callouts. It has about 28
  site extractors (YouTube, HN, GitHub, Reddit, X, Mastodon, Substack, Wikipedia, ChatGPT/Claude
  shares…). Downsides for agent-web's goals: it *is* article extraction (`removeLowScoring`,
  partial-selector removal). It **removes hidden elements by default**, so an agent never learns
  they existed. `parseAsync` may call a third-party API (FxTwitter): a middleman unless
  `useAsync:false`.
  **(c) Don't replace pandoc**, since that would lose content and add Node. **Borrow ideas:**
  code-block line-number stripping, footnote normalization, and per-site extractors (YouTube
  transcript, HN, GitHub) like agent-web's Reddit one.
- **Microsoft MarkItDown** (MIT, 0.1.8 on 2026-09-21). https://github.com/microsoft/markitdown/releases
  Local, any file to Markdown (HTML via markdownify-like, PDF via pdfminer). Loses sup and merged
  cells; PDF table score 0.273 on the ODL benchmark. **(c) Ignore.** Its Office/EPUB coverage
  could be an optional fallback for `.docx/.pptx/.xlsx` URLs, which agent-web doesn't handle.
- **Crawl4AI** (Apache-2.0, 0.9.5 on 2026-10-14). Local crawler (Playwright) plus an html2text-fork
  converter. Same losses as markitdown. Its only real advantage is JS rendering. **(c) Ignore.**
- **Jina Reader** (Apache-2.0). https://github.com/jina-ai/reader . Hosted `r.jina.ai` is a
  middleman. Self-hosting needs headless Chrome plus "non-redistributable artifacts in licensed/",
  and the converter is Turndown. ReaderLM is a model in the loop. **(c) Ignore.**
- **html-to-markdown** (kreuzberg, Rust/PyO3, MIT, 3.18.4 on 2026-10-19). Fast. Pads rowspan and
  colspan so columns stay aligned, but flattens sup/sub and silently drops hidden text.
  **(c) Ignore** (pandoc + HTML tables is more faithful).

## Agent skills/plugins that replace WebFetch (2025–2026)

- **Djarvur/cc-websearch** (MIT, 26★). https://github.com/Djarvur/cc-websearch . A Claude Code plugin.
  Search scrapes `lite.duckduckgo.com`; fetch is Readability plus markdown, HTML only, and it
  reports cross-host redirects instead of following them. DDG is blocked for this user. **(c)**
- **Aas-ee/open-webSearch**: MCP, CLI, local daemon and skill. Multi-engine scraping (Bing, DDG,
  Brave, Startpage, Baidu…; Google still on its TODO list), plus a generic `fetch-web`.
  https://github.com/Aas-ee/open-webSearch . Keyless scraping is the class already rejected. **(c)**
- **alleneubank web-fetch skill**: curl + `html2markdown` with per-site CSS selectors. It first
  checks whether the response is `text/markdown`.
  https://raw.githubusercontent.com/alleneubank/claude-code/main/.claude/skills/web-fetch/SKILL.md **(c)**
- **full-stack-biz/fetch-urls-skill**: has an *outline* mode (list headings) and *anchor* mode
  (return just the `#section` with its subsections). https://github.com/full-stack-biz/fetch-urls-skill .
  **Feature worth copying** (see Missing).
- **brightdata/skills**, Firecrawl and similar route through paid proxies or APIs, so they are
  middlemen. **(c)**

## Search without middlemen

- **googler**: archived 2022-03-05 (https://github.com/jarun/googler). **ddgr** v2.2 is still
  around, but it scrapes DuckDuckGo HTML, which is blocked here. **(c)**
- **SearXNG** (self-hosted, AGPL, rolling releases, docs build 2026.10.19). Its Google engine
  fetches an XML layout using *Nokia user agents*, because "the normal web version requires
  executing JavaScript" (https://docs.searxng.org/dev/engines/online/google.html). So it sends
  anonymous requests from the user's IP, which is exactly the setup that got captcha'd. It also
  means running a server. #5286 documents Google's logged-out JS challenge (the `SG_SS` cookie).
  **(c) Ignore**, but note the risk: agent-web works because cookies from a real session skip
  that challenge.
- **Playwright Google scrapers** (web-agent-master/google-search: saved browser state and a headed
  window for CAPTCHAs; google-surf-mcp: hidden system Chrome plus a RocksDB "knowledge graph").
  Local, but heavy and fragile. **(c) Ignore for now.** This approach is the fallback if Google
  ever demands JS even with cookies.
- I found **no** other CLI that searches Google with the user's browser cookies. That part of
  agent-web is unique.

## PDF converters (where agent-web is weakest)

On the Attention paper's Table 2, agent-web (pymupdf 2.0 + layout) splits the header into "BL|EU"
and "Training C|ost". It also misaligns the last two rows: Transformer (base) shows `3.3·` under
EN-DE and `10^18` under EN-FR. It does keep the superscript exponents, and the existing Note warns
about this kind of error.

ODL benchmark (vendor-run, 200 PDFs; https://opendataloader.org/docs/benchmark), table TEDS score:
- opendataloader hybrid 0.928 (Apache-2.0)
- docling 0.887 (MIT)
- mineru 0.873 (custom license, was AGPL)
- marker 0.808 (GPL-3.0, 54 s/page)
- opendataloader local 0.489
- **pymupdf4llm 0.401**
- markitdown 0.273

- **opendataloader-pdf** 2.5.13 (Java 11+ CLI with a Python wrapper). I ran local mode on page 8:
  the borderless table was **not detected**, it was flattened into one paragraph, and the exponents
  were lost (`1.0 · 1020`). That is worse than pymupdf here. Hybrid mode sends complex pages to a
  local `docling-fast` server. Interesting feature: it filters PDF prompt injection (transparent
  or zero-size text, off-page content, invisible layers). https://pypi.org/project/opendataloader-pdf/
- **Docling** (IBM, MIT, 2.140.0 on 2026-10-16, very active). Fully local, with a TableFormer
  model for table structure, but it pulls in torch and model weights (GBs). Not run here.
- **(b) Reuse candidate:** an opt-in `--pdf-engine docling` (or an extra) for table-heavy PDFs.
  Keep pymupdf as the default for speed and footprint. Benchmark it on agent-web's own PDFs first;
  vendor numbers aren't proof.

## Things agent-web is missing that others have

1. **PDF hidden text is silently dropped, not labeled.** In my test PDF, render-mode-3, white and
   0.5pt text all disappeared without a Note. That is inconsistent with the HTML policy, and the
   page also got a false "probably scanned" note. pymupdf exposes render mode, color and bbox, so
   agent-web could label them like ODL's filter does.
2. **Outline / `#fragment` section mode** (fetch-urls-skill): list headings with offsets, or return
   only the section a URL fragment points to. This is cheaper than `--find`, with no loss.
3. **Office docs, EPUB and other non-HTML/non-PDF formats**: markitdown could be an optional fallback.
4. **Site extractors** (defuddle): YouTube transcripts, HN threads and GitHub issues, alongside
   Reddit.
5. **JS rendering**: none of the local alternatives does it cheaply (crawl4ai and Playwright scrapers
   are heavy). agent-web flags these pages; the skill could point to a browser tool (Playwright MCP
   or Claude in Chrome) as the next step.
6. **`Accept: text/markdown`** (Cloudflare "Markdown for Agents", since 2026-02-12;
   https://developers.cloudflare.com/fundamentals/reference/markdown-for-agents/): an edge
   conversion that strips nav and footers, with unknown table fidelity. **Don't** send it by
   default, since it replaces agent-web's converter with Cloudflare's. Site-authored `.md`/`index.md`
   variants could be mentioned in the skill.

## Bugs/quirks in agent-web noticed while using it for this research

- **Search snippets sometimes contain raw Google inline JS.** One for `markitdown microsoft releases
  github` (result 1: `(function(){var src='https://www.googleadservices.com/…`), one for a
  Playwright/MCP query (result 4: `(function(){var id='fld_…'`). Strip `<script>` text from snippet
  nodes.
- **`<time>`/`<relative-time datetime=…>` lose the date.** GitHub release dates print as
  "10 Oct 15:34" with no year, although `datetime="2026-10-10T15:34:53Z"` is in the HTML.
  Consider appending the datetime when the visible text is partial.
- **GitHub pages are noisy with hidden text.** 63 ⟦hidden⟧ labels on /releases are GitHub's UI
  error templates ("Sorry, something went wrong", "No results found"). `--find` then labeled a
  passage with the hidden heading "## No results found" as its section.
