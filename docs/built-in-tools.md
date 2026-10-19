# Agents' built-in web tools (verified 2026-10-19)

What the built-in tools of Claude Code, OpenCode and Codex actually give the model, and how each
agent's skills, permissions and hooks work. This backs the claims in the README and the decision
to remove agent-web's hooks ([decisions.md](decisions.md)).

## Claude Code (v2.1.302)

Method: Claude Code isn't open source. Its native binary embeds the JavaScript bundle as plain
text, which was read directly, and cross-checked against the readable v2.1.88 source. Details
below can change between versions.

**WebFetch**
- The page is converted with **Turndown** (whole page, no content extraction; `style`, `script`,
  `noscript` and `iframe` removed). Turndown has **no table support** here: every cell becomes its
  own paragraph, so tables arrive as a vertical list of values.
- The result (first 100,000 characters) goes to the **"small fast model"** (Haiku by default; set
  by `ANTHROPIC_SMALL_FAST_MODEL`) with the model's question. Its instructions cap quotes at 125
  characters and say everything else must not be word-for-word. **The main model only sees that
  answer**, which explains invented numbers and wrong-row values.
- Exception: on ~96 preapproved documentation domains, a `text/markdown` response under 100K
  characters is passed through without the summarizer. A built-in `web-fetch` subagent also gets
  verbatim text.
- Exfiltration: local sessions do a direct GET with **no provenance check**. Guards are a domain
  blocklist preflight, preapproved docs domains, and per-domain permission rules; once a domain is
  approved with "don't ask again", any path on it runs unasked. Provenance checks exist only on
  the cloud proxy path. (claude.ai's own web fetch was fixed in 2026 to stop following links from
  fetched pages: [Simon Willison](https://simonwillison.net/2026/Jul/15/claude-web-fetch-exfiltration/).)
- Disable with `"permissions": {"deny": ["WebFetch"]}` (or `CLAUDE_CODE_DISABLE_WEB_FETCH=1`).

**WebSearch**
- A secondary conversation on the main session's model (Haiku behind a feature flag) runs the
  search and writes a summary. The main model gets result titles and URLs (no snippets) plus that
  **model-written summary**. The backend is Brave Search.

**Skills and permissions**
- Skill names and descriptions are in context every turn; the full `SKILL.md` loads when Claude
  invokes it or the user types `/name`. `disable-model-invocation: true` hides the description.
  Personal skills live in `~/.claude/skills/<name>/SKILL.md`.
- Bash permission rules match command prefixes (`Bash(web search *)`) and understand `&&`, `|`
  and `;`, so an allow rule doesn't approve a chained command.

## OpenCode (v2.0.32, V2 config)

**webfetch** returns markdown (no summarizer), but on agent-web's test pages it:
- flattened a table with merged cells into misaligned lines;
- turned the exponent 10⁻³ into "10-3";
- passed `display:none` prompt-injection text through **unlabeled**;
- can't read PDFs or other binary files.

Strikethrough, figures and citations came through. **websearch** only works through third-party
providers (Exa, Firecrawl, Parallel, Tavily, TinyFish).

**Permissions:** ordered rules in `opencode.jsonc` (`permissions: [{action, resource, effect}]`,
last match wins). **The default policy allows everything, including any shell command**, so `web`
runs without asking unless you add an `ask` rule. Built-in tools are disabled with
`{ "action": "webfetch", "resource": "*", "effect": "deny" }` (same for `websearch`).

**Skills:** read from `~/.config/opencode/skills`, `~/.claude/skills` and `~/.agents/skills`
(plus project folders); descriptions are advertised and the `skill` tool loads the full file.

**Plugins** are TypeScript. A `permission.hook("evaluate")` can change any allow/ask decision,
and `tool.hook("execute.before")` can inspect tool input. A provenance gate would need its own
plugin.

## Codex (CLI 0.163.0)

**web_search** is a hosted tool that runs on OpenAI's side. Modes: `disabled`, `cached` (default:
an OpenAI-maintained index, no live access), `indexed`, `live` (default under full access).
Hooks can't see it. Disable with `web_search = "disabled"` in `~/.codex/config.toml`.

**Sandbox:** commands run without network access by default, so every `web` call asks to run
outside the sandbox. `approvals_reviewer = "auto_review"` makes a reviewer model answer those
requests instead of the user. **Rules** in `~/.codex/rules/*.rules` allow command prefixes:
`prefix_rule(pattern=["web", ["search", "doctor"]], decision="allow")` (checked with
`codex execpolicy check`).

**Skills:** read from `.agents/skills` up to the repo root and `~/.agents/skills`; the list of
descriptions has a size budget, and the full `SKILL.md` is read when Codex selects a skill.

**Hooks** look almost like Claude Code's (same JSON shape; plugin hooks even get
`CLAUDE_PLUGIN_ROOT`), with differences that matter:
- In `PreToolUse`, `permissionDecision: "ask"` is **not supported**: Codex marks the hook as failed
  and **runs the tool call anyway**. Only `deny` (and `allow` with rewritten input) work.
- `PermissionRequest` runs just before an approval prompt and can allow or deny it; that is where
  a gate would have to live.
- Every hook must be reviewed and trusted once (`/hooks`), and is skipped until it is.
