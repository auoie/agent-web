# Ideas and known issues

Not planned yet; kept simple for now. Research behind these: `docs/alternatives.md` and
`docs/decisions.md`.

## Ideas

- **Outline mode, section links (`#fragment`), context around `--find` matches:** for now agents
  do these with `rg` and `sed` on the saved full-text file (`rg -n '^#'`, `rg -A 8 '^## Heading'`,
  `rg -C 3`). Build them into `web` only if agents keep missing that. The case that prompted the
  context idea: `--find 'Security Considerations'` on the MCP spec's tools page returns the table
  of contents and the bare heading, not the section.
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
- **Tuning the Google budget:** 20 searches per 10 minutes comes from one day of blocks (after
  25-38). Only refreshes are logged; logging each block with the number of searches before it
  would show whether 20 is too strict or too loose.
- **Firecrawl as another fallback engine:** its free plan needs no card (500 searches a month,
  10 a minute), so it could serve users without a Brave key.
- **Refreshing Google's token before it runs out:** a browser gets a fresh `__Secure-STRP` (5
  minutes) on every search, agent-web only after Google challenges it. If
  `~/.cache/agent-web/google-refresh.log` shows a refresh on nearly every search during busy
  periods, refresh ahead of time instead (one headless Firefox run every few minutes while agents
  search).
- **Pages that need JavaScript** are only flagged. The skill could point the agent to a browser
  tool as the next step, or `web fetch --render` could run the page in headless Chrome and convert
  the result as usual (same notes, untrusted blocks and saved files). A prototype was tested on
  2026-10-21 and then shelved; what it found:
  - **Cases it would cover:** apps that are empty until JavaScript runs (Oracle Recruiting on
    `jobs.akamai.com`), Akamai's and Cloudflare's JavaScript challenges (`evergreenhealth.com`,
    `careers.inl.gov`), and content inside shadow DOM, which `chrome --dump-dom` misses.
  - **Driving Chrome needs no library:** `--remote-debugging-pipe` speaks the DevTools protocol
    over file descriptors 3 and 4 (null-terminated JSON), which `os.posix_spawn` can set up.
  - **Headless Chrome 156's giveaways and their fixes,** checked on a local page that reports what
    scripts see:
    - The user agent says `HeadlessChrome` (careers.pnnl.gov answers 403, Cloudflare challenges
      it). `Emulation.setUserAgentOverride` with the browser's own client-hint metadata fixes it
      for the page and its dedicated workers. The `--user-agent` flag fixes the string but empties
      the detailed client hints (`fullVersionList`), which is its own giveaway. The client-hint
      brands already say "Google Chrome".
    - `navigator.webdriver` is true when driven over the pipe:
      `--disable-blink-features=AutomationControlled`.
    - The screen is 800×600: `--screen-info={0,0 1920x1080}`.
    - `outerWidth`/`outerHeight` read 0 if the page loads before the window reports its size:
      wait for `outerWidth > 0` on `about:blank` first.
    - WebGL reports the SwiftShader software renderer: `--enable-gpu` reports the real GPU, as a
      normal Linux Chrome does (+0.5 s startup).
    - Never call `Runtime.enable` (a known way pages detect DevTools); `Runtime.evaluate` works
      without it.
  - **Results with those fixes:** deviceandbrowserinfo.com/are_you_a_bot said "You are human"
    with every check false (CDP, headless, client hints, workers, iframes), BrowserScan's bot
    detection was "Normal" throughout, bot.sannysoft.com passed every row, and careers.inl.gov
    loaded its job search where plain headless Chrome got "Just a moment".
  - **Cost:** Chrome's start and exit take 0.4 s; a render took about 3.5 s against 1.5 s for a
    plain fetch, so a long-running Chrome isn't worth it.
  - **Reading the page:** `DOM.getDocument({depth: -1, pierce: true})` returns open and closed
    shadow roots (slotted nodes carry `assignedSlot`, slots carry `distributedNodes`) and same-site
    frames' documents. Cross-site frames are separate targets: auto-attach with `flatten`, match
    each by the owner element's `frameId` (equal to the target's id), and read its document there.
  - **Unsolved:** knowing when a page is done (5 of 7 test pages never went network-idle because
    of ads, analytics and long polling, so it needs the load event, a quiet window that ignores
    long-lived requests, and a cap); setting the user agent in frames and workers before their
    scripts run (auto-attach with `waitForDebuggerOnStart`, and browser-level auto-attach for
    service workers); keeping pages off the local network (the browser fetches subresources
    itself; a SOCKS5 proxy inside agent-web with `--proxy-bypass-list=<-loopback>` could apply
    `allow_private_networks`); cookies for `cookie_domains`; and pages that aren't HTML.

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
- **Google's JavaScript-wall detection is a heuristic.** Every results page carries a hidden
  "enable JavaScript" fallback, so a page with no parsed results is called a JavaScript wall
  unless it says "did not match any documents" (searches use `hl=en`). A `site:` search with no
  matches was once misreported that way, and the agent concluded search was blocked. Another
  zero-result wording, or a layout change that breaks the parser, would show up the same way, and
  since a JavaScript check now pauses searches for 10 minutes, a misreport would cost that pause.
  The raw page isn't saved, so a misreport can't be checked afterwards.
- **Not used on purpose:** Cloudflare's "Markdown for Agents" (`Accept: text/markdown`) would swap
  agent-web's converter for Cloudflare's, whose table handling is unknown.
