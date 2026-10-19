# Ideas and known issues

Not planned yet; kept simple for now. Research behind these: `docs/alternatives.md` and
`docs/decisions.md`.

## Ideas

- **Outline mode:** a flag that prints only a page's headings with their offsets, so an agent can
  jump to a section with `--offset`.
- **Section links:** for a URL with a `#fragment`, return just that section instead of the page
  from the top.
- **PDF tables:** pymupdf garbles some tables (wrong columns on the Attention paper's Table 2). An
  opt-in engine such as Docling would be more accurate but is heavy (GBs of models). For now, have
  the agent use a PDF-specific tool when tables matter.
- **Large PDFs and books:** downloads over 25 MB are refused, and a 700-page book (54.7 MB) takes
  ~70 s to convert (plain text alone takes under a second). Would need a separate size limit and
  reading by page range; the PDF's table of contents could serve as the overview.
- **Other formats:** Office files and EPUB aren't converted (markitdown could be an optional
  fallback).
- **More site rules** like the Reddit one, e.g. YouTube transcripts or Hacker News threads
  (Defuddle has extractors for these). Also from Defuddle: strip line numbers from code blocks.
- **Optional search APIs:** e.g. the Brave Search API (the backend behind Claude Code's
  WebSearch) or Firecrawl's search, configured with an API key, as an alternative to Google
  cookies or a fallback when Google shows a captcha or the browser isn't signed in. Queries would
  then go to that provider, and results should stay in the same title/URL/snippet format.
  Fetching would stay local.
- **Pages that need JavaScript** are only flagged. The skill could point the agent to a browser
  tool as the next step.

## Known issues

- **GitHub's hidden UI templates add noise.** GitHub pages carry error and empty-state templates
  ("Sorry, something went wrong.", "Uh oh! There was an error while loading", "No results found")
  marked `hidden`, which JavaScript shows only when a widget fails. They're labeled
  `⟦hidden on page: …⟧` like any hidden text. On `github.com/astral-sh/uv/releases` that was 61
  labels with only 14 distinct texts (~4% of the page, ~1,000 tokens). Effects: extra tokens, a
  hidden-text note that is a false alarm (which may teach agents to ignore it), and `--find`
  sometimes naming a hidden heading like "## No results found" as a passage's section. Issue and
  repo home pages tested had none (the templates sit outside `<main>`). A tried fix, labeling each
  distinct hidden passage once and dropping exact repeats (~20 lines), cut it to 15 labels; it was
  left out to keep the code simple.
- **Hidden text in PDFs** (invisible, white or tiny text) is dropped by pymupdf rather than labeled,
  unlike HTML. A scan's OCR text layer over a page image is kept.
- **Short PDFs can get a false "probably scanned" note:** the check is fewer than 50 characters of
  text per page, which a one-page PDF with a single sentence fails.
- **Cloudflare-protected sites** (e.g. Stack Overflow) block anonymous requests from the user's IP
  whatever browser fingerprint curl_cffi uses. Sending cookies (`cookie_domains`) gets through.
  For Stack Overflow, the Stack Exchange API also works anonymously.
- **Not used on purpose:** Cloudflare's "Markdown for Agents" (`Accept: text/markdown`) would swap
  agent-web's converter for Cloudflare's, whose table handling is unknown.
