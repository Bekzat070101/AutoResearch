# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import quotes  # noqa: E402

PAPER = {"id": "2310.17623", "source": "html", "text": "Abstract\nWe show that it is possible to provide provable guarantees of test set contamination.\n1 Introduction\nLarge language models are trained on vast amounts of internet data.\n6 Limitations\nThe p-values presented in this paper do not have multi-\nple test corrections applied.\n7 Conclusion\nWe believe it is an exciting open problem."}


def test_hyphenated_quote_resolves_to_limitations():
    quotes.clear_prepared()
    got = quotes.locate("multiple test corrections", PAPER)
    assert got["found"] and got["section"] == "limitations"


def test_plain_quote_resolves_to_abstract():
    quotes.clear_prepared()
    got = quotes.locate("provable guarantees of test set contamination", PAPER)
    assert got["found"] and got["section"] == "abstract"
