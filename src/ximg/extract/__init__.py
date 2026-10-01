"""Pure extractors: (base URL, body) -> image references and links. No I/O."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Ref:
    """An image reference. `url` is absolute + normalized, or a `data:` URI."""

    url: str
    kind: str
    context: str = ""
