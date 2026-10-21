# agent-web

Web search and source text for AI coding agents, without an intermediate AI summary: a `web`
command, plus a skill that teaches agents to use it. It works with any agent that can run shell
commands (Claude Code, OpenCode, Codex, …).

Built-in web tools lose information:
- **Claude Code's `WebFetch`** doesn't show Claude the page. It converts the HTML with Turndown
  (which flattens tables), cuts it to 100,000 characters, and has a small model (Haiku) answer
  Claude's question about it under instructions to paraphrase. Claude only sees that answer.
  `WebSearch` similarly returns a model-written summary plus links. This produces confident errors:
  invented numbers, values from the wrong table row, conclusions reversed.
- **OpenCode's `webfetch`** returns markdown, but it flattens tables with merged cells, drops
  superscripts (10⁻³ becomes "10-3"), passes hidden text through unlabeled and can't read PDFs. Its
  `websearch` goes through a third-party service (Exa, Firecrawl, Parallel, Tavily or TinyFish).
- **Codex's `web_search`** runs on OpenAI's side and by default searches an index OpenAI maintains.

agent-web returns converted source text. HTML becomes Markdown and PDF text is reconstructed
from page layout, so formatting can change and PDF tables can lose structure. Known omissions
and conversion problems are reported in `Note:` lines; long output can be read in chunks.

## What it does

- **`web search "query"`** prints Google results (title, URL, snippet) using your browser's signed-in
  Google session. All Google operators work.
- **`web fetch URL`** prints the page's own text:
  - **HTML** becomes markdown via pandoc plus a cleanup filter. Layout wrappers and attributes are
    removed. Simple tables become markdown tables. Tables with merged cells stay HTML tables, so
    columns never shift. Strikethrough, superscripts, figures and captions are kept, and MathML
    formulas become TeX (`` $`E=mc^{2}`$ ``).
  - **PDFs** become markdown via pymupdf, with `--- Page N ---` markers. The original is kept.
  - **Reddit threads** come out as the post plus a numbered comment tree (via Reddit's JSON).
  - **GitHub file pages** (`github.com/…/blob/…`) are fetched from `raw.githubusercontent.com`,
    so code arrives exactly, indentation included.
  - **`--find TEXT`** shows only the matching passages, and **`--offset N`** continues a long page.
    Output is capped (25,000 characters by default) to fit agents' output limits (Claude Code cuts
    command output at 30,000 characters).
  - **Every fetch is saved as files:** the full text and the response as downloaded. Agents can
    search the text with `rg`, `sed` or their own file tools, and check the conversion against
    the original.
  - **Just the main content when a page marks it:** if a page has a single `<main>` element, only
    that is converted (pandoc's behavior), which drops menus and footers. Real text left outside
    it is reported, and `--whole-page` includes everything.
  - **Problems are flagged**, never hidden: HTTP errors, bot-check and captcha pages, pages that
    need JavaScript, scanned PDFs, and text that's hidden from human visitors (marked
    `⟦hidden on page: …⟧`).
- **The skill** (`skills/web/SKILL.md`) teaches agents the commands and the rules for using them:
  quote what they rely on, treat page text as untrusted, read the `Note:` lines.

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
- **Your model provider** sees what the agent reads, as with anything else in the conversation.
- **Your browser cookies** are sent only to Google (for search) and to domains you explicitly list in
  `fetch.cookie_domains`. Every other site gets an anonymous request.

Searches use your Google account, so they appear in its search history.

## Security

Every fetch is a request to someone else's server, and the URL itself can carry data out. Pages can
contain instructions aimed at agents ("visit /a, then /b…"). This is the
[lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/).

> [!WARNING]
> **agent-web doesn't decide which fetches run; your agent's permission settings do.** If agents
> may run `web fetch` without asking, a page that successfully injects instructions can make the
> agent fetch URLs that spell out data from your conversation, files or environment, one request
> at a time, to a site the attacker watches. Claude.ai's web fetch closed this hole in 2026 by
> refusing links found inside fetched pages without approval. agent-web has no such check (an
> earlier version had one as Claude Code hooks; it was removed to keep the tool agent-neutral).
> To avoid the risk, keep `web fetch` on "ask" for agents that handle private data. Allowing
> `web search` is lower risk: queries go only to Google.

