# Design decisions

Why agent-web is built the way it is, including what was tried and rejected. Newest changes are
noted with their commit. Supporting research: [alternatives.md](alternatives.md) and
[built-in-tools.md](built-in-tools.md).

## Guiding priorities

1. **Accuracy first.** Give the agent source text without an intermediate AI summary. Preserve
   meaningful structure during conversion and report known omissions and conversion problems.
   Token savings that lose information (dropping link URLs, navigation or footers, article
   extraction) were declined.
2. **No middlemen without a reason.** Nothing sits between the agent and the source unless it
   earns its place. Today search goes straight to Google and pages are fetched directly from the
   user's machine, so no proxy, API or hosted converter is involved. Services aren't ruled out:
   an optional search API (Brave Search API, Firecrawl) could be added later, e.g. as a fallback
   when Google's cookies don't work (see TODO.md). A hosted page fetcher or converter would
   replace agent-web's own conversion, so it would have to match its fidelity (merged-cell tables,
   superscripts, labeled hidden text) to be worth it. The model provider sees what the agent reads,
   as with anything else.
3. **Simple.** A command and a skill. Rules are added only when the generic path gives wrong
   output.

## Shape: a CLI plus one skill, for any agent (`ca1c28e`)

- `web` is a normal command, so any agent with a shell can use it. A skill (`skills/web/SKILL.md`)
  teaches it; Claude Code, OpenCode and Codex all read the same file.
- **The Claude Code plugin and its hooks were removed.** The hooks redirected WebFetch/WebSearch
  and ran a *provenance gate*: a fetch ran without asking only if the URL came from the user's
  message or a search result; links found inside pages needed approval (the fix Anthropic applied
  to claude.ai after the letter-by-letter exfiltration attack). A `trusted_domains` list let
  agents browse chosen sites. They were removed to keep the tool agent-neutral:
  - Codex's PreToolUse hook doesn't support "ask": it records the hook as failed and **runs the
    command anyway**, so the same hook would let unapproved fetches through.
  - OpenCode would need a separate TypeScript plugin.
- **Accepted risk:** each agent's own permission settings now decide what runs without asking. If
  `web fetch` is auto-approved, a prompt injection can exfiltrate data through fetched URLs. The
  user accepted this; the README's Security section carries the warning.
- Also rejected: refusing cross-site redirects (too much friction for a small gain) and protecting
  the config file from agent edits (too paranoid).
- **The skill doesn't tell agents how to present answers.** Its job is getting the agent real
  page text and using it safely. It used to require quoting every fact taken from a page, then
  quoting numbers and exact wording; agents (GPT-6.1 Sol, GLM 5.3) filled their answers with
  quotes. Citing and quoting are up to the user and their agent.
- **The skill leaves the reading strategy to the agent.** It used to call reading a long page from
  the top a mistake and push `--find`. Reading a whole page is right when all of it matters, and
  `--find` only matches exact words, so an empty result can make an agent wrongly conclude a page
  doesn't cover something. The skill now describes both ways and that limit.

## Search: Google with the user's browser cookies

Chosen because it is direct and was the only free approach that worked from the user's IP. Paid
search APIs weren't needed for that; they remain an option for later (see TODO.md).

- Every keyless approach was blocked or captcha'd from the user's IP: ddgs (DuckDuckGo and
  others), DonSeTch's search, Google's no-JavaScript/Nokia lane (what SearXNG uses), plain Google.
  "If it doesn't work from my IP, it doesn't work."
- Google answers requests carrying a signed-in session with a normal results page. Cookies come
  from the configured browser (Firefox by default) and go only to Google hosts.
- Results are parsed structurally (a link containing an `<h3>`) rather than by Google's obfuscated
  class names. `just test-live` catches layout drift.
- At least 2 s between Google requests, shared across processes (a file lock), to protect the
  account. Searches appear in the account's search history.
- **After a captcha or JavaScript check, searches pause for 10 minutes**, shared through the same
  locked state file, and `web search` says so without contacting Google. On 2026-10-21 a research
  session ran about 18 searches in two minutes (the 2 s spacing allows ~30 requests a minute),
  then started five agents searching in parallel. Google answered with its JavaScript check and
  twice with its `/sorry/` captcha, and the agents kept retrying for 17 minutes (some with
  `sleep 45`), almost all blocked. The old message blamed expired cookies, which sent agents
  into retry loops; the cookies were fine. A search 2.5 hours later worked.
