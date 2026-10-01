"""`srcset` / `image-set()` candidate parsing and "largest" selection."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Candidate:
    url: str
    width: float = 0.0  # from "800w"
    density: float = 1.0  # from "2x" (default 1x)

    @property
    def rank(self) -> tuple[float, float]:
        return (self.width, self.density)


def _descriptor(cand: Candidate, desc: str) -> Candidate:
    width, density = cand.width, cand.density
    for token in desc.split():
        token = token.lower()
        try:
            if token.endswith("w"):
                width = float(token[:-1])
            elif token.endswith("x"):
                density = float(token[:-1])
            elif token.endswith("dppx"):
                density = float(token[:-4])
            elif token.endswith("dpi"):
                density = float(token[:-3]) / 96
        except ValueError:
            continue
    return Candidate(cand.url, width, density)


def parse_srcset(value: str) -> list[Candidate]:
    """Parse per the HTML spec's shape: URLs may contain commas (e.g. Cloudinary `w_300,c_fill`)."""
    out: list[Candidate] = []
    s, i, n = value, 0, len(value)
    while i < n:
        while i < n and (s[i].isspace() or s[i] == ","):
            i += 1
        if i >= n:
            break
        start = i
        while i < n and not s[i].isspace():
            i += 1
        url = s[start:i]
        desc = ""
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            start, depth = i, 0
            while i < n:
                c = s[i]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth = max(0, depth - 1)
                elif c == "," and depth == 0:
                    break
                i += 1
            desc = s[start:i].strip()
        if url:
            out.append(_descriptor(Candidate(url), desc))
    return out


def pick(candidates: list[Candidate], mode: str) -> list[str]:
    """`largest` -> the single best candidate; `all` -> every URL (deduped, in order)."""
    if not candidates:
        return []
    if mode == "all":
        return list(dict.fromkeys(c.url for c in candidates))
    return [max(candidates, key=lambda c: c.rank).url]
