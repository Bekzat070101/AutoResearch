# -*- coding: utf-8 -*-
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import arxiv  # noqa: E402


HTML_TABLE = """<html><body><article>
<p>Intro line</p>
<table>
  <caption>Scores</caption>
  <tr><th>A</th><th>B</th><th>C</th></tr>
  <tr><td>1</td><td>2</td><td>3</td></tr>
</table>
<p>After table</p>
</article></body></html>"""


def test_html_to_text_keeps_table_structure():
    got = arxiv.parse_html(HTML_TABLE)
    assert "[[table:t1]]" in got["text"]
    assert "Intro line" in got["text"]
    assert "After table" in got["text"]
    assert got["tables"]
    t = got["tables"][0]
    assert t["id"] == "t1"
    assert t["headers"] == ["A", "B", "C"]
    assert t["rows"] == [["1", "2", "3"]]
    assert arxiv._html_to_text(HTML_TABLE) == got["text"]


def test_html_to_text_without_table_has_empty_tables():
    got = arxiv.parse_html("<html><article><p>Hello world</p></article></html>")
    assert got["tables"] == []
    assert "Hello world" in got["text"]


def test_broken_table_becomes_placeholder():
    page = "<html><article><p>X</p><table><tr></tr></table><p>Y</p></article></html>"
    got = arxiv.parse_html(page)
    assert "[[table:fail:" in got["text"]
    assert got["tables"] == []
