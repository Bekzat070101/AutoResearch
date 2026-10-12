# -*- coding: utf-8 -*-
"""边学边练：项目成果压缩包的收取与评阅。

分工（见 skills/README.md）：
- 代码负责一切能确定的事：压缩包安全检查、文件清单、README 分段、引用的路径是否存在、有没有结果文件和代码；
- 模型只做判断题：按 skills/project-review/SKILL.md 看成果和项目要求对不对得上、写评语；
- 规则结果是上限：没有结果文件，模型也不能判「有自己的结果」；模型引用的原文必须真的出现在文件里。

只看交上来的文件，不推测作者能力；从不解压到磁盘、从不执行任何代码。
"""
from __future__ import annotations

import io
import json
import posixpath
import re
import struct
import zipfile
import zlib
from typing import Any

import llm
import skills
from schemas import now_iso

MAX_ZIP_BYTES = 20 * 1024 * 1024       # 压缩包本身
MAX_TOTAL_BYTES = 100 * 1024 * 1024    # 解压后总量
MAX_FILE_BYTES = 20 * 1024 * 1024      # 单个文件
MAX_FILES = 300
MAX_ENTRIES = 2000                     # 含目录的条目总数；在建条目对象之前就从目录尾记录里查
MAX_CENTRAL_DIR = 1024 * 1024          # 中央目录字节数上限：zipfile 按它逐条建对象，条目数可以造假，字节数不行；
                                       # 两千个条目、每个带很长的中文路径也在 1 MB 以内
MAX_RATIO = 120                        # 压缩比过高视为压缩炸弹
TEXT_BUDGET = 14000                    # 送给模型的正文上限（字符）
MAX_DOCX_XML = 8 * 1024 * 1024         # .docx 里 word/document.xml 解压后的上限（套娃压缩包同样要限）
MAX_TEXT_CHARS = 400_000               # 每个文件解码后最多留这么多字符，规则检查只看这么多

IGNORED = ("__MACOSX/", ".DS_Store", "Thumbs.db", ".git/", ".ipynb_checkpoints/", "__pycache__/")
CODE_EXT = {".py", ".ipynb", ".r", ".m", ".jl", ".cpp", ".c", ".h", ".java", ".js", ".ts", ".go", ".rs", ".sql", ".sh", ".do", ".stata", ".sas", ".tex"}
TEXT_EXT = {".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".py", ".r", ".m", ".jl", ".tex", ".sql", ".sh", ".do", ".js", ".ts", ".go", ".rs", ".c", ".cpp", ".h", ".java", ".html"}
FIGURE_EXT = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".pdf"}
DATA_EXT = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".parquet", ".npy", ".npz", ".mat", ".dta", ".sav", ".h5"}
README_NAMES = ("readme.md", "readme.txt", "readme", "说明.md", "说明.txt", "readme.markdown")

CRITERIA = [
    {"key": "problem", "criterion": "说清了要解决什么问题"},
    {"key": "results", "criterion": "有自己做出来的结果"},
    {"key": "match", "criterion": "和项目要求对得上"},
    {"key": "check", "criterion": "别人能照着核对或复现"},
    {"key": "limits", "criterion": "说清了没做完的和下一步"},
]

SECTION_MARKS = {
    "problem": ("题目", "问题", "目标", "项目", "要解决", "研究问题", "背景"),
    "done": ("做了什么", "方法", "过程", "步骤", "我做了", "实现", "思路"),
    "results": ("结果", "结论", "发现", "输出", "效果"),
    "reproduce": ("复现", "运行", "怎么跑", "环境", "依赖", "使用方法", "如何运行", "数据来源"),
    "limits": ("局限", "不足", "没做完", "未完成", "下一步", "改进", "问题与反思", "待改进"),
}


class SubmissionError(Exception):
    """压缩包本身不合格（不是成果好坏的问题），直接告诉用户怎么改。"""


# ---------- 读取压缩包 ----------

def _decode_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not info.flag_bits & 0x800:
        # Windows 自带压缩常用 GBK 写文件名却不打 UTF-8 标记，Python 会按 cp437 解出乱码
        try:
            name = name.encode("cp437").decode("gbk")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name.replace("\\", "/")