- **Each search is one request for one results page** (`--page N`, 1-5, default 1), and all of
  its 8-10 results are returned. The output ends with the command for the next page, like
  `web fetch`'s `--offset` hint. A result count (`-n`, removed without an alias) either made a
  second request for the last one or two results or fetched a page and threw most of it away;
  the default `-n 10` usually cost two requests. A page count (`--pages N`, briefly) fetched
  pages 1 to N, so asking for more after a first search fetched page 1 again.

## Fetching

- Local download with curl_cffi browser impersonation (TLS fingerprint of a real browser). When
  cookies are sent, the impersonated browser matches the one the cookies came from.
- **Cookies only for domains the user lists** (`fetch.cookie_domains`, default empty). With every
  cookie, the agent would be logged in as the user everywhere (mail, GitHub, bank), so injected
  instructions could make it read private pages; some sites also act on plain GET links (logout,
  unsubscribe). Cookies are re-checked on every redirect hop and never follow a redirect to another
  host. Sites that block anonymous requests (Reddit, Stack Overflow behind Cloudflare) work once
  listed; the output then suggests listing a blocked site, asking the user first (`40cac6a`).
- **No local network access** unless `allow_private_networks` is set: loopback, private,
  link-local, carrier-grade NAT (Tailscale), reserved and multicast addresses are refused, on every
  redirect hop. Each connection is pinned to the addresses that passed the check (curl `RESOLVE`),
  so DNS rebinding can't switch it to a local service (`aac4e9a`).
- Limits: 25 MB (counted after decompression, so compression bombs are refused), 30 s timeout,
  10 redirects.

## Converting HTML

- **pandoc, not article extraction.** An lxml pre-pass, then pandoc
  (`-t gfm-tex_math_dollars --wrap=none --sandbox`) with a Lua cleanup filter (`clean.lua`).
  Article extractors were cheaper but lost content: trafilatura dropped a Wikipedia table's numbers
  and took sidebar headlines instead of a news article; DonSeTch's pipe tables shifted merged
  cells into the wrong columns.
- **Tables with merged cells stay HTML tables** (rowspan/colspan kept, attributes stripped), so
  values never shift columns. Simple tables become pipe tables.
- Strikethrough, superscripts and subscripts, figures and captions are kept. MathML becomes TeX;
  hidden MathML copies next to a formula image (Wikipedia) are dropped so formulas appear once.
