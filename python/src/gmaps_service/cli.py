"""Command line entry point for a one-off search or the HTTP service."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from gmaps_service.config import Config
from gmaps_service.errors import CaptchaDetectedError, ScraperError
from gmaps_service.places_api import compose_query
from gmaps_service.scraper import GoogleMapsScraper


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gmaps-scraper",
        description="Find business records from Google Maps or the Places API.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="Run one search and write JSON and CSV")
    search.add_argument("query", help='Business query, for example "coffee shops"')
    search.add_argument("location", nargs="?", default="", help='Place, for example "New York"')
    search.add_argument("--max-results", type=int, default=None)
    search.add_argument("--backend", choices=["auto", "playwright", "places"], default=None)
    search.add_argument("-o", "--output", default=None, help="Output path without requiring an extension")
    search.add_argument("--headless", action=argparse.BooleanOptionalAction, default=None)
    search.add_argument("--delay-min", type=float, default=None)
    search.add_argument("--delay-max", type=float, default=None)
    search.add_argument("--no-emails", action="store_true")
    search.add_argument("--config", default=None, help="JSON config file")

    serve = sub.add_parser("serve", help="Run the HTTP API")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--config", default=None, help="JSON config file")
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    overrides: dict[str, object] = {}
    if getattr(args, "backend", None):
        overrides["backend"] = args.backend
    if getattr(args, "max_results", None):
        overrides["max_results"] = args.max_results
    if getattr(args, "headless", None) is not None:
        overrides["headless"] = args.headless
    if getattr(args, "delay_min", None) is not None:
        overrides["delay_min"] = args.delay_min
    if getattr(args, "delay_max", None) is not None:
        overrides["delay_max"] = args.delay_max
    if getattr(args, "no_emails", False):
        overrides["extract_emails"] = False
    if getattr(args, "host", None):
        overrides["host"] = args.host
    if getattr(args, "port", None):
        overrides["port"] = args.port
    return Config.load(getattr(args, "config", None), **overrides)


def default_output(config: Config, query: str, location: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", compose_query(query, location).lower()).strip("-")[:80] or "results"
    return str(Path(config.output_dir) / slug)


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = config_from_args(args)
    except ScraperError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.command == "serve":
        return _serve(config)
    return _search(config, args)


def _search(config: Config, args: argparse.Namespace) -> int:
    output = args.output or default_output(config, args.query, args.location or "")
    scraper = GoogleMapsScraper(config)
    try:
        result = asyncio.run(
            scraper.run(
                args.query,
                args.location or "",
                max_results=args.max_results,
                backend=args.backend,
                filename=output,
            )
        )
    except CaptchaDetectedError as exc:
        print(exc, file=sys.stderr)
        return 2
    except ScraperError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"saved {result.total_found} businesses from {result.backend} to {output}.json and {output}.csv")
    return 0


def _serve(config: Config) -> int:
    import uvicorn

    from gmaps_service.api import create_app

    uvicorn.run(create_app(config), host=config.host, port=config.port)
    return 0


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


if __name__ == "__main__":
    raise SystemExit(main())