def _kind(path: str) -> str:
    low = path.lower()
    base = low.rsplit("/", 1)[-1]
    ext = "." + base.rsplit(".", 1)[-1] if "." in base else ""
    if base in README_NAMES:
        return "readme"
    if ext == ".ipynb":
        return "notebook"
    if ext in CODE_EXT:
        return "code"
    if ext in {".docx", ".doc", ".md", ".txt", ".pptx"}:
        return "doc"
    if ext in FIGURE_EXT:
        return "figure"
    if ext in DATA_EXT:
        return "data"
    return "other"


def _directory_size(data: bytes) -> tuple[int, int] | None:
    """从目录尾记录（含 zip64）读出条目总数和中央目录字节数，不建任何条目对象。
    返回 None 表示根本没有目录尾记录（不是 zip，交给 zipfile 报错，它也不会建条目）。
    zip64 记录和 zipfile 一样按「紧挨在定位记录前面」找，所以前面加了别的数据也能对上；
    有目录尾却核对不了 zip64 记录的，直接拒收，不让 zipfile 去按它的理解逐条建对象。"""
    tail = data[-(65535 + 22):]
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < 22:
        return None
    entries, cd_size = struct.unpack_from("<HI", tail, at + 10)
    eocd = len(data) - len(tail) + at  # 换回整个文件里的位置
    loc, rec = eocd - 20, eocd - 76
    has_loc = loc >= 0 and data[loc:loc + 4] == b"PK\x06\x07"
    if entries != 0xFFFF and cd_size != 0xFFFFFFFF and not has_loc:
        return entries, cd_size
    if not has_loc or rec < 0 or data[rec:rec + 4] != b"PK\x06\x06":
        raise SubmissionError("压缩包的目录记录核对不上（可能是 zip64 格式异常或前面拼接了别的数据）。请用系统自带的「压缩」重新打包。")
    return struct.unpack_from("<QQ", data, rec + 32)


def _open_zip(data: bytes) -> zipfile.ZipFile:
    """先查条目数和中央目录大小，再交给 zipfile。十万个空目录也会在打开时各建一个对象。"""
    size = _directory_size(data)
    if size and size[0] > MAX_ENTRIES:
        raise SubmissionError(f"压缩包里的条目太多（{size[0]} 个，含文件夹）。请删掉依赖目录（如 node_modules、venv）后再打包。")
    if size and size[1] > MAX_CENTRAL_DIR:
        raise SubmissionError(f"压缩包的文件名和附加信息太大（目录记录超过 {MAX_CENTRAL_DIR // 1024} KB）。"
                              "请缩短文件夹层级和文件名，或去掉依赖目录后再打包。")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SubmissionError("这不是一个能打开的 .zip 文件。请用系统自带的「压缩」重新打包（不要用 .rar / .7z）。")
    if len(z.infolist()) > MAX_ENTRIES:
        z.close()
        raise SubmissionError(f"压缩包里的条目太多（含文件夹超过 {MAX_ENTRIES} 个）。")
    return z


def _strip_xml(xml: str) -> str:
    """去标签、段落结束换行。线性扫描：原来的 <[^>]+> 遇到大量没闭合的 < 会反复扫到结尾，而且正则不释放 GIL，
    放在线程里也会拖住整个服务。没闭合的标签之后的内容直接丢掉。"""
    out, i, n = [], 0, len(xml)
    while i < n:
        lt = xml.find("<", i)
        if lt < 0:
            out.append(xml[i:])
            break
        out.append(xml[i:lt])
        gt = xml.find(">", lt + 1)
        if gt < 0:
            break
        if xml.startswith("</w:p>", lt):
            out.append("\n")
        i = gt + 1
    return "".join(out)


def _docx_text(blob: bytes) -> str:
    """.docx 本身也是压缩包：解压前先看声明的大小和压缩比，读的时候再按上限截断（声明可以造假）。"""
    try:
        with _open_zip(blob) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML or info.file_size > MAX_RATIO * max(1, info.compress_size):
                return ""
            with z.open(info) as f:
                raw = f.read(MAX_DOCX_XML + 1)
    except (KeyError, zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, SubmissionError):
        return ""
    if len(raw) > MAX_DOCX_XML:
        return ""
    return _strip_xml(raw.decode("utf-8", errors="ignore"))[:MAX_TEXT_CHARS]


