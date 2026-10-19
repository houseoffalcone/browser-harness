import base64
import io

import pytest
from PIL import Image

from browser_harness import daemon


def make_png(width, height):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def fake_png():
    return make_png


@pytest.fixture(autouse=True)
def _no_dedicated_chrome_probe(monkeypatch):
    """Keep the suite hermetic on a machine that runs a Chrome on 9223."""
    monkeypatch.setenv("BH_PREFER_CDP_URL", "")


@pytest.fixture(autouse=True)
def _owned_registry_in_tmp(tmp_path, monkeypatch):
    """Never read or write the machine's real owned-tab registry from a test."""
    monkeypatch.setattr(daemon, "OWNED_PATH", tmp_path / "owned.json")
