"""Command-line interface."""

import asyncio
import json
import signal
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from ximg import __version__, logs
from ximg.config import Config, ConfigError, load_config
from ximg.crawler import Crawler, Renderer
from ximg.export import export_all, export_friendly
from ximg.store import OutputError, Store, is_output_dir, open_output, wipe_output

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Crawl a website and save every image it exposes.")
console = Console(stderr=True)

EXIT_OK, EXIT_USAGE, EXIT_FAILURES, EXIT_INTERRUPTED = 0, 1, 2, 130


def _fail(msg: str, code: int = EXIT_USAGE) -> typer.Exit:
    console.print(f"[red]error:[/red] {msg}")
    return typer.Exit(code)


def _version(value: bool) -> None:
    if value:
        print(f"ximg {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True)] = False,
) -> None:
    pass


STARTER = """\
# ximg config. Full reference: docs/configuration.md
# Top-level keys must come before the first [table].
seeds  = ["{seed}"]
out    = "{out}"           # downstream tools read from here
robots = "respect"         # respect | ignore

[engagement]
id      = ""            # authorization reference
notes   = ""
contact = ""            # goes into the User-Agent, e.g. "you@example.com"

[scope.pages]
hosts      = [{hosts}]
subdomains = false
exclude    = []          # path globs, e.g. ["/logout*", "/cart/*"]

[scope.images]
hosts           = []     # extra image hosts (CDNs) you're allowed to fetch from
also_page_hosts = true

[limits]
max_pages       = 5000
max_depth       = 10
max_file_bytes  = "50MB"
max_total_bytes = "5GB"

[filters]
min_file_bytes = 2048
srcset         = "largest"

[rate]
per_host = 1.0           # requests per second per host

[render]
mode = "auto"            # never | auto | always (needs: uv sync --extra render)
"""


@app.command()
def init(
    out: Annotated[Path, typer.Argument(help="Output directory the crawl will write to")],
    seed: Annotated[str, typer.Option(help="Start URL")] = "https://www.example.com/",
    config: Annotated[Path, typer.Option("--config", "-c", help="Where to write the config")] = Path("ximg.toml"),
) -> None:
    """Write a starter ximg.toml."""
    if config.exists():
        raise _fail(f"{config} already exists")
    from urllib.parse import urlsplit

    host = urlsplit(seed).hostname or "www.example.com"
    config.write_text(STARTER.format(seed=seed, out=out, hosts=f'"{host}"'))
    console.print(f"wrote {config}: review scope, then run [bold]ximg crawl -c {config}[/bold]")


def _stored_config(out: Path) -> Config:
    store = Store(out)
    try:
        raw = json.loads(store.meta("config_json") or "{}")
    finally:
        store.close()
    raw["out"] = str(out)
    return Config.model_validate(raw)


@app.command()
def crawl(
    config: Annotated[Path | None, typer.Option("--config", "-c", help="TOML config file")] = None,
    seed: Annotated[list[str] | None, typer.Option("--seed", help="Start URL (repeatable)")] = None,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Output directory")] = None,
    page_host: Annotated[list[str] | None, typer.Option("--page-host", help="Page scope host (repeatable)")] = None,
    image_host: Annotated[list[str] | None, typer.Option("--image-host", help="Extra image host (repeatable)")] = None,
    max_pages: Annotated[int | None, typer.Option()] = None,
    max_depth: Annotated[int | None, typer.Option()] = None,
    max_total_bytes: Annotated[str | None, typer.Option(help="e.g. 5GB")] = None,
    min_file_bytes: Annotated[str | None, typer.Option(help="e.g. 2KB, 0 to keep everything")] = None,
    render: Annotated[str | None, typer.Option(help="never | auto | always")] = None,
    rate: Annotated[float | None, typer.Option(help="Requests/second per host")] = None,
    robots: Annotated[str | None, typer.Option(help="respect | ignore")] = None,
    images_only: Annotated[
        Path | None, typer.Option(help="Skip crawling; download the image URLs listed in this file")
    ] = None,
    dry_run: Annotated[bool, typer.Option(help="Discover image URLs but don't download")] = False,
    overwrite: Annotated[bool, typer.Option(help="Wipe a finished run in --out and start over")] = False,
    force_config: Annotated[bool, typer.Option(help="Resume even though seeds/scope changed")] = False,
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 0,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
) -> None:
    """Crawl and download images. Re-run the same command to resume an interrupted run."""
    overrides: dict[str, Any] = {
        "seeds": seed or None,
        "out": out,
        "scope.pages.hosts": page_host or None,
        "scope.images.hosts": image_host or None,
        "limits.max_pages": max_pages,
        "limits.max_depth": max_depth,
        "limits.max_total_bytes": max_total_bytes,
        "filters.min_file_bytes": min_file_bytes,
        "render.mode": render,
        "rate.per_host": rate,
        "robots": robots,
    }
    try:
        if config is None and not seed and not images_only and out and is_output_dir(out.resolve()):
            # Resume with the config the run started with (CLI flags still override).
            base = _flatten(_stored_config(out.resolve()).model_dump(by_alias=True))
            cfg = load_config(None, {**base, **{k: v for k, v in overrides.items() if v is not None}})
        else:
            cfg = load_config(config, overrides)
        cfg.require_runnable()
    except ConfigError as e:
        raise _fail(str(e)) from None
    assert cfg.out is not None

    image_list: list[str] | None = None
    if images_only:
        image_list = [ln.strip() for ln in images_only.read_text().splitlines() if ln.strip() and ln[0] != "#"]

    try:
        store = open_output(cfg, overwrite=overwrite, force_config=force_config)
    except OutputError as e:
        raise _fail(str(e)) from None
    log_path = logs.setup(store.log_dir, -1 if quiet else verbose)

    renderer = _make_renderer(cfg) if image_list is None else None
    crawler = Crawler(cfg, store, renderer=renderer, dry_run=dry_run, image_list=image_list)
    if not quiet:
        console.print(
            f"ximg {__version__} → [bold]{cfg.out}[/bold]  pages:{','.join(cfg.scope.pages.hosts)}"
            f"  images:+{','.join(cfg.scope.images.hosts) or '-'}  robots:{cfg.robots}"
            f"  rate:{cfg.rate.per_host}/s/host  render:{cfg.render.mode if renderer else 'never'}"
        )
    status = asyncio.run(_run(crawler, renderer, quiet))
    export_all(store)
    counts = store.counts()
    store.close()
    if not quiet:
        _print_summary(status, counts, cfg.out, log_path)
    if status == "interrupted":
        raise typer.Exit(EXIT_INTERRUPTED)
    failed = counts["images"].get("failed", 0) + counts["pages"].get("failed", 0)
    total = sum(counts["images"].values()) + sum(counts["pages"].values())
    if total and failed / total > 0.2:
        raise typer.Exit(EXIT_FAILURES)


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            flat.update(_flatten(v, key + "."))
        else:
            flat[key] = v
    return flat


