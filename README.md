<p align="center">
  <img src=".github/img/logo.png" alt="ximg logo" width="160">
</p>

# ximg

<p align="center">
  <a href="https://github.com/PeacexF/ximg/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/PeacexF/ximg/ci.yml?branch=main&style=flat-square&label=ci&labelColor=0f1e29" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.14-0f1e29?style=flat-square&labelColor=0f1e29" alt="Python 3.14">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-0f1e29?style=flat-square&labelColor=0f1e29" alt="License: MIT"></a>
</p>

Crawl a website and save every image it exposes, byte for byte, into a directory your other tools can read.

ximg only collects images. It doesn't analyze them: no EXIF and no metadata extraction. It finds images in places a naive crawler misses:

- `<img src>` and `srcset` (largest candidate), plus `<picture><source>`
- lazy-load attributes (`data-src`, `data-srcset`, `data-bg`, …)
- CSS backgrounds: inline styles, `<style>` blocks, external stylesheets, `@import`, `image-set()`
- `og:image` / `twitter:image`, icons, `preload` links, JSON-LD, web manifests, `browserconfig.xml`
- image sitemaps, links to full-size images, `data:` URIs, SVG `<image>`, video posters
- with `render`: images that only exist after JavaScript runs, including lazy-loaded and infinite-scroll images
- optionally, historical image URLs from the Wayback Machine

Each unique image is saved once, named by its SHA-256. A manifest records every URL it came from and every page that referenced it.

> **Only run ximg against sites you're authorized to collect from.** It stays in a strict host allowlist, respects robots.txt by default, rate-limits per host, identifies itself honestly, and never tries to bypass blocking.

## Install

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install .                # puts `ximg` on your PATH (static crawling)
```

For JavaScript-rendered sites, run from the checkout with the `render` extra:

```sh
uv sync --extra render && uv run playwright install chromium
uv run ximg crawl …
```

## Quick start

```sh
# One-off: crawl a site, images from the site's own hosts only
ximg crawl --seed https://www.example.com/ --out /data/recon/example

# Allow a CDN host for images, go faster/slower, skip nothing
ximg crawl --seed https://www.example.com/ --out /data/recon/example \
           --image-host cdn.example.com --rate 2 --min-file-bytes 0

# Or with a config file (recommended for engagements)
ximg init /data/recon/example --seed https://www.example.com/   # writes ximg.toml
ximg crawl -c ximg.toml
```

Interrupted (Ctrl-C, crash, limit reached)? Run the same command again, or just `ximg crawl --out DIR`, and it resumes where it stopped.

## Output

```
<out>/
  images/
    www.example.com/3f9a…e1.jpg     # <sha256>.<real extension>, one folder per serving host
    cdn.example.com/b1d2…9f.webp
    _data-uri/…                      # images embedded as data: URIs
  manifest.csv     # one row per (file, image URL, referring page, how it was referenced)
  manifest.json    # the same, grouped by file
  urls.csv         # every image URL seen, including skipped/failed ones and why
  .ximg/           # crawl state (SQLite) and JSON-lines logs
```

- Files are the exact bytes the server sent. Nothing is re-encoded or stripped.
- The extension comes from the file's magic bytes, not the URL.
- Images smaller than 2 KB (spacers, tracking pixels) are skipped by default and still listed in `urls.csv`.
- `ximg export DIR --layout friendly` adds `friendly/<host>/<url path>/<filename>` hardlinks that mirror the site, using no extra disk.

## Commands

| Command | What it does |
|---|---|
| `ximg init OUT` | Write a starter `ximg.toml` |
| `ximg crawl` | Crawl and download (resumes automatically) |
| `ximg status OUT` | Counts, top hosts, recent errors |
| `ximg export OUT [--layout friendly]` | Rewrite the manifests (and optionally the friendly tree) |
| `ximg retry OUT` | Requeue failed URLs and resume |
| `ximg wayback OUT` | Queue historical image URLs from the Wayback Machine, then run `ximg crawl --out OUT` |
| `ximg purge OUT` | Delete everything ximg wrote in OUT |

Exit codes: `0` finished, `1` usage/config error, `2` finished with more than 20% failures, `130` interrupted (resumable).

## Docs

- [docs/configuration.md](docs/configuration.md): every config key and CLI flag
- [docs/architecture.md](docs/architecture.md): how it works, scope and safety model, storage schema

## Development

```sh
uv sync --extra render && uv run playwright install chromium
uv run pytest                 # fast suite (in-process fixture site, no network)
uv run pytest -m render       # real Chromium against a localhost server
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy
```

## License

MIT