# ---------- notebook：只取每个单元的 source，outputs 只看有没有 ----------
# 直接 json.loads 会把输出里的每个数字、每个小字典都建成对象：7 MB 的压缩包能吃掉近 200 MB 内存。
# 仍用标准库的 json（C 实现、线性、完整校验语法，坏文件照旧判不合格），但：
# - 不是单元、也不是顶层的对象，解析完立刻换成占位的 1，里面的内容随即释放；
# - 数字一律解析成 0（小整数是共享的，不新建对象）。
# 钩子只在对象解析完才被调用，对象里面的数组、字符串那时已经建好了；所以解析前先按字节数结构限额：
# 逗号和括号的个数近似 JSON 值的个数（字符串里的逗号也算，只会高估），超了就不解析，只提示清空输出再交。

MAX_CELLS = 5000
NB_MAX_BYTES = 8 * 1024 * 1024    # 限额以内最坏的情况（一个输出里十几万个长字符串）峰值约 30 MB
NB_MAX_VALUES = 150_000


def _nb_too_big(blob: bytes) -> bool:
    return len(blob) > NB_MAX_BYTES or blob.count(b",") + blob.count(b"[") + blob.count(b"{") > NB_MAX_VALUES


RESULT_OUTPUTS = ("stream", "execute_result", "display_data")  # 代码真的跑出了东西；error 不算


def _nb_object(pairs: list) -> Any:
    """只留顶层（有 cells）和单元（有 cell_type）；输出只留它的 output_type；元数据一律换成占位，哪怕它碰巧也有 source 键。"""
    if any(k == "cells" or k == "cell_type" for k, _ in pairs):
        return dict(pairs)
    for k, v in pairs:
        if k == "output_type" and isinstance(v, str):
            return {"output_type": v[:40]}
    return 1


def _cell_has_output(cell: dict) -> bool:
    """代码单元里有真的运行输出。原来任何单元只要 outputs 不空就算，Markdown 单元上挂一个 outputs 也拿到「有结果」（Codex 复现）。"""
    outs = cell.get("outputs")
    return (cell.get("cell_type") == "code" and isinstance(outs, list)
            and any(isinstance(o, dict) and o.get("output_type") in RESULT_OUTPUTS for o in outs))


def _ipynb_text(blob: bytes) -> tuple[str, bool]:
    try:
        nb = json.loads(blob.decode("utf-8", errors="ignore"), object_pairs_hook=_nb_object,
                        parse_int=lambda _s: 0, parse_float=lambda _s: 0, parse_constant=lambda _s: 0)
    except (json.JSONDecodeError, RecursionError):
        return "", False
    cells = nb.get("cells") if isinstance(nb, dict) else None
    if not isinstance(cells, list):
        return "", False
    parts, has_out, size = [], False, 0
    for cell in cells[:MAX_CELLS]:
        if not isinstance(cell, dict):
            continue
        src = cell.get("source", "")
        if size < MAX_TEXT_CHARS:
            text = "".join(map(str, src)) if isinstance(src, list) else str(src)
            parts.append(text)
            size += len(text)
        if _cell_has_output(cell):
            has_out = True
    return "\n\n".join(parts), has_out


def _text(blob: bytes, cut: bool = False) -> str:
    """cut=True 表示只读了文件开头：末尾可能截断在一个多字节字符中间，先去掉这半个字再判断编码。"""
    for enc, tail in (("utf-8", 3), ("gbk", 1)):
        for k in range(tail + 1 if cut else 1):
            try:
                return (blob[:len(blob) - k] if k else blob).decode(enc)
            except UnicodeDecodeError:
                continue
    return blob.decode("utf-8", errors="replace")