- **A single `<main>`: only it is converted** (pandoc's behavior), which drops menus and footers.
  Real text left outside it is counted and reported, and `--whole-page` converts everything.
- **Hidden text is labeled, not removed**: `⟦hidden on page: …⟧` (inline `display:none` and
  similar, the `hidden` attribute, fragments of 12+ characters), `<noscript>` content, and
  invisible Unicode tag characters. Removing it would hide the fact that a page addresses agents
  invisibly. Stylesheet-based hiding can't be seen without a browser.
- In-page links become `#fragment` and permalink anchors (`¶`, `#`, an icon or a zero-width
  space) are dropped (cheap, lossless).
- `<time>` dates shown without a year get their machine-readable timestamp.
- libxml2 (lxml) predates HTML5 and, when `<body>` is implicit, puts `<main>`, `<section>` and the
  rest into `<head>`, where pandoc ignores them. The pre-pass moves them back.
- Block elements (`<div>`, `<p>`, `<section>`, …) and `<br>` inside headings are unwrapped before
  pandoc, after hidden-text labeling (`20b1570`, `004736a`). Otherwise pandoc splits the title into
  an empty heading and separate paragraphs, drops the heading, or breaks it at the line break,
  losing section context for `--find`. Spaces preserve word boundaries; inline formatting stays.
- If pandoc fails or keeps less than half of the visible text, plain text is shown instead, with a
  note.

## PDFs

- pymupdf4llm, no OCR, with `--- Page N ---` markers; the original PDF is saved like any download.
  pymupdf is AGPL, hence the project's license.
- Every PDF gets a note that tables and formulas can be garbled. Table structure is the weakest
  spot (wrong columns on the Attention paper's Table 2). Docling scores far better in a
  vendor-run benchmark but needs gigabytes of models; the plan is for the agent to use a
  PDF-specific tool when tables matter.
- Large books aren't supported (25 MB cap; a 700-page book takes ~70 s to convert). See TODO.md.

## Site rules

Only where the generic path gives wrong output:
- **Reddit threads** use the `.json` endpoint: the post plus a numbered comment tree.
- **GitHub file pages** (`/blob/`) are fetched from `raw.githubusercontent.com`, because the page
  loses code indentation.

Declined: a Stack Overflow rule (cookies work) and handling GitHub's repeated hidden UI templates
(harmless noise, not worth the code; see TODO.md).

## Output

- **Problems are flagged, never hidden.** `Note:` lines for HTTP errors, bot-check pages, pages that
  need JavaScript, nearly empty pages, scanned PDFs, hidden text and fallbacks, plus exit codes.
  There is no silent fallback to another source.
- **Untrusted blocks hold only what the page wrote.** The title, the text and each `--find`
  passage get their own `<untrusted-web-content-RANDOM>` block (one random tag per output, so a
  page can't close it), after one note not to follow instructions inside. Agent-web's labels
  (`Title:`, `Length:`, `Passage 3 of 20, offset …`) stay outside, so the agent can tell them
  apart from page text, and a page can't fake them. Passages used to share one block with
  `[offset …]` labels inside it. The exceptions are agent-web's `⟦…⟧` labels and
  `--- Page N ---` markers, which sit between pieces of page text. A passage's heading is page
  text, so it opens the block, and only its offset goes in the label. The label gives the
  passage's end offset too, so an agent can read on after it, not only before, and the offset of
  each match (the first ten): a cut-down table's span is the whole table, so without them an agent
  couldn't jump to the matching row. Matching is case-insensitive through a regex rather than
  `lower()`, which changes the length of some strings (`"İ".lower()` is two characters) and would
  shift the offsets. Control characters
  are removed so pages can't send escape codes to a terminal.
- **Skipped text is marked `⟦72 lines left out⟧` / `⟦2,831 characters left out⟧`,** not with a
  bare `…`. Pages write `…` themselves, so a bare one couldn't be told apart from page text, and
  the size says how much is missing. The `⟦⟧` brackets are the ones agent-web already uses for
  `⟦hidden on page: …⟧` and `⟦image: …⟧`; pages almost never contain them.
- 25,000 characters per call by default, because Claude Code cuts command output at 30,000.
  The budget caps page text; metadata and `⟦… left out⟧` marks are extra. `--offset` continues;
  `--find` prints only matching blocks and honors smaller budgets too. A table, list or code block
  over 2,000 characters (or too big for the rest of the budget) shows its first line, a table's
  `|---|` rule and its matching lines. Before that, a page's whole config table (50,656 characters,
  7 matching rows) came back as a 25,000-character slice that used the budget up: 1 of 20 matches
  shown. Now all 20 fit in 6,700 characters. If the first match still doesn't fit, it is windowed
  around the match, keeping the whole match when it fits.
- **Every fetch is also saved as files** (`1129503`), and the output prints both paths: the full
  text (`Full text:`), fenced like the printed output and headed by its URL and fetch time, and
  the response as downloaded (`Original:`). Agents already know `rg`, `sed` and their own file
  tools, so they can do what `--find` can't (a regex, context lines, the headings, one section)
  without `web` growing a flag for each. The original lets them check the conversion, e.g. a garbled
  table. The page is still printed by default: most fetches are meant to be read, and printing
  only the paths would make each one two calls.
- Declined: a flag that pipes the text through a command the agent supplies (`--filter 'rg …'`),
  which would keep its output inside the fence. People allow `web fetch` as a read-only command,
  and the flag would let it run any command without approval (even `rg --pre`, `sed`'s `e` and
  `awk`'s `system()` run programs). Reading the saved file loses the fence from view instead; the
  skill tells agents the file's text is the page's.

## Cache

- One hour. Error, block and nearly empty pages aren't cached (a cached Reddit block page once
  kept being served after cookies were added, `47ab58e`).
- The key includes whether (and from which browser) cookies are sent and `--whole-page`, so
  changing those settings never serves an old result.
- `FORMAT_VERSION` in `cache.py` is bumped whenever conversion output changes.
- Saved files (`text/`, `files/`) share the 200 MB limit and are removed oldest first, not after
  an hour, so paths printed earlier usually stay valid. A cache entry whose files are gone is a
  miss, since its output would point to them. A result that isn't cached (an error or block page
  after `--refresh`) still overwrites the files, so it also removes the page's older entry;
  otherwise that entry would be served with the error page's files.

## Code conventions

- Expected failures, content kinds and problems are sum types (`failures.py`, `model.py`), returned
  rather than raised and matched exhaustively with `assert_never`. Exceptions are for bugs.
- pyright strict with blanket `# type: ignore` disabled; ruff lint and format; pytest with warnings
  as errors. Every conversion rule has a fixture test; network tests use a local server; live tests
  are opt-in (`just test-live`).
