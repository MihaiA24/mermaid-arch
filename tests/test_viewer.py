from __future__ import annotations

import base64
import json
from html.parser import HTMLParser

import pytest

from mermaid_arch.models import Engine
from mermaid_arch.viewer import build_html

LIGHT = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="4 -49 100 150">'
    '<text x="5" y="0">Light</text></svg>'
)
DARK = LIGHT.replace("Light", "Dark")


class PayloadParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture = False
        self.payload = ""
        self.scripts = 0

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts += 1
            self.capture = dict(attrs).get("id") == "payload"

    def handle_endtag(self, tag):
        if tag == "script":
            self.capture = False

    def handle_data(self, data):
        if self.capture:
            self.payload += data


def test_source_and_title_cannot_escape_payload() -> None:
    source = '</script><!-- &amp; "É" \u2028 @@LIGHT@@'
    title = "<img src=x onerror=alert(1)> @@DATA@@"
    document = build_html(
        title=title, engine=Engine.MERMAID, source=source, light_svg=LIGHT, dark_svg=None
    )
    parser = PayloadParser()
    parser.feed(document)
    payload = json.loads(parser.payload)
    assert base64.b64decode(payload["source"]).decode() == source
    assert payload["title"] == title
    assert parser.scripts == 2
    assert "<img src=x" not in document
    assert '<template id="dark-svg"></template>' in document


@pytest.mark.browser
def test_viewer_theme_zoom_and_source_download(browser_session, tmp_path) -> None:
    source = "</script>é &amp;\u2028"
    document = build_html(
        title="Shop", engine=Engine.MERMAID, source=source, light_svg=LIGHT, dark_svg=DARK
    )
    with browser_session.page({"/": document}) as page:
        assert page.locator("#drawing > svg").text_content() == "Light"
        assert page.locator("#drawing > svg").count() == 1
        before = page.locator("#zoom").inner_text()
        page.locator("#zoom-in").click()
        assert page.locator("#zoom").inner_text() != before
        transform = page.locator("#drawing").evaluate("e => e.style.transform")
        page.locator("#theme").click()
        assert page.locator("#drawing > svg").text_content() == "Dark"
        assert page.locator("#drawing").evaluate("e => e.style.transform") == transform
        page.locator("#export").click()
        with page.expect_download() as pending:
            page.locator('[data-export="source"]').click()
        path = tmp_path / "download.mmd"
        pending.value.save_as(path)
        assert path.read_text() == source
        assert pending.value.suggested_filename == "shop.mmd"