def _make_renderer(cfg: Config) -> Renderer | None:
    if cfg.render.mode == "never":
        return None
    try:
        from ximg.render import PlaywrightRenderer
    except ImportError:
        if cfg.render.mode == "always":
            raise _fail(
                "render = always needs Playwright: uv sync --extra render && uv run playwright install chromium"
            ) from None
        console.print("[yellow]note:[/yellow] Playwright not installed; JS rendering disabled (render=auto)")
        return None
    return PlaywrightRenderer(cfg)


async def _run(crawler: Crawler, renderer: Renderer | None, quiet: bool) -> str:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    presses = 0

    def on_sigint() -> None:
        nonlocal presses
        presses += 1
        if presses == 1:
            console.print("\n[yellow]stopping after in-flight requests… (Ctrl-C again to abort now)[/yellow]")
            crawler.request_stop()
            loop.call_later(10, task.cancel)
        else:
            task.cancel()

    loop.add_signal_handler(signal.SIGINT, on_sigint)
    live = None if quiet or not sys.stderr.isatty() else Live(console=console, refresh_per_second=4, transient=True)
    progress = asyncio.create_task(_progress(crawler, live)) if live else None
    try:
        if live:
            live.start()
        if renderer is not None and hasattr(renderer, "__aenter__"):
            async with renderer:  # type: ignore[attr-defined]
                return await crawler.run()
        return await crawler.run()
    except asyncio.CancelledError:
        return "interrupted"
    finally:
        if progress:
            progress.cancel()
        if live:
            live.stop()
        loop.remove_signal_handler(signal.SIGINT)


async def _progress(crawler: Crawler, live: Live) -> None:
    while True:
        s = crawler.stats
        qp, qi = crawler.store.queued_pages(), crawler.store.queued_images()
        line = Text.assemble(
            ("pages ", "dim"), f"{s.pages}", (f" ({s.pages_failed} failed)" if s.pages_failed else "", "red"),
            ("  assets ", "dim"), f"{s.assets}",
            ("  images ", "dim"), (f"{s.images_saved} saved", "green"),
            f", {s.images_dup} dup, {s.images_skipped} skipped",
            (f", {s.images_failed} failed" if s.images_failed else "", "red"),
            ("  ", ""), f"{s.bytes_saved / 1048576:.1f} MB",
            ("  queue ", "dim"), f"p:{qp} i:{qi}",
            (f"  rendered {s.rendered}" if s.rendered else "", "dim"),
        )  # fmt: skip
        live.update(line)
        await asyncio.sleep(0.5)


def _print_summary(status: str, counts: dict[str, Any], out: Path, log_path: Path | None) -> None:
    color = {"finished": "green", "limit": "yellow", "dry_run": "cyan"}.get(status, "red")
    console.print(f"\n[bold {color}]{status}[/bold {color}]")
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_row("pages", _fmt_states(counts["pages"]))
    t.add_row("assets", _fmt_states(counts["assets"]))
    t.add_row("image URLs", _fmt_states(counts["images"]))
    if counts["image_skips"]:
        t.add_row("  skipped", ", ".join(f"{k}: {v}" for k, v in sorted(counts["image_skips"].items())))
    t.add_row("files", f"{counts['files']} unique, {counts['bytes'] / 1048576:.1f} MB")
    t.add_row("output", f"{out}/images  (+ manifest.csv, manifest.json, urls.csv)")
    if log_path:
        t.add_row("log", str(log_path))
    console.print(t)
    if status == "limit":
        console.print("a limit was reached; raise it and re-run the same command to continue")
    elif status == "interrupted":
        console.print("interrupted; re-run the same command to resume")


