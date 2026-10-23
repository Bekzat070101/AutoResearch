# -*- coding: utf-8 -*-
"""paper-lab 的取论文脚本：arXiv HTML → 带 [S<n>] 句子编号的纯文本。不联网，用一段仿 LaTeXML 的页面当输入。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "paper-lab" / "scripts" / "fetch_paper.py"


@pytest.fixture(scope="module")
def fp():
    spec = importlib.util.spec_from_file_location("fetch_paper", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PAGE = """<html><head><title>x</title><script>ignore()</script></head><body>
<nav class="ltx_page_navbar">nav junk</nav>
<article class="ltx_document ltx_authors_1line">
<h1 class="ltx_title ltx_title_document">Measuring Things</h1>
<div class="ltx_authors"><span class="ltx_personname">Ann Author</span> Affiliation: Somewhere</div>
<div class="ltx_abstract"><h6 class="ltx_title ltx_title_abstract">Abstract</h6><p class="ltx_p">Body abstract copy.</p></div>
<section id="S1" class="ltx_section"><h2 class="ltx_title ltx_title_section"><span class="ltx_tag ltx_tag_section">1 </span>Introduction</h2>
<div class="ltx_para"><p class="ltx_p">Models reach <math alttext="43.9\\%" class="ltx_Math"><semantics><mn>43.9</mn></semantics></math> accuracy (Brown et al., 2020). Brown et al. (2020) also saw this, e.g. in Table 1. We agree.<span class="ltx_note ltx_role_footnote"><sup class="ltx_note_mark">1</sup><span class="ltx_note_outer"><span class="ltx_note_content"><sup class="ltx_note_mark">1</sup><span class="ltx_tag ltx_tag_note">1</span>We also re-ran this on unambiguous items. Results were similar.</span></span></span></p></div>
<figure class="ltx_figure"><figcaption>Figure 1: caption junk</figcaption><table class="ltx_tabular"><tr><td>cell</td></tr></table></figure>
<section id="S1.SS1" class="ltx_subsection"><h3 class="ltx_title ltx_title_subsection"><span class="ltx_tag">1.1 </span>Setup</h3>
<section class="ltx_paragraph"><h4 class="ltx_title ltx_title_paragraph">Prompt.</h4><div class="ltx_para"><p class="ltx_p">First sentence? Second sentence!</p><p class="ltx_p"><span class="ltx_text ltx_font_bold">Inline Head.</span> Third one. <span class="ltx_text ltx_font_bold">Not a head</span> stays.</p></div></section>
</section></section>
<section id="S2" class="ltx_section"><h2 class="ltx_title ltx_title_section"><span class="ltx_tag">2 </span>Empty Container</h2>
<section id="S2.SS1" class="ltx_subsection"><h3 class="ltx_title ltx_title_subsection">2.1 Results</h3><div class="ltx_para"><p class="ltx_p">Only line.</p></div></section></section>
<section class="ltx_section"><h2 class="ltx_title ltx_title_section">Acknowledgements</h2><div class="ltx_para"><p class="ltx_p">Thanks.</p></div></section>
<section class="ltx_bibliography"><h2 class="ltx_title ltx_title_bibliography">References</h2><ul><li class="ltx_bibitem">Brown 2020.</li></ul></section>
<section class="ltx_appendix"><h2 class="ltx_title ltx_title_appendix"><span class="ltx_tag">Appendix A </span>Extra Control</h2><div class="ltx_para"><p class="ltx_p">Random order control. Same conclusion.</p></div></section>
</article><footer>footer junk</footer></body></html>"""


def test_sentences_split_rules(fp):
    s = fp.sentences
    assert s("Models reach 43.9% accuracy (Brown et al., 2020). Brown et al. (2020) also saw this, e.g. in Table 1. We agree.") == [
        "Models reach 43.9% accuracy (Brown et al., 2020).",
        "Brown et al. (2020) also saw this, e.g. in Table 1.",
        "We agree.",
    ]
    assert s("See Fig. 3 for details. See Appendix A. Then? Yes! Done.") == ["See Fig. 3 for details.", "See Appendix A.", "Then?", "Yes!", "Done."]
    assert s("Decimal 3.5 stays. Next.") == ["Decimal 3.5 stays.", "Next."]
    assert s("") == []


def test_cert_failure_message_not_traceback(fp, monkeypatch):
    import urllib.error
    def boom(*a, **k):
        raise urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    monkeypatch.setattr(fp.urllib.request, "urlopen", boom)
    monkeypatch.setitem(__import__("sys").modules, "certifi", None)  # 当作没装 certifi
    with pytest.raises(SystemExit) as e:
        fp._get("https://arxiv.org/abs/0000.00000")
    assert "--html" in str(e.value) and "Install Certificates" in str(e.value)


def test_tex_alttext_cleanup(fp):
    assert fp._tex(r"43.9\%") == "43.9%"
    assert fp._tex(r"\mathrm{acc}\approx 0.5\times 10^{3}") == "acc≈ 0.5× 10^3"


def test_article_blocks_and_render(fp, tmp_path):
    page = tmp_path / "p.html"
    page.write_text(PAGE, encoding="utf-8")
    art = fp.parse_html("0000.00000", str(page))
    assert (art.title, art.authors, art.abstract) == ("Measuring Things", ["Ann Author"], "Body abstract copy.")
    blocks = art.blocks
    kinds = [k for k, _ in blocks]
    heads = [t for k, t in blocks if k == "head"]
    assert heads == ["1 Introduction", "1.1 Setup", "2 Empty Container", "2.1 Results", "Appendix A Extra Control"]
    assert ("runin", "Prompt.") in blocks
    joined = " ".join(t for _, t in blocks)
    assert "43.9%" in joined and "(Brown et al., 2020)" in joined
    assert ("runin", "脚注 1") in blocks and ("para", "We also re-ran this on unambiguous items. Results were similar.") in blocks
    assert ("para", "Random order control. Same conclusion.") in blocks
    for junk in ("caption junk", "cell", "Body abstract copy", "Ann Author", "nav junk", "footer junk", "Thanks", "Brown 2020"):
        assert junk not in joined, junk
    assert "Measuring Things" not in joined
    text = fp.render({"id": "0000.00000", "title": "Measuring Things", "authors": ["Ann Author"], "abstract": "We propose X. It works."}, blocks)
    lines = text.splitlines()
    assert lines[0] == "# Measuring Things" and lines[3] == "## Abstract"
    assert lines[4] == "[S1] We propose X." and lines[5] == "[S2] It works."
    assert "## 2 Empty Container" not in text  # 只有标题、没有句子的容器节不输出
    sec = text.split("## 1.1 Setup", 1)[1].split("## ", 1)[0]
    assert "**Prompt.**" in sec and "**Inline Head.**" in sec
    assert [ln for ln in sec.splitlines() if ln.startswith("[S")] == ["[S1] First sentence?", "[S2] Second sentence!", "[S3] Third one.", "[S4] Not a head stays."]
    intro = text.split("## 1 Introduction", 1)[1].split("## ", 1)[0]
    assert intro.strip().splitlines()[0] == "[S1] Models reach 43.9% accuracy (Brown et al., 2020)."
    assert "[S3] We agree." in intro
    assert "**脚注 1**\n[S4] We also re-ran this on unambiguous items.\n[S5] Results were similar." in intro  # 脚注跟在所在段后，编号接着排
    assert "## Appendix A Extra Control\n[S1] Random order control." in text
    assert "## 2.1 Results\n[S1] Only line." in text
    assert "Acknowledgements" not in text and "References" not in text and "Thanks." not in text
    assert "{{" not in text