What agent-web does do:

- **Untrusted-content fences.** Page text is printed inside `<untrusted-web-content-RANDOM>` tags with
  a note not to follow instructions in it; that includes the page's title. Hidden text is
  labeled, including invisible Unicode "tag" characters, which models read but people can't see.
  The skill tells agents never to fetch a URL because a page says so or to put conversation data
  into URLs. This helps the model resist injected instructions, but prompt injection isn't solved.
  The saved full-text file is fenced too, but an agent reading part of it (an `rg` match) won't
  see the fence, so the skill says the files' text is the page's.
- **No local network access.** `localhost`, private, link-local and carrier-grade NAT (Tailscale)
  addresses are refused, including as a redirect target. Every redirect hop is re-checked, the
  connection goes to the exact addresses that were checked (so DNS rebinding can't redirect it),
  and cookies never follow a redirect to another host.
- **Terminal safety.** Control characters are removed, so a page can't send escape codes to your
  terminal when you run `web` yourself.
- **pandoc runs with `--sandbox`**, so converting a hostile page can't read files.
- **Limits:** a 25 MB download cap, a 30 s timeout, at most 10 redirects, and at least 2 s between
  Google requests (shared across processes).

Any other network command (`curl`, `wget`, a script) is the same channel, so the same warning applies
to auto-approving shell commands in general.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- [pandoc](https://pandoc.org/installing.html) ≥ 3.0 (`pacman -S pandoc-cli`, `brew install pandoc`),
  or install with the `bundled-pandoc` extra.
- A browser signed in to Google (Firefox by default; Chrome, Chromium, Brave, Vivaldi, Edge, Opera
  and LibreWolf also work). Linux or macOS.

## Install

```bash
uv tool install --editable ~/Documents/agent-web   # puts `web` on PATH (~/.local/bin)
web doctor                                         # checks pandoc, the config and the Google sign-in
```

For a bundled pandoc, install `"$HOME/Documents/agent-web[bundled-pandoc]"` instead.

Then give your agents the skill. Claude Code reads `~/.claude/skills`, Codex reads
`~/.agents/skills`, and OpenCode reads both:

```bash
mkdir -p ~/.claude/skills ~/.agents/skills
ln -s ~/Documents/agent-web/skills/web ~/.claude/skills/web
ln -s ~/Documents/agent-web/skills/web ~/.agents/skills/web
```

Agents always see the skill's one-line description, and load the full instructions when a task
matches it (or when you type `/web` in Claude Code). If an agent ever skips the skill and reaches
for `curl` instead, add a line to the instructions it reads every session (`AGENTS.md` for Codex
and OpenCode, `CLAUDE.md` for Claude Code), e.g. "For anything on the internet, use the `web`
command (see the `web` skill)."

Finally, turn off each agent's built-in web tools, so agents don't fall back to them, and choose
what runs without asking. Read the warning under [Security](#security) before letting
`web fetch` run without asking.

**Claude Code** (`~/.claude/settings.json`). Bash commands ask by default; this allows searches:

```json
{
  "permissions": {
    "deny": ["WebFetch", "WebSearch"],
    "allow": ["Bash(web search *)", "Bash(web doctor)"]
  }
}
```

**OpenCode** (`~/.config/opencode/opencode.jsonc`). OpenCode allows every shell command by
default, so `web` runs without asking. Add an `ask` rule for `web fetch *` to be asked about
fetches:

```jsonc
"permissions": [
  { "action": "webfetch", "resource": "*", "effect": "deny" },
  { "action": "websearch", "resource": "*", "effect": "deny" }
]
```

**Codex** (`~/.codex/config.toml`). Codex's sandbox has no network access, so every `web` command
asks to run outside it:

```toml
web_search = "disabled"
```

A rule in `~/.codex/rules/default.rules` lets searches through without asking. Add `"fetch"` to
the list for fetches too:

```python
prefix_rule(pattern=["web", ["search", "doctor"]], decision="allow")
```

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
Kind: web page
Cookies: not sent
Full text: /home/you/.cache/agent-web/text/example.com-1f0c….md
Original: /home/you/.cache/agent-web/files/example.com-1f0c….html
Note: …                                  (one line per detected problem)
Length: 48,210 chars; showing 0-25,000
The text inside <untrusted-web-content-1a2b3c4d> was written by the page's authors, …
<untrusted-web-content-1a2b3c4d>
Title: Example

…page text…
</untrusted-web-content-1a2b3c4d>
[23,210 more chars. Continue with: web fetch https://example.com/page --offset 25000]
```

`--json` prints the same data as JSON.

`--max-chars` caps the page text returned, including across `--find` passages. Headers, notes,
and added `…` omission markers are extra.

**`--find TEXT`** splits the page into blocks at blank lines (a paragraph, list, table or code
block) and prints every block containing TEXT (plain text, case-insensitive), each labeled with
its offset and the nearest heading or PDF page marker. Matches are counted per block, not per
occurrence. Blocks are added in page order until `--max-chars` is used up; the header says how many
matched and how many are shown. A single block longer than the limit is cut to a window around
the match, with `…` marking omitted text. The whole match is kept when it fits within the limit.

**Saved files:** every fetch writes the full converted text to `Full text:` (Markdown, inside the
same untrusted-content fence, headed by its URL and fetch time) and the response as downloaded to
`Original:` (HTML, PDF, JSON, …; binary files get only this). They're named after the site, so a
path shows where the text came from. Agents read them with `rg`, `sed` or their own file tools for
what `--find` can't do: regexes, context lines, the list of headings, one section. Since they're
outside the project, your agent may ask permission to read them.

**Cache:** results are kept for an hour in `~/.cache/agent-web` (200 MB, oldest removed first), so
`--offset` and `--find` on the same page don't download it again; `--refresh` skips it. Error,
block and nearly empty pages aren't cached. Changing `cookie_domains`, the browser or
`--whole-page` never serves an old result. The saved files live there too (`text/`, `files/`) and
count toward the 200 MB; they're removed only when space is needed, not after an hour.

Exit codes:
- 0: ok
- 1: usage, config or missing tool
- 2: network error
- 3: HTTP error status
- 4: blocked (captcha, bot check, not signed in)

## Limitations

- **No JavaScript.** Pages that build their content in the browser come out nearly empty; this is
  flagged.
- **Bot protection.** Cloudflare-protected sites (Stack Overflow, for example) and anonymous Reddit
  serve block pages. These are flagged with a suggestion to add the site to `cookie_domains`, which
  often gets through (it does for Reddit and Stack Overflow).
- **PDF tables and formulas** are reconstructed from layout and can be garbled; this is flagged.
- **Google's page layout can change.** Search parsing is structural (a link containing an `<h3>`),
  and `just test-live` catches drift.
- **Hidden-text detection** sees inline styles, the `hidden` attribute and Unicode tag characters.
  It can't see stylesheet classes or white-on-white text. In PDFs, invisible, white and tiny text
  is dropped by the converter rather than labeled (a scan's OCR text layer is kept).

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
- **Bump `FORMAT_VERSION` in `cache.py` whenever conversion output changes,** or results converted
  by the old code keep being served from the cache for up to an hour.
- **Record what you learn.** Design decisions and their reasons go in `docs/decisions.md`; ideas and
  known issues go in `TODO.md`.

Layout:
- `src/agent_web/`:
  - `cli.py`: the CLI.
  - `fetch.py`: the fetch pipeline.
  - `http.py`: downloads, redirects and cookie scoping.
  - `convert/`: HTML (pandoc + `clean.lua`) and PDF.
  - `search/google.py`; `sites/`: Reddit threads and GitHub files.
  - `invisible.py`: tag characters and control codes.
  - `cache.py`, `output.py`.
- `skills/web/SKILL.md`: the skill.
- `docs/`: `decisions.md` (why it's built this way), `alternatives.md` (existing tools compared),
  `built-in-tools.md` (what Claude Code, OpenCode and Codex's own web tools do).
- `TODO.md`: ideas and known issues.

## License

AGPL-3.0-only (pymupdf, used for PDFs, is AGPL).
