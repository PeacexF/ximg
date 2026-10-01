# Configuration

Settings come from a TOML file (`-c ximg.toml`) and/or CLI flags. **CLI flags override the file.** A relative `out` in the file is resolved against the file's directory; a relative `--out` against the current directory. Unknown keys are rejected, which catches typos.

When you resume (`ximg crawl --out DIR` with no `-c`), ximg reuses the config the run started with, which is stored in `DIR/.ximg/ximg.db`. Flags you pass still override it. Changing **seeds or scope** on an unfinished run is refused unless you pass `--force-config`. Other settings (limits, rate, filters) can change freely between resumes.

## CLI flags (`ximg crawl`)

| Flag | Config key | Notes |
|---|---|---|
| `-c, --config FILE` | — | TOML config |
| `--seed URL` (repeatable) | `seeds` | Start URLs |
| `-o, --out DIR` | `out` | **Required.** Output directory |
| `--page-host HOST` (repeatable) | `scope.pages.hosts` | Defaults to the seed hosts |
| `--image-host HOST` (repeatable) | `scope.images.hosts` | Extra hosts images may be downloaded from |
| `--max-pages N` | `limits.max_pages` | |
| `--max-depth N` | `limits.max_depth` | |
| `--max-total-bytes SIZE` | `limits.max_total_bytes` | e.g. `5GB` |
| `--min-file-bytes SIZE` | `filters.min_file_bytes` | `0` keeps everything |
| `--render never\|auto\|always` | `render.mode` | |
| `--rate R` | `rate.per_host` | requests/second per host |
| `--robots respect\|ignore` | `robots` | |
| `--images-only FILE` | — | Don't crawl; download the image URLs listed in FILE (one per line, scope still applies) |
| `--dry-run` | — | Crawl pages and record image URLs without downloading. A later normal run downloads them |
| `--overwrite` | — | Wipe a *finished* run in `--out` and start over |
| `--force-config` | — | Resume although seeds/scope changed |
| `-v` / `-q` | — | More / less console output |

Sizes accept `2048`, `"2KB"`, `"50MB"`, `"5GB"` (1024-based).

## Full reference

```toml
# Top-level keys must come before the first [table] (TOML rule).
seeds   = ["https://www.example.com/"]
out     = "/data/recon/example"
robots  = "respect"             # respect | ignore
upgrade = []                    # built-in "original resolution" rules, or ["all"]:
                                # wordpress-size-suffix, strip-size-params, cloudinary, shopify-size-suffix

[engagement]
id      = ""            # authorization reference, stored with the run
notes   = ""
contact = ""            # appended to the User-Agent: ximg/<ver> (+contact)

[scope.pages]           # hosts whose HTML pages are crawled
hosts      = []         # default: the seed hosts
subdomains = false      # true: *.host too
include    = ["/*"]     # globs on path+query; '*' also matches '/'
exclude    = []         # e.g. ["/logout*", "/cart/*", "/search*"]

[scope.images]          # hosts images may be downloaded from
hosts           = []
subdomains      = false
also_page_hosts = true  # page hosts are image hosts too

[limits]
max_pages             = 5000    # HTML pages fetched (assets/sitemaps don't count)
max_depth             = 10      # link depth from the seeds
max_pages_per_pattern = 200     # crawler-trap guard (see below)
max_file_bytes        = "50MB"  # larger images are skipped (filter_size_max)
max_total_bytes       = "5GB"   # stop cleanly when reached; raise and re-run to continue
max_page_bytes        = "10MB"  # HTML/CSS/sitemap body cap

[filters]
min_file_bytes = 2048           # smaller images are skipped (filter_size)
formats        = ["avif", "bmp", "gif", "heic", "ico", "jpeg", "jxl", "png", "svg", "tiff", "webp"]
srcset         = "largest"      # largest | all
data_uris      = true           # decode and save data:image/... URIs

[concurrency]
global   = 8                    # total parallel requests
per_host = 2

[rate]
per_host = 1.0                  # requests/second per host
jitter   = 0.3                  # ±30% on the spacing

[http]
timeout           = 30          # seconds
retries           = 3           # on connection errors, 429, 500, 502, 503, 504 (honours Retry-After, max 5 min)
user_agent        = ""          # default: ximg/<version> (+contact)
proxy             = ""          # single fixed egress, e.g. "socks5://127.0.0.1:1080"
strip_page_params = ["utm_*", "fbclid", "gclid", "mc_cid", "mc_eid"]
max_bad_streak    = 15          # consecutive 403/429/503 from a host -> stop using that host

[render]                        # needs the `render` extra + Chromium
mode         = "auto"           # never | auto | always
pages        = 2                # concurrent browser tabs
max_scrolls  = 20               # viewport scrolls per page (lazy load / infinite scroll)
idle_timeout = 10               # seconds to wait for network idle
patterns     = []               # path globs that always render
scan_json    = false            # also take image URLs from XHR/fetch JSON responses

[auth]                          # for authorized authenticated crawling
cookies_file = ""               # Netscape cookies.txt or JSON list of {name, value, domain, path}
headers_env  = "XIMG_HEADERS"   # env var holding a JSON object of extra headers

[discovery]
lazy_attrs = ["data-src", "data-srcset", "data-lazy-src", "data-lazy-srcset", "data-original", "data-lazy",
              "data-bg", "data-background", "data-background-image", "data-image", "data-full",
              "data-zoom-image", "data-hi-res", "data-large_image", "data-flickity-lazyload"]
sitemaps = true                 # robots.txt Sitemap: lines, else /sitemap.xml
css      = true                 # fetch stylesheets for url()/image-set()
jsonld   = true
manifest = true                 # web app manifest + browserconfig.xml

[[upgrade_rules]]               # custom rules (regex -> replacement); repeatable
name    = "thumbs-to-full"
match   = '^(.*)/thumbs/(.*)$'
replace = '\1/full/\2'

[wayback]                       # used by `ximg wayback`
mode  = "live"                  # live: try historical URLs on the live site | archive: download archived copies
limit = 10000                   # max CDX rows
rate  = 0.5                     # req/s to web.archive.org in archive mode
```

## Notes

**Scope.** Pages are only crawled on `scope.pages` hosts. Images are only downloaded from `scope.images` hosts (plus page hosts, unless `also_page_hosts = false`). Every redirect hop is checked. Out-of-scope image URLs are still listed in `urls.csv` with `skip_reason = scope`, so you can see which external hosts serve the site's images and widen scope deliberately. During rendering, the browser can't reach out-of-scope hosts either; those requests are aborted.

**Crawler traps.** URLs are collapsed into patterns (digits → `N`, long IDs → `X`, query → its sorted keys). Once `max_pages_per_pattern` pages of one pattern are queued, more are recorded as `skipped: pattern_limit`.

**Rendering in `auto` mode** kicks in for pages with an SPA mount node (`#root`, `#app`, `#__next`, …) and little text, pages whose `<noscript>` asks for JavaScript and that have almost no static images, and paths matching `render.patterns`. The browser never downloads images itself: it gets a 1×1 placeholder, and ximg downloads each image once, rate-limited.

**Skip reasons** in `urls.csv`: `scope`, `scope:redirect`, `robots`, `filter_size` (< min), `filter_size_max` (> max), `filter_format`, `not_image` (e.g. an HTML error page served as `.jpg`). Failures carry an `error` (HTTP status or network error).