def _head(z: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> tuple[bytes, bool]:
    """只读文件开头 limit 字节：纯文本最后只留 MAX_TEXT_CHARS 个字符，没必要整份解压进内存。"""
    with z.open(info) as f:
        blob = f.read(limit)
    return blob, info.file_size > limit


def read_zip(data: bytes) -> dict[str, Any]:
    """安全读取：只在内存里读，返回文件清单和可读正文。不合格直接抛 SubmissionError。"""
    if len(data) > MAX_ZIP_BYTES:
        raise SubmissionError(f"压缩包超过 {MAX_ZIP_BYTES // 1024 // 1024} MB。大数据集请只放样例，并在 README 里写下载链接。")
    z = _open_zip(data)
    infos = [i for i in z.infolist() if not i.is_dir()]
    if len(infos) > MAX_FILES:
        raise SubmissionError(f"文件太多（{len(infos)} 个）。请删掉依赖目录（如 node_modules、venv）后再打包。")
    total = sum(i.file_size for i in infos)
    if total > MAX_TOTAL_BYTES:
        raise SubmissionError("解压后超过 100 MB。请只放必要的结果和代码。")
    files = []
    for info in infos:
        name = _decode_name(info)
        if any(tok in name for tok in IGNORED) or name.startswith("/") or ".." in name.split("/"):
            continue
        if info.file_size > MAX_FILE_BYTES or (info.compress_size and info.file_size / max(1, info.compress_size) > MAX_RATIO and info.file_size > 1024 * 1024):
            raise SubmissionError(f"文件「{name}」过大或压缩比异常，已拒收。")
        files.append((name, info))
    if not files:
        raise SubmissionError("压缩包是空的。")
    # 压缩包里常套一层同名文件夹：去掉共同的顶层目录，让 README 回到根目录
    tops = {n.split("/", 1)[0] for n, _ in files}
    strip = ""
    if len(tops) == 1 and all("/" in n for n, _ in files):
        strip = next(iter(tops)) + "/"
    inventory: list[dict[str, Any]] = []
    texts: dict[str, str] = {}
    notes: list[str] = []
    for name, info in files:
        path = name[len(strip):] if strip and name.startswith(strip) else name
        kind = _kind(path)
        item = {"path": path, "size": info.file_size, "kind": kind}
        low = path.lower()
        # 每个文件读完就截到上限再存，不把几十 MB 的完整正文攒到最后才截
        try:
            if kind in ("readme", "doc", "code", "notebook") or low.endswith((".csv", ".tsv", ".json")):
                if low.endswith((".doc", ".pptx")):
                    notes.append(f"「{path}」是 {low.rsplit('.', 1)[-1]} 格式，没有读取正文；关键内容请写进 README。")
                elif low.endswith(".docx"):  # 本身是压缩包，要整份；单个文件已限 20 MB
                    texts[path] = _docx_text(z.read(info))
                    if not texts[path]:
                        notes.append(f"「{path}」读不出正文（文件损坏或解压后过大），关键内容请写进 README。")
                elif low.endswith(".ipynb"):  # JSON 要整份才能解析
                    blob = z.read(info)
                    if _nb_too_big(blob):
                        texts[path] = ""
                        item["has_outputs"] = False  # 没解析就不算「有输出」：猜出来的证据不能给分
                        notes.append(f"「{path}」太大或输出太多，没有读取，也不计入「有输出」。请清空输出（Kernel → Restart & Clear Output）后再交，"
                                     "并把结果导出到 results/、在 README 里写明。")
                    else:
                        txt, has_out = _ipynb_text(blob)
                        texts[path] = txt[:MAX_TEXT_CHARS]
                        item["has_outputs"] = has_out
                    del blob
                elif low.endswith((".csv", ".tsv")):  # 只看前 12 行
                    blob, cut = _head(z, info, 64 * 1024)
                    texts[path] = "\n".join(_text(blob, cut).splitlines()[:12])
                else:
                    blob, cut = _head(z, info, MAX_TEXT_CHARS * 4)  # UTF-8 一个字最多 4 字节
                    texts[path] = _text(blob, cut)[:MAX_TEXT_CHARS]
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError):
            # 目录记录完好、成员数据坏了（CRC 不对、压缩方式不支持）要在这里拦下，不然一路抛成 500
            raise SubmissionError(f"压缩包里的「{name}」读不出来（文件损坏或用了不支持的压缩方式），请重新打包再交。")
        if low.endswith(".pdf"):
            notes.append(f"「{path}」是 PDF，没有读取正文，只算作一个结果文件。")
        item["empty"] = info.file_size == 0
        inventory.append(item)
    inventory.sort(key=lambda x: (x["kind"] != "readme", x["path"]))
    return {"inventory": inventory, "texts": {k: v[:MAX_TEXT_CHARS] for k, v in texts.items()}, "notes": notes}


