"""Claude Code hooks: replace WebFetch/WebSearch with `web`, and gate fetches on provenance.

Invoked as `python -m agent_web.hooks <event>` with the hook payload on stdin:

- pre-tool-use        WebFetch/WebSearch → deny, pointing at `web`.
                      Bash `web …` → allow searches and fetches of URLs with provenance,
                      ask for any other fetch or anything that isn't a simple `web` command.
- user-prompt-submit  record URLs the user typed or pasted.
- post-tool-use       record result URLs printed by a successful `web search`.

A fetch is auto-approved only for URLs the user provided or a search returned. Links found
inside fetched pages need approval: that is how a page could make an agent send data out.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import dataclass
from typing import assert_never

from agent_web.cli_args import build_parser
from agent_web.jsonvalue import as_dict, get_str
from agent_web.provenance import Store, payload_session
from agent_web.search_text import urls_from_search_output
from agent_web.urls import extract_urls, validate_url

REDIRECT_REASON = (
    "{tool} is replaced by the agent-web plugin: Claude Code's {tool} hands you a summary written "
    "by another model, not the source. Use Bash instead: `web fetch <url>` reads a page verbatim, "
    '`web search "<query>"` searches Google. See the `web` skill.'
)
ASK_REASON = (
    "agent-web: this URL wasn't typed by you or returned by a search in this session. Fetching "
    "it sends a request to that site, and the URL itself can carry data out. Approve only if you "
    "expect this fetch."
)


@dataclass(frozen=True)
class Allow:
    reason: str


@dataclass(frozen=True)
class Ask:
    reason: str


@dataclass(frozen=True)
class Deny:
    reason: str


@dataclass(frozen=True)
class NoOpinion:
    pass


Decision = Allow | Ask | Deny | NoOpinion


@dataclass(frozen=True)
class NotWeb:
    pass


@dataclass(frozen=True)
class Unsafe:
    reason: str


@dataclass(frozen=True)
class WebSearch:
    query: str


@dataclass(frozen=True)
class WebFetch:
    url: str


@dataclass(frozen=True)
class WebLocal:
    """`web doctor`, `web config`, `--help`, `--version`: nothing leaves the machine."""


WebCommand = NotWeb | Unsafe | WebSearch | WebFetch | WebLocal

_PUNCTUATION = set("();<>|&")


def parse_web_command(command: str) -> WebCommand:
    """Classify a Bash command string. Only a single plain `web …` invocation is accepted."""
    if "\n" in command.strip() or "`" in command or "$(" in command or "${" in command:
        return Unsafe("command substitution or multiple lines")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as e:
        return Unsafe(f"unparseable ({e})")
    if not tokens:
        return NotWeb()
    if tokens[0] != "web":
        # The plugin only runs this hook when Claude Code saw a `web` subcommand; a `web` buried
        # in a compound command (`cd x && web fetch …`) is reviewed rather than auto-approved.
        return Unsafe("`web` inside a compound command") if "web" in tokens else NotWeb()
    if any(token and set(token) <= _PUNCTUATION for token in tokens):
        return Unsafe("shell operators (pipes, redirects, chaining)")
    if any("$" in token for token in tokens):
        return Unsafe("shell variables")
    args = tokens[1:]
    if not args or any(a in ("-h", "--help", "--version") for a in args):
        return WebLocal()
    try:
        parsed = build_parser().parse_args(args)
    except (argparse.ArgumentError, SystemExit) as e:
        return Unsafe(f"unrecognized arguments ({e})")
    match parsed.command:
        case "search":
            return WebSearch(str(parsed.query))
        case "fetch":
            return WebFetch(str(parsed.url))
        case "doctor" | "config":
            return WebLocal()
        case _:
            return Unsafe("unknown subcommand")


def decide_bash(command: str, session_id: str, store: Store) -> Decision:
    parsed = parse_web_command(command)
    match parsed:
        case NotWeb():
            return NoOpinion()
        case Unsafe(reason):
            return Ask(f"agent-web: not a simple `web` command ({reason}); review it.")
        case WebSearch():
            return Allow("agent-web: search query goes to Google")
        case WebLocal():
            return Allow("agent-web: local command")
        case WebFetch(url):
            if not isinstance(validate_url(url), str):
                return Ask(f"agent-web: {url!r} is not a valid http(s) URL; review it.")
            source = store.source_of(session_id, url)
            if source == "user":
                return Allow("agent-web: URL provided by the user")
            if source == "search":
                return Allow("agent-web: URL returned by a search")
            return Ask(ASK_REASON)
        case _:
            assert_never(parsed)


def _decision_output(decision: Decision) -> dict[str, object] | None:
    match decision:
        case Allow(reason):
            value = "allow"
        case Ask(reason):
            value = "ask"
        case Deny(reason):
            value = "deny"
        case NoOpinion():
            return None
        case _:
            assert_never(decision)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": value,
            "permissionDecisionReason": reason,
        }
    }


def pre_tool_use(payload: dict[str, object], store: Store) -> dict[str, object] | None:
    tool = get_str(payload, "tool_name")
    if tool in ("WebFetch", "WebSearch"):
        return _decision_output(Deny(REDIRECT_REASON.format(tool=tool)))
    if tool != "Bash":
        return None
    tool_input = as_dict(payload.get("tool_input")) or {}
    command = get_str(tool_input, "command")
    try:
        decision = decide_bash(command, payload_session(payload), store)
    except Exception as e:  # never let a hook bug auto-approve: fall back to asking
        decision = Ask(f"agent-web: provenance check failed ({type(e).__name__}); review it.")
    return _decision_output(decision)


def user_prompt_submit(payload: dict[str, object], store: Store) -> None:
    urls = extract_urls(get_str(payload, "prompt"))
    store.add(payload_session(payload), urls, "user")


def post_tool_use(payload: dict[str, object], store: Store) -> None:
    if get_str(payload, "tool_name") != "Bash":
        return
    tool_input = as_dict(payload.get("tool_input")) or {}
    if not isinstance(parse_web_command(get_str(tool_input, "command")), WebSearch):
        return
    response = as_dict(payload.get("tool_response")) or {}
    urls = urls_from_search_output(get_str(response, "stdout"))
    store.add(payload_session(payload), urls, "search")


def run(event: str, raw: str, store: Store | None = None) -> str:
    """Handle one hook event; returns what to print on stdout (may be empty)."""
    try:
        payload = as_dict(json.loads(raw)) or {}
    except ValueError:
        payload = {}
    store = store or Store()
    match event:
        case "pre-tool-use":
            output = pre_tool_use(payload, store)
            return json.dumps(output) if output else ""
        case "user-prompt-submit":
            user_prompt_submit(payload, store)
            return ""
        case "post-tool-use":
            post_tool_use(payload, store)
            return ""
        case _:
            return ""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m agent_web.hooks <pre-tool-use|user-prompt-submit|post-tool-use>")
        return 2
    try:
        output = run(args[0], sys.stdin.read())
    except Exception:  # recording hooks must never break the session
        if args[0] == "pre-tool-use":
            output = json.dumps(_decision_output(Ask("agent-web: hook error; review it.")))
        else:
            output = ""
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
