"""The configs shown in docs/examples must load exactly as written."""

import re
from pathlib import Path

from ximg.config import load_config

ROOT = Path(__file__).parent.parent


def test_configuration_reference_is_valid(tmp_path: Path) -> None:
    text = (ROOT / "docs" / "configuration.md").read_text()
    block = re.search(r"```toml\n(.*?)```", text, re.S)
    assert block
    toml = tmp_path / "ref.toml"
    toml.write_text(block.group(1))
    cfg = load_config(toml)
    assert cfg.robots == "respect" and cfg.upgrade == [] and cfg.upgrade_rules[0].name == "thumbs-to-full"
    assert cfg.wayback.mode == "live" and cfg.http.max_bad_streak == 15


def test_example_config_is_valid() -> None:
    cfg = load_config(ROOT / "examples" / "ximg.example.toml")
    assert cfg.robots == "respect" and cfg.upgrade == ["wordpress-size-suffix"]
    assert cfg.scope.images.hosts == ["cdn.example.com"]