# ---------- 规则检查（确定的事） ----------

def _readme(bundle: dict[str, Any]) -> tuple[str, str]:
    for item in bundle["inventory"]:
        if item["kind"] == "readme" and "/" not in item["path"]:
            return item["path"], bundle["texts"].get(item["path"], "")
    for item in bundle["inventory"]:
        if item["kind"] == "readme":
            return item["path"], bundle["texts"].get(item["path"], "")
    return "", ""


def _sections(text: str) -> dict[str, bool]:
    heads = "\n".join(ln for ln in text.splitlines() if ln.lstrip().startswith(("#", "**")) or len(ln.strip()) <= 20)
    hay = heads or text
    return {k: any(m in hay for m in marks) for k, marks in SECTION_MARKS.items()}


_PATH_RUN = re.compile(r"[\w\-./一-鿿]+")
_PATH_EXT = re.compile(r"\.(?:png|jpg|jpeg|svg|csv|xlsx|pdf|ipynb|py|txt|md|json|docx|r|m)", re.I)


def _path_tokens(text: str) -> set[str]:
    """README 里像文件路径的词：一段连续的路径字符，截到最后一个已知扩展名为止。
    线性扫描。原来的「字符类+ 扩展名」写法遇到很长、又不带扩展名的词会反复回溯（16 KB 要一秒）。"""
    out = set()
    for run in _PATH_RUN.findall(text):
        last = None
        for last in _PATH_EXT.finditer(run):
            pass
        if last is not None:
            out.add(run[:last.end()])
    return out


def _referenced_paths(text: str, inventory: list[dict[str, Any]], readme_path: str = "") -> tuple[list[str], list[str]]:
    paths = {i["path"] for i in inventory}
    names: dict[str, list[str]] = {}
    for p in paths:
        names.setdefault(p.rsplit("/", 1)[-1], []).append(p)
    found, missing = [], []
    base = readme_path.rsplit("/", 1)[0] if "/" in readme_path else ""
    for tok in _path_tokens(text):
        tok = re.sub(r"/+", "/", tok)
        # 按 README 所在目录解析，再规整掉 ./ 和 ../：work/docs/README.md 写的 ../results/x.csv 指 work/results/x.csv。
        # 原来先把开头的 ./ 和 ../ 剥掉再拼，../ 的意思就变了，明明在的结果被判成没找到（Codex 复现）
        rel = posixpath.normpath(posixpath.join(base, tok)) if base else posixpath.normpath(tok)
        root = posixpath.normpath(tok.lstrip("/"))  # 也认从压缩包根目录写起的
        if rel in paths:
            found.append(rel)
        elif root in paths:
            found.append(root)
        elif "/" not in root and len(names.get(root, [])) == 1:
            # 带目录的引用必须路径完全对上（写 results/ 实际在 data/ 算没找到）；只写文件名时，同名文件唯一才认
            found.append(names[root][0])
        elif not root.lower().startswith("readme"):
            missing.append(root)
    return sorted(set(found)), sorted(set(missing))


