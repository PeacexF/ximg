"""Config models (TOML + CLI overrides), validated with pydantic."""

import json
import re
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ximg.sniff import FORMATS

__all__ = ["Config", "ConfigError", "load_config", "parse_size"]


class ConfigError(Exception):
    pass


_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*$", re.I)
_MULT = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def parse_size(value: Any) -> int:
    """`2048`, `"2048"`, `"2KB"`, `"50MB"`, `"5GiB"` -> bytes (1024-based)."""
    if isinstance(value, bool):
        raise ValueError(f"invalid size: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and (m := _SIZE.match(value)):
        return int(float(m.group(1)) * _MULT[m.group(2).lower()])
    raise ValueError(f"invalid size: {value!r} (use e.g. 2048, '2KB', '50MB', '5GB')")


Size = Annotated[int, BeforeValidator(parse_size), Field(ge=0)]


def _norm_host(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    if not host or "/" in host or "://" in host:
        raise ValueError(f"expected a bare host name like 'www.example.com', got {host!r}")
    host = host.split(":")[0] if host.count(":") == 1 else host  # drop a port; keep IPv6 as-is
    if not host.isascii():
        host = host.encode("idna").decode("ascii")
    return host


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Engagement(_Model):
    id: str = ""
    notes: str = ""
    contact: str = ""


class PageScope(_Model):
    hosts: list[str] = []
    subdomains: bool = False
    include: list[str] = ["/*"]
    exclude: list[str] = []

    @field_validator("hosts")
    @classmethod
    def _norm_hosts(cls, v: list[str]) -> list[str]:
        return [_norm_host(h) for h in v]


class ImageScope(_Model):
    hosts: list[str] = []
    subdomains: bool = False
    also_page_hosts: bool = True

    @field_validator("hosts")
    @classmethod
    def _norm_hosts(cls, v: list[str]) -> list[str]:
        return [_norm_host(h) for h in v]


class ScopeConfig(_Model):
    pages: PageScope = Field(default_factory=PageScope)
    images: ImageScope = Field(default_factory=ImageScope)


class Limits(_Model):
    max_pages: int = Field(5000, ge=0)
    max_depth: int = Field(10, ge=0)
    max_pages_per_pattern: int = Field(200, ge=1)
    max_file_bytes: Size = 50 * 1024**2
    max_total_bytes: Size = 5 * 1024**3
    max_page_bytes: Size = 10 * 1024**2


class Filters(_Model):
    min_file_bytes: Size = 2048
    formats: list[str] = sorted(FORMATS)
    srcset: Literal["largest", "all"] = "largest"
    data_uris: bool = True

    @field_validator("formats")
    @classmethod
    def _known_formats(cls, v: list[str]) -> list[str]:
        v = [f.lower().replace("jpg", "jpeg") for f in v]
        if unknown := set(v) - FORMATS:
            raise ValueError(f"unknown formats {sorted(unknown)}; known: {sorted(FORMATS)}")
        return v


class Concurrency(_Model):
    global_: int = Field(8, alias="global", ge=1)
    per_host: int = Field(2, ge=1)


class Rate(_Model):
    per_host: float = Field(1.0, gt=0)
    jitter: float = Field(0.3, ge=0, lt=1)


class Http(_Model):
    timeout: float = Field(30, gt=0)
    retries: int = Field(3, ge=0)
    user_agent: str = ""
    proxy: str = ""
    strip_page_params: list[str] = ["utm_*", "fbclid", "gclid", "mc_cid", "mc_eid"]
    max_bad_streak: int = Field(15, ge=1)


class Render(_Model):
    mode: Literal["never", "auto", "always"] = "auto"
    pages: int = Field(2, ge=1)
    max_scrolls: int = Field(20, ge=0)
    idle_timeout: float = Field(10, gt=0)
    patterns: list[str] = []
    scan_json: bool = False


class Auth(_Model):
    cookies_file: Path | None = None
    headers_env: str = "XIMG_HEADERS"


class Discovery(_Model):
    lazy_attrs: list[str] = [
        "data-src", "data-srcset", "data-lazy-src", "data-lazy-srcset", "data-original", "data-lazy",
        "data-bg", "data-background", "data-background-image", "data-image", "data-full",
        "data-zoom-image", "data-hi-res", "data-large_image", "data-flickity-lazyload",
    ]  # fmt: skip
    sitemaps: bool = True
    css: bool = True
    jsonld: bool = True
    manifest: bool = True


class UpgradeRule(_Model):
    name: str
    match: str
    replace: str

    @field_validator("match")
    @classmethod
    def _valid_regex(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}") from e
        return v


class Wayback(_Model):
    mode: Literal["live", "archive"] = "live"
    limit: int = Field(10000, ge=1)
    rate: float = Field(0.5, gt=0)


class Config(_Model):
    seeds: list[str] = []
    out: Path | None = None
    engagement: Engagement = Field(default_factory=Engagement)
    scope: ScopeConfig = Field(default_factory=ScopeConfig)
    limits: Limits = Field(default_factory=Limits)
    filters: Filters = Field(default_factory=Filters)
    concurrency: Concurrency = Field(default_factory=Concurrency)
    rate: Rate = Field(default_factory=Rate)
    http: Http = Field(default_factory=Http)
    robots: Literal["respect", "ignore"] = "respect"
    render: Render = Field(default_factory=Render)
    auth: Auth = Field(default_factory=Auth)
    discovery: Discovery = Field(default_factory=Discovery)
    upgrade: list[str] = []  # built-in rule names (see ximg.upgrade.BUILTIN) or ["all"]
    upgrade_rules: list[UpgradeRule] = []
    wayback: Wayback = Field(default_factory=Wayback)

    @field_validator("upgrade")
    @classmethod
    def _known_upgrades(cls, v: list[str]) -> list[str]:
        from ximg.upgrade import BUILTIN

        if v == ["all"]:
            return sorted(BUILTIN)
        if unknown := set(v) - set(BUILTIN):
            raise ValueError(f"unknown built-in upgrade rules {sorted(unknown)}; known: {sorted(BUILTIN)} or 'all'")
        return v

    @model_validator(mode="after")
    def _check(self) -> Config:
        for seed in self.seeds:
            parts = urlsplit(seed)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"seed must be an absolute http(s) URL: {seed!r}")
        if not self.scope.pages.hosts:
            # Convenience: no explicit scope -> exactly the seed hosts.
            self.scope.pages.hosts = sorted({_norm_host(urlsplit(s).hostname or "") for s in self.seeds})
        pages = self.scope.pages
        for seed in self.seeds:
            host = _norm_host(urlsplit(seed).hostname or "")
            if not (host in pages.hosts or (pages.subdomains and any(host.endswith("." + h) for h in pages.hosts))):
                raise ValueError(f"seed {seed!r} is outside scope.pages.hosts {pages.hosts}")
        return self

    def require_runnable(self) -> None:
        if self.out is None:
            raise ConfigError("an output directory is required (--out or `out = ...` in the config)")
        if not self.seeds and not self.scope.pages.hosts:
            raise ConfigError("no seeds and no scope: give --seed URL or `seeds = [...]`")

    def snapshot(self) -> dict[str, Any]:
        # Auth holds only a cookie-file path and an env-var name, never the secrets themselves.
        return self.model_dump(mode="json", by_alias=True)

    def scope_key(self) -> str:
        """What must not change when resuming an output dir (seeds + scope)."""
        return json.dumps({"seeds": sorted(self.seeds), "scope": self.scope.model_dump(mode="json")}, sort_keys=True)


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _dotted(overrides: dict[str, Any]) -> dict[str, Any]:
    """{'limits.max_pages': 5} -> {'limits': {'max_pages': 5}}; None values are dropped."""
    tree: dict[str, Any] = {}
    for key, value in overrides.items():
        if value is None:
            continue
        node = tree
        *parents, leaf = key.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = value
    return tree


def load_config(path: Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    """Load TOML (if given), apply dotted-key CLI overrides, validate.

    A relative `out` in the TOML is resolved against the config file's directory;
    a relative `--out` from the CLI against the current directory.
    """
    raw: dict[str, Any] = {}
    if path is not None:
        try:
            raw = tomllib.loads(path.read_text())
        except FileNotFoundError as e:
            raise ConfigError(f"config file not found: {path}") from e
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from e
        if isinstance(raw.get("out"), str) and not Path(raw["out"]).expanduser().is_absolute():
            raw["out"] = str((path.parent / raw["out"]).resolve())
    over = _dotted(overrides or {})
    if isinstance(over.get("out"), (str, Path)):
        over["out"] = str(Path(over["out"]).expanduser().resolve())
    if isinstance(raw.get("out"), str):
        raw["out"] = str(Path(raw["out"]).expanduser())
    try:
        return Config.model_validate(_deep_merge(raw, over))
    except ValidationError as e:
        lines = [f"  {'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}" for err in e.errors()]
        raise ConfigError("invalid config:\n" + "\n".join(lines)) from e
