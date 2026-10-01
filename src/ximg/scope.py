"""Scope policy: which URLs may be fetched as pages, as images, or as page assets."""

from fnmatch import fnmatchcase

from ximg.config import ScopeConfig
from ximg.urls import host_of, path_and_query


def _host_in(host: str, hosts: frozenset[str], subdomains: bool) -> bool:
    if host in hosts:
        return True
    return subdomains and any(host.endswith("." + h) for h in hosts)


class ScopePolicy:
    """Each check returns None when allowed, or a short denial reason."""

    def __init__(self, cfg: ScopeConfig) -> None:
        self._cfg = cfg
        self._page_hosts = frozenset(cfg.pages.hosts)
        self._image_hosts = frozenset(cfg.images.hosts)

    def page_host(self, host: str) -> bool:
        return _host_in(host, self._page_hosts, self._cfg.pages.subdomains)

    def image_host(self, host: str) -> bool:
        if _host_in(host, self._image_hosts, self._cfg.images.subdomains):
            return True
        return self._cfg.images.also_page_hosts and self.page_host(host)

    def any_host(self, host: str) -> bool:
        return self.page_host(host) or self.image_host(host)

    def page(self, url: str) -> str | None:
        """HTML pages (and sitemaps): page hosts only, subject to include/exclude globs."""
        if not self.page_host(host_of(url)):
            return "scope"
        pq = path_and_query(url)
        if self._cfg.pages.include and not any(fnmatchcase(pq, g) for g in self._cfg.pages.include):
            return "scope:include"
        if any(fnmatchcase(pq, g) for g in self._cfg.pages.exclude):
            return "exclude"
        return None

    def image(self, url: str) -> str | None:
        return None if self.image_host(host_of(url)) else "scope"

    def asset(self, url: str) -> str | None:
        """Stylesheets, web manifests: anything the site serves from a page or image host."""
        return None if self.any_host(host_of(url)) else "scope"
