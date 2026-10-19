# agent-web

Verbatim web search and page reading for AI coding agents: a `web` command and a Claude Code plugin.

Claude Code's built-in `WebFetch` doesn't show Claude the page. It converts the HTML with Turndown
(which flattens tables), cuts it to 100,000 characters, and has a small model (Haiku) answer Claude's
question about it under instructions to paraphrase. Claude only sees that answer. `WebSearch` similarly
returns a model-written summary plus links. This produces confident errors: invented numbers, values
from the wrong table row, conclusions reversed. agent-web prints the source itself.

## What it does

- **`web search "query"`** prints Google results (title, URL, snippet) using your browser's signed-in
  Google session. All Google operators work.
- **`web fetch URL`** prints the page's own text:
  - **HTML** becomes markdown via pandoc plus a cleanup filter. Layout wrappers and attributes are
    removed. Simple tables become markdown tables. Tables with merged cells stay HTML tables, so
    columns never shift. Strikethrough, superscripts, figures and captions are kept.
  - **PDFs** become markdown via pymupdf, with `--- Page N ---` markers. The original is kept.
  - **Reddit threads** come out as the post plus a numbered comment tree (via Reddit's JSON).
  - **`--find TEXT`** shows only the matching passages, and **`--offset N`** continues a long page.
    Output is capped (25,000 characters by default) to fit Claude Code's 30,000-character Bash limit.
  - **Just the main content when a page marks it:** if a page has a single `<main>` element, only
    that is converted (pandoc's behavior), which drops menus and footers. Real text left outside
    it is reported, and `--whole-page` includes everything.
  - **Problems are flagged**, never hidden: HTTP errors, bot-check and captcha pages, pages that
    need JavaScript, scanned PDFs, and text that's hidden from human visitors (marked
    `⟦hidden on page: …⟧`).
- **The Claude Code plugin:**
  - Teaches agents the command (skill `web`).
  - Redirects `WebFetch`/`WebSearch` to it.
  - Auto-approves fetches only for URLs you typed or a search returned (see Security).

## Token cost

Measured on 2026-10-19 with `bench/tokens.py`. Every tool reads the same downloaded bytes.
Counts use tiktoken's o200k_base as a proxy, since Claude's tokenizer isn't public: absolute
numbers differ, ratios hold. ✗ means the page's real content (a sentence, or a number from the
table) is missing.

| Page | `curl` | `curl` + html2text | `curl` + pandoc | WebFetch → Haiku | trafilatura | DonSeTch | DonSeTch `--focus` | **agent-web** | agent-web `--find` |
| :- | -: | -: | -: | -: | -: | -: | -: | -: | -: |
| Wikipedia table | 124,154 | 23,462 | 64,828 | 23,537 | 1,647 ✗ | 3,222 | 1,482 | **17,809** | 7,566 |
| News article | 43,875 | 7,138 | 8,916 | 6,864 | 564 ✗ | 967 | 249 | **5,249** | 230 |
| Blog post | 6,410 | 2,277 | 2,943 | 2,228 | 1,727 | 1,811 | 466 | **2,213** | 232 |
| Link post | 5,361 | 1,621 | 2,583 | 1,313 | 550 | 553 | 395 | **1,562** | 355 |
| Python docs | 95,016 | 22,305 | 81,951 | 24,979 | 20,297 | 18,978 | 2,035 | **25,529** | 358 |
| GitHub README | 129,674 | 6,736 | 70,188 | 6,644 | 1,307 | 1,443 | 174 | **5,866** | 1,346 |
| Tiny page | 310 | 43 | 39 | 43 | 33 | 60 | 54 | **38** | 8 |
| **Total** | 404,800 | 63,582 | 231,448 | 65,608 | 26,125 | 27,034 | 4,855 | **58,266** | 10,095 |

- **Against raw `curl`:** about 86% fewer tokens (7×). Against `curl` + plain pandoc, 75% fewer.
- **Against a plain converter (html2text):** about the same size (8% fewer). agent-web's edge
  there is fidelity: merged table cells, strikethrough, hidden-text labels and problem notes.
- **Against article extraction (trafilatura):** trafilatura is cheapest, but it dropped the table's
  numbers on Wikipedia and grabbed sidebar headlines instead of the article on The Register.