def rule_checks(bundle: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    inv = bundle["inventory"]
    readme_path, readme = _readme(bundle)
    sec = _sections(readme) if readme else {k: False for k in SECTION_MARKS}
    found, missing = _referenced_paths(readme, inv, readme_path)
    results = [i for i in inv if not i["empty"] and i["kind"] in ("figure", "data") and i["path"] != readme_path]
    results += [i for i in inv if not i["empty"] and i["path"].lower().startswith(("results/", "result/", "output/", "outputs/", "结果/")) and i not in results]
    code = [i for i in inv if i["kind"] in ("code", "notebook")]
    docs = [i for i in inv if i["kind"] == "doc"]
    notebook_out = any(i.get("has_outputs") for i in inv if i["kind"] == "notebook")
    title = project.get("name") or ""
    mentions_project = bool(title) and (title[:6] in readme or (project.get("url") or "#") in readme or (project.get("source_url") or "#") in readme)

    def status(ok: bool, partial: bool = False) -> str:
        return "pass" if ok else ("partial" if partial else "fail")

    caps = {
        # 规则能确定的上限：模型只能在这个范围内往下判
        "problem": status(bool(readme) and sec["problem"] and len(readme) >= 80, bool(readme)),
        # README 引用的文件里至少有一个是结果文件才算 pass：只引用代码、结果文件没人提，不能给满
        "results": status(bool(set(found) & {i["path"] for i in results}), bool(results) or notebook_out),
        "match": "pass" if readme else "partial",
        "check": status((bool(code) and sec["reproduce"]) or (not code and bool(docs or results) and sec["done"]), bool(code) or sec["reproduce"] or sec["done"]),
        "limits": status(sec["limits"]),
    }
    return {
        "readme_path": readme_path, "readme_len": len(readme), "sections": sec,
        "referenced": found, "missing_refs": missing,
        "result_files": [i["path"] for i in results][:20], "code_files": [i["path"] for i in code][:20],
        "doc_files": [i["path"] for i in docs][:10], "notebook_has_outputs": notebook_out,
        "mentions_project": mentions_project, "caps": caps,
    }


RULE_FIX = {
    "problem": "在根目录 README.md 开头写「## 题目」：项目名、来源链接，再用一两句自己的话说清要解决什么问题。",
    "results": "把图、表或输出放进 results/，并在 README 的「## 结果在哪」里写出每个文件的路径和它说明了什么。",
    "match": "对照项目要求逐条写：哪一条做了、结果在哪个文件；没做的也写出来。",
    "check": "写「## 怎么复现」：用了什么数据（链接）、运行哪条命令或打开哪个 notebook，能得到 results/ 里的哪个文件。",
    "limits": "加一段「## 还没做完的」：哪些要求没做到、结果哪里不可靠、下一步打算怎么改。",
}

RULE_PASS = {
    "problem": "README 写清了题目和要解决的问题。",
    "results": "有结果文件，README 也指向了它们。",
    "match": "README 在回应项目要求。",
    "check": "给出了复现或核对的方法。",
    "limits": "写了没做完的和下一步。",
}


def _rule_review(checks: dict[str, Any], project: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for c in CRITERIA:
        st = checks["caps"][c["key"]]
        if c["key"] == "match" and st == "pass":
            # 没有模型时，用项目要求里的词和 README 的重合度粗判；拿不准就记 partial
            st = "partial"
        ev = []
        if c["key"] == "results":
            ev = [{"file": p, "quote": ""} for p in checks["referenced"] if p in checks["result_files"]][:3]
        comment = RULE_PASS[c["key"]] if st == "pass" else RULE_FIX[c["key"]]
        if c["key"] == "results" and checks["missing_refs"]:
            comment += f" README 提到但压缩包里没有：{('、'.join(checks['missing_refs'][:3]))}。"
        if c["key"] == "match" and st == "partial":
            comment = "未连模型，没法逐条对照项目要求。" + RULE_FIX["match"]
        out.append({"key": c["key"], "criterion": c["criterion"], "status": st, "evidence": ev,
                    "comment": comment, "fix": "" if st == "pass" else RULE_FIX[c["key"]]})
    return out


# ---------- 模型评阅（判断题） ----------

ORDER = {"fail": 0, "partial": 1, "pass": 2}


def _corpus(bundle: dict[str, Any], readme_path: str) -> str:
    parts, used = [], 0
    ordered = sorted(bundle["texts"].items(), key=lambda kv: (kv[0] != readme_path, kv[0]))
    for path, txt in ordered:
        chunk = txt.strip()
        if not chunk:
            continue
        room = TEXT_BUDGET - used
        if room <= 200:
            break
        chunk = chunk[: min(len(chunk), 6000 if path == readme_path else 2500, room)]
        parts.append(f"=== 文件：{path} ===\n{chunk}")
        used += len(chunk)
    return "\n\n".join(parts)


def _llm_review(bundle: dict[str, Any], checks: dict[str, Any], project: dict[str, Any]) -> list[dict[str, Any]] | None:
    uid = project.get("user_id")
    if not llm.ready(uid):
        return None
    corpus = _corpus(bundle, checks["readme_path"])
    inv = "\n".join(f"- {i['path']}（{i['kind']}，{i['size']} 字节）" for i in bundle["inventory"][:60])
    req = json.dumps({k: project.get(k) for k in ("name", "practices", "todo", "source_name", "url")}, ensure_ascii=False)
    caps = json.dumps(checks["caps"], ensure_ascii=False)
    data = llm.chat_json(
        skills.load("project-review"),
        f"项目要求：{req}\n\n规则检查给出的上限（你只能等于或低于它）：{caps}\n"
        f"README 路径：{checks['readme_path'] or '（没有）'}；README 里提到但不存在的文件：{checks['missing_refs'][:5]}\n\n"
        f"文件清单：\n{inv}\n\n文件正文（节选）：\n{corpus}\n\n"
        '输出 JSON：{"criteria":[{"key":"problem|results|match|check|limits","status":"pass|partial|fail",'
        '"evidence":[{"file":"路径","quote":"原文，不超过 40 字"}],"comment":"一句话","fix":"没做到时写改哪个文件、补什么；做到了留空"}],'
        '"summary":"一两句，只说这份成果"}',
        timeout=45, tag="project-review", uid=project.get("user_id"),
    )
    items = (data or {}).get("criteria") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    # 模型给的结构先逐项核对类型：key 是列表、evidence 是数字这类，原来直接 500，额度扣了、评阅也没存（Codex 复现）
    by_key = {i["key"]: i for i in items if isinstance(i, dict) and isinstance(i.get("key"), str)}
    paths = {i["path"] for i in bundle["inventory"]}
    out = []
    for c in CRITERIA:
        it = by_key.get(c["key"])
        if not it:
            return None
        st = it.get("status") if isinstance(it.get("status"), str) else "fail"
        if st not in ORDER:
            st = "fail"
        cap = checks["caps"][c["key"]]
        if ORDER[st] > ORDER[cap]:
            st = cap
        ev = []
        raw_ev = it.get("evidence")
        for e in raw_ev if isinstance(raw_ev, list) else []:
            # 引文类型不对（列表、数字）就整条不要：原来当成空引文，只要文件在就算证据，承认没做完的项目也拿了 5/5（Codex 复现）
            if not isinstance(e, dict) or not isinstance(e.get("file"), str) or not isinstance(e.get("quote"), (str, type(None))):
                continue
            f, q = e["file"], (e.get("quote") or "").strip()
            # 文件必须真在压缩包里；写了引文就必须真在那个文件里。原来不存在的文件配空引文也算证据（Codex 复现）
            if f in paths and (not q or q in bundle["texts"].get(f, "")):
                ev.append({"file": f, "quote": q[:60]})
        if st == "pass" and not ev:
            st = "partial"  # 说做到了却拿不出一条站得住的证据（编的引文被滤掉了）：不算做到
        fix = str(it.get("fix") or "").strip()[:160]
        if st != "pass" and not fix:
            fix = RULE_FIX[c["key"]]
        out.append({"key": c["key"], "criterion": c["criterion"], "status": st, "evidence": ev[:3],
                    "comment": str(it.get("comment") or "").strip()[:140] or (RULE_PASS[c["key"]] if st == "pass" else RULE_FIX[c["key"]]),
                    "fix": "" if st == "pass" else fix})
    out_summary = str((data or {}).get("summary") or "").strip()[:160]
    if out_summary:
        out[0]["_summary"] = out_summary
    return out


def review(data: bytes, project: dict[str, Any]) -> dict[str, Any]:
    bundle = read_zip(data)
    checks = rule_checks(bundle, project)
    judged = _llm_review(bundle, checks, project)
    voice = "llm" if judged else "rules"
    if not judged:
        judged = _rule_review(checks, project)
    summary = judged[0].pop("_summary", "") if judged else ""
    passed = sum(1 for j in judged if j["status"] == "pass")
    first_fix = next((j for j in judged if j["status"] != "pass"), None)
    if not summary:
        summary = f"{len(CRITERIA)} 条里做到 {passed} 条。" + ("" if first_fix else "可以把这份成果当作下一个项目的起点。")
    return {
        "passed": passed, "total": len(CRITERIA), "criteria": judged,
        "summary": summary,
        "next_step": (f"先改「{first_fix['criterion']}」：{first_fix['fix']}" if first_fix else "五条都做到了。下一步可以挑一个难一档的项目。"),
        "inventory": bundle["inventory"], "notes": bundle["notes"],
        "checks": {k: checks[k] for k in ("readme_path", "sections", "referenced", "missing_refs", "result_files", "code_files")},
        "voice": voice, "reviewed_at": now_iso(),
    }
