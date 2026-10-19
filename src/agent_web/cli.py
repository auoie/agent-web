"""The `web` command."""

from __future__ import annotations

import argparse
import sys
from typing import TextIO

from agent_web import __version__
from agent_web.cli_args import build_parser
from agent_web.config import EXAMPLE_CONFIG, Config, config_path, load_config
from agent_web.failures import EXIT_OK, EXIT_USAGE, Failure, InvalidConfig, describe, exit_code

MAX_SEARCH_RESULTS = 50


def _fail(failure: Failure, err: TextIO) -> int:
    print(f"web: {describe(failure)}", file=err)
    return exit_code(failure)


def _search(args: argparse.Namespace, config: Config, out: TextIO, err: TextIO) -> int:
    from agent_web.output import render_search, search_json
    from agent_web.search.google import SearchResults, search

    n = max(1, min(int(args.n), MAX_SEARCH_RESULTS))
    outcome = search(str(args.query), n, config)
    if not isinstance(outcome, SearchResults):
        return _fail(outcome, err)
    print(search_json(outcome) if args.json else render_search(outcome), file=out)
    return EXIT_OK


def _fetch(args: argparse.Namespace, config: Config, out: TextIO, err: TextIO) -> int:
    from agent_web.fetch import fetch
    from agent_web.model import FetchResult
    from agent_web.output import exit_code_for, fetch_json, render_fetch

    max_chars = config.max_chars if args.max_chars is None else int(args.max_chars)
    if max_chars < 1 or int(args.offset) < 0:
        print("web: --max-chars must be positive and --offset non-negative", file=err)
        return EXIT_USAGE
    outcome = fetch(
        str(args.url), config, refresh=bool(args.refresh), whole_page=bool(args.whole_page)
    )
    if not isinstance(outcome, FetchResult):
        return _fail(outcome, err)
    find = str(args.find) if args.find else None
    if args.json:
        print(fetch_json(outcome, offset=args.offset, max_chars=max_chars, find=find), file=out)
    else:
        print(render_fetch(outcome, offset=args.offset, max_chars=max_chars, find=find), file=out)
    return exit_code_for(outcome)


def _doctor(config: Config, out: TextIO) -> int:
    from agent_web.convert.pandoc import find_pandoc
    from agent_web.cookies import load_browser_cookies
    from agent_web.failures import CookieReadFailure, ToolMissing
    from agent_web.search.google import signed_in

    problems = 0
    path = config_path()
    print(f"agent-web {__version__}", file=out)
    print(
        f"config:   {path} ({'found' if path.exists() else 'not found, using defaults'})", file=out
    )
    pandoc = find_pandoc(config.pandoc)
    if isinstance(pandoc, ToolMissing):
        problems += 1
        print(f"pandoc:   MISSING: {pandoc.hint}", file=out)
    else:
        print(f"pandoc:   {pandoc.path} ({'.'.join(map(str, pandoc.version))})", file=out)
    cookies = load_browser_cookies(config.browser, "google.com", config.cookie_file)
    if isinstance(cookies, CookieReadFailure):
        problems += 1
        print(f"google:   can't read {config.browser} cookies: {cookies.reason}", file=out)
    elif signed_in(cookies):
        print(f"google:   signed in ({config.browser} cookies)", file=out)
    else:
        problems += 1
        print(
            f"google:   NOT signed in to Google in {config.browser}: searches will fail", file=out
        )
    domains = ", ".join(config.cookie_domains) or "(none: every fetch is anonymous)"
    print(f"cookies:  sent on fetch to: {domains}", file=out)
    print(f"cache:    {config.cache_dir}", file=out)
    return EXIT_USAGE if problems else EXIT_OK


def _config(args: argparse.Namespace, config: Config, out: TextIO) -> int:
    if args.example:
        print(EXAMPLE_CONFIG, end="", file=out)
        return EXIT_OK
    print(f"# {config_path()}", file=out)
    for key, value in config.__dict__.items():
        print(f"{key} = {value!r}", file=out)
    return EXIT_OK


def main(argv: list[str] | None = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentError as e:
        print(f"web: {e}", file=err)
        return EXIT_USAGE
    if args.version:
        print(f"agent-web {__version__}", file=out)
        return EXIT_OK
    if args.command is None:
        parser.print_help(out)
        return EXIT_USAGE
    config = load_config()
    if isinstance(config, InvalidConfig):
        return _fail(config, err)
    match args.command:
        case "search":
            return _search(args, config, out, err)
        case "fetch":
            return _fetch(args, config, out, err)
        case "doctor":
            return _doctor(config, out)
        case "config":
            return _config(args, config, out)
        case _:
            parser.print_help(err)
            return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