- **Against DonSeTch** (v4.10.1): DonSeTch is about half the size, for three reasons:
  - It drops link URLs (21% of agent-web's tokens on these pages).
  - It extracts the article instead of converting the page.
  - It writes every table as a pipe table.

  The last two cost fidelity. On the Wikipedia table, merged cells shift values into the wrong
  column (China National Petroleum's revenue reads 25.2 instead of 476), and icon-only cells
  vanish. In PDFs it lost exponents (`2.3 · 1019`) and split cells. It also drops strikethrough
  and figures. Its keyless search was blocked from the test machine's IP.
- **Against WebFetch:** the WebFetch column is what *Haiku* reads. Claude itself gets only Haiku's
  short answer, which is the cheapest for Claude and also the source of the made-up details.
- **`--find`** reads only matching passages: about 83% fewer tokens than the full page.

Reproduce:

```bash
uv run --with tiktoken --with trafilatura --with html2text bench/tokens.py
```

Set `TURNDOWN_DIR` to a folder with `node_modules/turndown` to include the WebFetch column.

## Privacy

There are no middlemen and no proxies:
- **Searches** go from your machine to Google.
- **Pages** are fetched directly from your machine.
- **Anthropic** sees what the agent reads, as with any Claude conversation.
- **Your browser cookies** are sent only to Google (for search) and to domains you explicitly list in
  `fetch.cookie_domains`. Every other site gets an anonymous request.

Searches use your Google account, so they appear in its search history.

## Security

Every fetch is a request to someone else's server, and the URL itself can carry data out. Pages can
contain instructions aimed at agents ("visit /a, then /b…"). This is the
[lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/). agent-web limits it:

- **Provenance gate (plugin hooks).**
  - A `web fetch` runs without asking only if the URL appeared in your own message (typed or pasted)
    or in the results of a `web search` in the same session.
  - Links found inside fetched pages, URLs inside search snippets, compound shell commands
    (`|`, `&&`, `;`, `$(…)`, redirects) and shell variables all go to you for approval.
  - Anthropic closed the same hole in claude.ai's web fetch in 2026.
- **Untrusted-content fences.** Page text is printed inside `<untrusted-web-content-RANDOM>` tags with
  a note not to follow instructions in it. Hidden text is labeled. This helps the model resist
  injected instructions, but prompt injection isn't solved.
- **No local network access.** `localhost`, private and link-local addresses are refused, including
  as a redirect target. Every redirect hop is re-checked, and cookies never follow a redirect to
  another host.
- **Limits:** a 25 MB download cap, a 30 s timeout, at most 10 redirects, and at least 2 s between
  Google requests (shared across processes).

Plain `curl` in Bash is still an exfiltration channel. Don't auto-approve Bash for agents that read
the web.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- [pandoc](https://pandoc.org/installing.html) ≥ 3.0 (`pacman -S pandoc-cli`, `brew install pandoc`),
  or install with the `bundled-pandoc` extra.
- A browser signed in to Google (Firefox by default; Chrome, Chromium, Brave, Vivaldi, Edge, Opera
  and LibreWolf also work). Linux or macOS.

## Install

**As a Claude Code plugin** (from this checkout):

```bash
claude plugin marketplace add ~/Documents/agent-web
claude plugin install agent-web@agent-web
```

Or try it for one session with `claude --plugin-dir ~/Documents/agent-web`. Once the repo is on GitHub:

```
/plugin install agent-web --marketplace <owner>/<repo>
```

The plugin puts `web` on the agent's PATH. The first call builds a runtime environment from the
locked dependencies. Then run `web doctor` to check pandoc, the config and the Google sign-in.

**As a standalone CLI:** `uv tool install ~/Documents/agent-web` (add `[bundled-pandoc]` to ship
pandoc).

## Configuration

Optional. `~/.config/agent-web/config.toml` (print a commented example with `web config --example`):

```toml
[cookies]
browser = "firefox"

[fetch]
cookie_domains = ["reddit.com"]   # sites that get your browser session; default: none

[search]
min_interval_seconds = 2.0
```

Unknown keys are errors, so a typo can't silently fall back to a default. `web config` shows the
effective settings.

## Output and exit codes

```
Fetched: https://example.com/page (HTTP 200, text/html)
Title: Example
Kind: web page
Cookies: not sent
Note: …                                  (one line per detected problem)
Length: 48,210 chars; showing 0-25,000
The text inside <untrusted-web-content-1a2b3c4d> was written by the page's authors, …
<untrusted-web-content-1a2b3c4d>
…page text…
</untrusted-web-content-1a2b3c4d>
[23,210 more chars. Continue with: web fetch https://example.com/page --offset 25000]
```

`--json` prints the same data as JSON.

Exit codes:
- 0: ok
- 1: usage, config or missing tool
- 2: network error
- 3: HTTP error status
- 4: blocked (captcha, bot check, not signed in)

## Limitations

- **No JavaScript.** Pages that build their content in the browser come out nearly empty; this is
  flagged.
- **Bot protection.** Sites like Cloudflare-protected pages or anonymous Reddit serve block pages;
  these are flagged. Adding the domain to `cookie_domains` often helps.
- **PDF tables and formulas** are reconstructed from layout and can be garbled; this is flagged.
- **Google's page layout can change.** Search parsing is structural (a link containing an `<h3>`),
  and `just test-live` catches drift.
- **Hidden-text detection** sees inline styles and the `hidden` attribute. It can't see stylesheet
  classes or white-on-white text.

## Development

```bash
just check       # ruff format + lint, pyright (strict), pytest with coverage
just test-live   # real Google search and pages, using your cookies
just fix         # apply formatting and safe lint fixes
```

Conventions:
- **Expected failures are a sum type** (`failures.py`), returned as `T | Failure` instead of raised,
  and matched exhaustively (`assert_never`) for exit codes and messages. Content and issues are sum
  types too (`model.py`). Exceptions are for bugs.
- **pyright runs in strict mode with blanket `# type: ignore` disabled.** The few suppressions are
  rule-specific `# pyright: ignore[…]` at boundaries with partly typed libraries.
- **Every cleanup rule has a fixture test.** That covers the Lua filter in
  `src/agent_web/convert/clean.lua` and the lxml pass in `convert/html.py`.
- **Network tests use a local HTTP server** (`tests/conftest.py`). The Google parser is tested
  against a sanitized real results page.

Layout:
- `src/agent_web/`:
  - `cli.py`: the CLI.
  - `fetch.py`: the fetch pipeline.
  - `http.py`: downloads, redirects and cookie scoping.
  - `convert/`: HTML (pandoc + `clean.lua`) and PDF.
  - `search/google.py`, `sites/reddit.py`.
  - `hooks.py` and `provenance.py`: the Claude Code hooks.
  - `cache.py`, `output.py`.
- `.claude-plugin/`, `bin/web`, `hooks/` and `skills/web/`: the Claude Code plugin.

## License

AGPL-3.0-only (pymupdf, used for PDFs, is AGPL).