def _fmt_states(states: dict[str, int]) -> str:
    return ", ".join(f"{k}: {v}" for k, v in sorted(states.items())) or "-"


def _open_existing(out: Path) -> Store:
    out = out.resolve()
    if not is_output_dir(out):
        raise _fail(f"{out} is not an ximg output directory")
    return Store(out)


@app.command()
def status(out: Annotated[Path, typer.Argument()]) -> None:
    """Show counts, top hosts and recent errors for an output directory."""
    store = _open_existing(out)
    counts = store.counts()
    _print_summary(store.meta("status") or "?", counts, out.resolve(), None)
    hosts = store.conn.execute(
        "SELECT substr(f.path, 8, instr(substr(f.path, 8), '/') - 1) AS host, COUNT(*), SUM(f.bytes)"
        " FROM file f GROUP BY host ORDER BY 2 DESC LIMIT 10"
    ).fetchall()
    if hosts:
        console.print("\n[bold]top image hosts[/bold]")
        for h, n, b in hosts:
            console.print(f"  {h}: {n} files, {b / 1048576:.1f} MB")
    errors = store.conn.execute(
        "SELECT url, error FROM image_url WHERE state='failed'"
        " UNION ALL SELECT url, error FROM page WHERE state='failed' LIMIT 10"
    ).fetchall()
    if errors:
        console.print("\n[bold]recent errors[/bold]")
        for url, err in errors:
            console.print(f"  {err}  {url}")
    store.close()


@app.command()
def export(
    out: Annotated[Path, typer.Argument()],
    layout: Annotated[str, typer.Option(help="'friendly' also builds a hardlink tree mirroring site paths")] = "",
) -> None:
    """(Re)write manifest.csv, manifest.json and urls.csv."""
    store = _open_existing(out)
    for p in export_all(store):
        console.print(f"wrote {p}")
    if layout == "friendly":
        n = export_friendly(store)
        console.print(f"linked {n} files under {store.out / 'friendly'}")
    elif layout:
        raise _fail(f"unknown layout {layout!r}")
    store.close()


@app.command()
def retry(
    out: Annotated[Path, typer.Argument()],
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 0,
) -> None:
    """Requeue failed pages and images, then resume the crawl with its original config."""
    store = _open_existing(out)
    pages, images = store.requeue_failed()
    if store.meta("status") == "finished":
        store.set_meta("status", "limit")
    store.close()
    console.print(f"requeued {pages} pages, {images} images")
    crawl(out=out, verbose=verbose)


@app.command()
def wayback(
    out: Annotated[Path, typer.Argument(help="Existing or new output directory")],
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    domain: Annotated[str | None, typer.Option(help="Domain to query (default: first page host)")] = None,
    mode: Annotated[str | None, typer.Option(help="live | archive")] = None,
    limit: Annotated[int | None, typer.Option(help="Max historical URLs to queue")] = None,
) -> None:
    """Queue historical image URLs from the Wayback Machine, then run `ximg crawl` to download them."""
    from ximg.wayback import queue_wayback

    out = out.resolve()
    try:
        over = {"out": out, "wayback.mode": mode, "wayback.limit": limit}
        if config is None and is_output_dir(out):
            cfg = load_config(None, {**_flatten(_stored_config(out).model_dump(by_alias=True)), **over})
        else:
            cfg = load_config(config, over)
        cfg.require_runnable()
        store = open_output(cfg, force_config=False)
    except (ConfigError, OutputError) as e:
        raise _fail(str(e)) from None
    logs.setup(store.log_dir, 1)
    target = domain or (cfg.scope.pages.hosts[0] if cfg.scope.pages.hosts else None)
    if not target:
        raise _fail("no domain: pass --domain or configure scope.pages.hosts")
    added, skipped = asyncio.run(queue_wayback(cfg, store, target))
    if store.meta("status") == "finished":
        store.set_meta("status", "limit")
    store.close()
    console.print(f"queued {added} historical image URLs ({skipped} out of scope / already known)")
    console.print(f"now run: [bold]ximg crawl --out {out}[/bold]")


@app.command()
def purge(
    out: Annotated[Path, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation")] = False,
) -> None:
    """Delete everything ximg wrote in an output directory."""
    out = out.resolve()
    if not is_output_dir(out):
        raise _fail(f"{out} is not an ximg output directory")
    if not yes and not typer.confirm(f"Delete images/, manifests and state in {out}?"):
        raise typer.Exit(EXIT_USAGE)
    wipe_output(out)
    if not any(out.iterdir()):
        out.rmdir()
    console.print(f"purged {out}")


def main() -> None:
    app()
