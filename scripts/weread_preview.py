#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weread_preview.py —— 微信读书 EPUB 本地预览 + 合规体检

用法:
    python3 weread_preview.py book.epub
    python3 weread_preview.py book.epub --open --force
    python3 weread_preview.py book.epub --no-annotate     # 纯预览，不含批注层
    python3 weread_preview.py /path/to/unpacked_dir      # 已解包的目录

注: 默认内置「选中即批注」层（--annotate，可在正文选中文字/图片直接批注并导出 MD/JSON）。
    历史预览仍可用配套 inject_annotate.py 后处理注入。

产出（默认位于 <epub同级>/<书名>.preview/ 下）:
    index.html           单文件预览页（可直接 file:// 打开，或在 WorkBuddy 里预览）
    weread-report.md     合规体检报告（人读）
    weread-report.json   合规体检报告（机器读）

设计要点:
  * 章节内容挂进 Shadow DOM，书籍自带 CSS 原样注入、不会污染预览 UI 外壳；
  * 「模拟层」会在预览里复现微信读书的丢样式行为（删复杂选择器 / 去内联 style /
    忽略 text-indent+line-height / px 字号压回正文 / 图片按版心等比缩放），
    即「不导真机也能看到哪块会塌」的样式差异；
  * 章节按需挂载，避免整本（尤其扫描书）一次性加载所有图片。
"""

from __future__ import annotations

import argparse
import bisect
import json
import posixpath
import re
import shutil
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

import xml.etree.ElementTree as ET

# 「选中即批注」层（批注 CSS/HTML/JS）的单一来源：来自 inject_annotate.py，
# 生成预览时按 --annotate 开关注入 SHELL 的占位符；保持一份实现，避免漂移。
try:
    from inject_annotate import CSS as ANNO_CSS, HTML as ANNO_HTML, JS as ANNO_JS
except ImportError:
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from inject_annotate import CSS as ANNO_CSS, HTML as ANNO_HTML, JS as ANNO_JS

VERSION = "1.0.0"

CONT_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"

# 结构性标签：实测「结构型元素基本全活」，裸标签选择器视为安全
STRUCT_TAGS = {
    "body", "html", "p", "div", "span", "em", "strong", "i", "b", "u", "small", "big",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd",
    "blockquote", "pre", "code", "kbd", "samp", "table", "thead", "tbody", "tfoot",
    "tr", "th", "td", "caption", "colgroup", "col", "img", "figure", "figcaption",
    "hr", "sup", "sub", "a", "br", "section", "article", "aside", "nav", "header",
    "footer", "main", "ruby", "rt", "rp",
}

# 白名单式字体识别：只有这一整串能出楷体
KAITI_STACK = (
    '"汉仪楷体S","ETrump KaiTi","方正仿宋","FZFSJW--GB1-0","PingFang SC",'
    '-apple-system,"SF UI Text","Lucida Grande",STheiti,"Microsoft YaHei",sans-serif'
)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

class LineMap:
    """把字符偏移换算成行号。"""

    def __init__(self, text: str):
        self._nl = [m.start() for m in re.finditer("\n", text)]

    def line(self, off: int) -> int:
        return bisect.bisect_right(self._nl, off) + 1


def unquote_path(url: str) -> str:
    return unquote(url.split("#")[0].split("?")[0])


def resolve_url(url: str, base_dir: str) -> str:
    p = posixpath.normpath(posixpath.join(base_dir, unquote_path(url))) if base_dir else posixpath.normpath(unquote_path(url))
    return quote(p, safe="/-_.~")


def q(ns: str, tag: str) -> str:
    return "{%s}%s" % (ns, tag)


def local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


# --------------------------------------------------------------------------- #
# 解包 / 解析
# --------------------------------------------------------------------------- #

def unpack(epub: Path, root: Path, force: bool = False) -> bool:
    marker = root / ".unpack.json"
    st = epub.stat()
    if marker.exists() and not force:
        try:
            m = json.loads(marker.read_text("utf-8"))
            if (m.get("mtime") == st.st_mtime and m.get("size") == st.st_size
                    and m.get("src") == str(epub.resolve())):
                return False
        except Exception:
            pass
    if root.exists():
        if not (root.name.endswith(".preview") or marker.exists()):
            raise SystemExit("拒绝清理看起来不是预览目录的路径：%s" % root)
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(epub)) as z:
        for info in z.infolist():
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts:
                continue
            z.extract(info, str(root))
    marker.write_text(json.dumps({
        "src": str(epub.resolve()), "mtime": st.st_mtime, "size": st.st_size,
    }, ensure_ascii=False), "utf-8")
    return True


class Book:
    def __init__(self, root: Path):
        self.root = root
        self.opf_path = self._find_opf()
        self.opf_dir = posixpath.dirname(self.opf_path)
        self._read_opf()
        self._read_toc()

    # -- container / opf -------------------------------------------------- #
    def _find_opf(self) -> str:
        cpath = self.root / "META-INF" / "container.xml"
        if not cpath.exists():
            raise SystemExit("不是有效的 EPUB：缺少 META-INF/container.xml")
        tree = ET.parse(str(cpath))
        for rf in tree.getroot().iter(q(CONT_NS, "rootfile")):
            return rf.get("full-path")
        raise SystemExit("container.xml 里没有 rootfile")

    def _read_opf(self):
        tree = ET.parse(str(self.root / self.opf_path))
        pkg = tree.getroot()

        meta = {}
        md = pkg.find(q(OPF_NS, "metadata"))
        if md is not None:
            for el in md:
                t = local(el.tag)
                if t in ("title", "language", "creator", "publisher", "date", "identifier", "rights"):
                    if t == "identifier" and el.get("id", "").lower().find("uuid") < 0 and "identifier" in meta:
                        continue
                    meta.setdefault(t, (el.text or "").strip())
        self.meta = meta

        self.manifest = {}
        mf = pkg.find(q(OPF_NS, "manifest"))
        if mf is None:
            raise SystemExit("OPF 缺少 manifest")
        for it in mf.findall(q(OPF_NS, "item")):
            href = it.get("href") or ""
            self.manifest[it.get("id")] = {
                "id": it.get("id"),
                "href": href,
                "path": posixpath.normpath(posixpath.join(self.opf_dir, unquote_path(href))) if self.opf_dir else unquote_path(href),
                "media_type": it.get("media-type") or "",
                "properties": (it.get("properties") or "").split(),
            }

        self.spine = []
        sp = pkg.find(q(OPF_NS, "spine"))
        if sp is None:
            raise SystemExit("OPF 缺少 spine")
        self.spine_toc_id = sp.get("toc")
        for ref in sp.findall(q(OPF_NS, "itemref")):
            idref = ref.get("idref")
            item = self.manifest.get(idref)
            if not item:
                continue
            self.spine.append({
                "id": idref, "path": item["path"], "href": item["href"],
                "linear": (ref.get("linear") or "yes") == "yes",
            })

        self.nav_path = None
        for m in self.manifest.values():
            if "nav" in m["properties"] and m["media_type"] in ("application/xhtml+xml", "text/html"):
                self.nav_path = m["path"]
                break
        self.ncx_path = None
        if self.spine_toc_id and self.spine_toc_id in self.manifest:
            self.ncx_path = self.manifest[self.spine_toc_id]["path"]
        else:
            for m in self.manifest.values():
                if m["media_type"] == "application/x-dtbncx+xml":
                    self.ncx_path = m["path"]
                    break

    # -- toc -------------------------------------------------------------- #
    def _read_toc(self):
        entries = []
        if self.nav_path and (self.root / self.nav_path).exists():
            entries = self._read_nav(self.root / self.nav_path)
        if not entries and self.ncx_path and (self.root / self.ncx_path).exists():
            entries = self._read_ncx(self.root / self.ncx_path)
        if not entries:
            entries = [{"href": s["path"], "title": "", "depth": 0} for s in self.spine]
        self.toc = entries

        # 建立 spine 顺序 -> 标题 的映射
        index = {}
        for i, s in enumerate(self.spine):
            index[s["path"]] = i
        titles = {i: [] for i in range(len(self.spine))}
        for e in entries:
            p = e.get("path")
            if p in index:
                t = (e.get("title") or "").strip()
                if t and t not in titles[index[p]]:
                    titles[index[p]].append(t)
        for i, s in enumerate(self.spine):
            s["index"] = i
            s["title"] = " / ".join(titles.get(i) or []) or self._fallback_title(s["path"], i)

    def _fallback_title(self, path: str, i: int) -> str:
        f = self.root / path
        if not f.exists():
            return "第 %d 节" % (i + 1)
        try:
            txt = f.read_text("utf-8", errors="ignore")
        except Exception:
            return "第 %d 节" % (i + 1)
        m = re.search(r"<h[1-4][^>]*>(.*?)</h[1-4]>", txt, re.S | re.I)
        if m:
            t = re.sub(r"<[^>]+>", "", m.group(1))
            t = re.sub(r"\s+", " ", t).strip()
            if t:
                return t[:60]
        for tag in ("title", "h5", "h6"):
            m = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), txt, re.S | re.I)
            if m:
                t = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                if t:
                    return t[:60]
        return "第 %d 节" % (i + 1)

    def _read_nav(self, path: Path) -> list:
        try:
            tree = ET.parse(str(path))
        except Exception:
            return []
        navs = []
        for nav in tree.getroot().iter(q(XHTML_NS, "nav")):
            if (nav.get(q(EPUB_NS, "type")) or "").strip() == "toc":
                navs = [nav]
                break
        if not navs:
            navs = list(tree.getroot().iter(q(XHTML_NS, "nav")))[:1]
        out = []
        base = posixpath.dirname(self.nav_path or "")

        def walk(ol, depth):
            for li in ol.findall(q(XHTML_NS, "li")):
                a = li.find(q(XHTML_NS, "a"))
                if a is not None and a.get("href"):
                    out.append({
                        "href": a.get("href"),
                        "path": posixpath.normpath(posixpath.join(base, unquote_path(a.get("href")))),
                        "title": re.sub(r"\s+", " ", "".join(a.itertext())).strip(),
                        "depth": depth,
                    })
                elif a is None:
                    sp = li.find(q(XHTML_NS, "span"))
                    if sp is not None:
                        out.append({
                            "href": "", "path": None,
                            "title": re.sub(r"\s+", " ", "".join(sp.itertext())).strip(),
                            "depth": depth,
                        })
                for sub in li.findall(q(XHTML_NS, "ol")):
                    walk(sub, depth + 1)

        for ol in navs[0].findall(q(XHTML_NS, "ol")):
            walk(ol, 0)
        return out

    def _read_ncx(self, path: Path) -> list:
        NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
        try:
            tree = ET.parse(str(path))
        except Exception:
            return []
        out = []
        base = posixpath.dirname(self.ncx_path or "")

        def walk(np, depth):
            lbl = np.find(NCX + "navLabel")
            text = ""
            if lbl is not None:
                t = lbl.find(NCX + "text")
                if t is not None:
                    text = re.sub(r"\s+", " ", "".join(t.itertext())).strip()
            c = np.find(NCX + "content")
            src = c.get("src") if c is not None else ""
            if src:
                out.append({
                    "href": src,
                    "path": posixpath.normpath(posixpath.join(base, unquote_path(src))),
                    "title": text, "depth": depth,
                })
            for sub in np.findall(NCX + "navPoint"):
                walk(sub, depth + 1)

        root = tree.getroot()
        navmap = root.find(NCX + "navMap")
        if navmap is None:
            return []
        for np in navmap.findall(NCX + "navPoint"):
            walk(np, 0)
        return out

    # -- 内容 ------------------------------------------------------------- #
    def chapters(self):
        out = []
        for s in self.spine:
            f = self.root / s["path"]
            raw = ""
            if f.exists():
                raw = f.read_text("utf-8", errors="ignore")
            body = extract_body(raw)
            base_dir = posixpath.dirname(s["path"])
            body = rewrite_urls(body, base_dir)
            # 该文件 <link> 的样式表（微信读书按文件挂 CSS，整页背景等独立 CSS
            # 只应作用于 link 它的页面）
            css_ref = []
            for lm_ in LINK_TAG_RE.finditer(raw):
                tag = lm_.group(0)
                if not LINK_REL_RE.search(tag):
                    continue
                hm = LINK_HREF_RE.search(tag)
                if hm:
                    css_ref.append(posixpath.normpath(
                        posixpath.join(base_dir, unquote_path(hm.group(2)))))
            out.append({"title": s["title"], "file": s["path"], "html": body,
                        "bodyClass": extract_body_class(raw), "cssRef": css_ref})
        return out

    def css_files(self):
        out = []
        for m in self.manifest.values():
            if m["media_type"] == "text/css":
                out.append(m["path"])
        return out


BODY_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.S | re.I)
BODY_CLASS_RE = re.compile(r"<body[^>]*?\bclass\s*=\s*([\"'])(.*?)\1", re.I | re.S)
HEAD_STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.I)
LINK_REL_RE = re.compile(r"rel\s*=\s*[\"']?stylesheet", re.I)
LINK_HREF_RE = re.compile(r"href\s*=\s*([\"'])([^\"']*)\1", re.I)
ATTR_RE = re.compile(r'(\b(?:src|href|poster|data-src)\s*=\s*)(["\'])([^"\']*)\2', re.I)
CSS_URL_RE = re.compile(r'url\(\s*(["\']?)([^"\')]+)\1\s*\)', re.I)
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def extract_body(raw: str) -> str:
    m = BODY_RE.search(raw)
    return m.group(1) if m else raw


def extract_body_class(raw: str) -> str:
    """EPUB 里写在 <body class="..."> 上的类名。预览把 body 标签丢掉只挂 innerHTML，
    类名会跟着丢（比如版权页 body.cr-page 的底色），所以单独带回、挂到 .wr-body 上。"""
    m = BODY_CLASS_RE.search(raw)
    return m.group(2).strip() if m else ""


def rewrite_urls(frag: str, base_dir: str) -> str:
    def rep(m):
        url = m.group(3).strip()
        if not url or url.startswith("#") or SCHEME_RE.match(url):
            return m.group(0)
        frag_id = ""
        if "#" in url and not url.startswith("#"):
            frag_id = "#" + url.split("#", 1)[1]
        return "%s%s%s%s%s" % (m.group(1), m.group(2), resolve_url(url, base_dir), frag_id, m.group(2))
    return ATTR_RE.sub(rep, frag)


# 预览显示用：把规范楷体栈映射到本机楷体。本机多半没有汉仪楷体S/方正仿宋，
# 不映射的话楷体引文会回退成黑体，用户就没法在预览里核对「哪些段落是楷体」。
# 只影响预览显示，不回写 EPUB；最终字体效果仍以真机为准。
KAI_LOCAL = '"Kaiti SC","STKaiti","KaiTi"'
_KAI_NAMES = ("汉仪楷体S", "ETrump KaiTi", "方正仿宋", "FZFSJW--GB1-0")

# 宋体同理：微信读书白名单里的宋体名在 mac 上不存在，映射到本机宋体才能核对
SONG_LOCAL = '"Songti SC","STSong","SimSun"'
_SONG_NAMES = ("思源宋体", "Source Han Serif SC", "SourceHanSerifCN",
               "方正宋三", "方正宋三简体", "FZS3JW--GB1-0",
               "方正书宋", "FZSSJW--GB1-0", "方正悠宋", "FZYSJW--GB1-0",
               "京华老宋体", "寒蝉活宋体", "汉仪宋体")


def remap_kai_for_display(css: str) -> str:
    for name in _KAI_NAMES:
        css = css.replace('"%s"' % name, '%s,"%s"' % (KAI_LOCAL, name))
        css = css.replace("'%s'" % name, "%s,'%s'" % (KAI_LOCAL, name))
    return css


def remap_song_for_display(css: str) -> str:
    for name in _SONG_NAMES:
        css = css.replace('"%s"' % name, '%s,"%s"' % (SONG_LOCAL, name))
        css = css.replace("'%s'" % name, "%s,'%s'" % (SONG_LOCAL, name))
    return css


# 微信读书引擎内置规则（2026-10-11 真机实测）：官方书类名会被**强制渲染为宋体**，
# 精确匹配类名、与 CSS font-family / DOM 结构 / 元素标签无关；字号由类名决定（与元素无关），
# 仅首行缩进跟元素：h1/h2/h3 不缩进、p 缩进。
# 已确认的宋体家族（字号依次递减）：firstTitle > secondTitle > thirdTitle > fourthTitle，
# 另有 copyRightTitle = 大号但不加粗的宋体。
# 这些类的 font-family 压过作者 CSS（color 可覆盖；copyRightTitle 颜色锁黑）。
# 预览里用本机宋体 + !important 模拟该行为，避免误报「宋体没生效」。
ENGINE_SERIF_CLASSES = ("firstTitle", "secondTitle", "thirdTitle",
                        "fourthTitle", "copyRightTitle")


def engine_serif_css() -> str:
    sel = ", ".join(".%s" % c for c in ENGINE_SERIF_CLASSES)
    return ("/* ==== 微信读书引擎内置（预览模拟）==== */\n"
            "%s { font-family: %s !important; }\n" % (sel, SONG_LOCAL))


def rewrite_css_urls(css: str, base_dir: str) -> str:
    def rep(m):
        url = m.group(2).strip()
        if not url or SCHEME_RE.match(url) or url.startswith("#"):
            return m.group(0)
        return "url(%s)" % resolve_url(url, base_dir)
    return CSS_URL_RE.sub(rep, css)


# --------------------------------------------------------------------------- #
# 图片内嵌：让 index.html 彻底自包含
#
# 为什么必须做：预览面板（WorkBuddy / 各类 IDE 预览）通常只把「这一个 HTML 文件」
# 通过一条静态路由发给浏览器，同级目录下的 OEBPS/images/* 不会被一起提供，
# 于是所有相对路径图片 404（file:// 直开反而正常，所以很容易漏测）。
# 内嵌成 data URI 后，沙箱 iframe、data: 文档、只服务单文件的 http 路由都能正常出图。
# 预览只看版式，超过 --img-max 的图按最长边等比缩小，避免 HTML 体积失控。
# --------------------------------------------------------------------------- #
EMBED_ATTR_RE = re.compile(
    r'(\b(?:src|poster|data-src)\s*=\s*)(["\'])(?!data:|https?:|blob:|#)([^"\']+)\2', re.I)


def _data_uri_for(path: Path, max_px: int):
    """读图并转 data URI。返回 (uri, 原字节, 出字节, 缩放说明) 或 None。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    import base64
    import io
    import mimetypes

    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    out, note = raw, ""
    if max_px and mime.startswith("image/") and mime != "image/svg+xml":
        try:
            from PIL import Image

            im = Image.open(io.BytesIO(raw))
            w, h = im.size
            reencoded = False
            if max(w, h) > max_px:
                sc = max_px / float(max(w, h))
                im = im.resize((max(1, round(w * sc)), max(1, round(h * sc))), Image.LANCZOS)
                note = "缩至 %d×%d" % im.size
                reencoded = True
            keep_png = mime == "image/png" and im.mode in ("RGBA", "LA", "P")
            buf = io.BytesIO()
            if keep_png:
                im.save(buf, "PNG", optimize=True)
                mime = "image/png"
            else:
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                im.save(buf, "JPEG", quality=82, optimize=True)
                mime = "image/jpeg"
            cand = buf.getvalue()
            # 缩放/重编码没换来更小体积就退回原图（也避免 PNG 被重编码反而变差）
            if reencoded or len(cand) < len(raw):
                out = cand
            else:
                note = ""
        except Exception:
            out, note = raw, ""       # 没有 PIL 或解码失败：原样内嵌
    return ("data:%s;base64,%s" % (mime, base64.b64encode(out).decode("ascii")),
            len(raw), len(out), note)


def embed_images(chapters, root: Path, max_px: int):
    """把章节 HTML 里指向图片的 src 换成 data URI（同一资源只编码一次）。"""
    cache = {}
    stat = {"ok": 0, "miss": 0, "raw": 0, "out": 0, "scaled": 0, "misses": []}

    for ch in chapters:
        def rep(m, stat=stat):
            url = unquote_path(m.group(3)).strip()
            if not url:
                return m.group(0)
            if url not in cache:
                cache[url] = _data_uri_for(root / url, max_px)
            info = cache[url]
            if info is None:
                stat["miss"] += 1
                if len(stat["misses"]) < 5:
                    stat["misses"].append(url)
                return m.group(0)
            stat["ok"] += 1
            stat["raw"] += info[1]
            stat["out"] += info[2]
            if info[3]:
                stat["scaled"] += 1
            return "%s%s%s%s" % (m.group(1), m.group(2), info[0], m.group(2))

        ch["html"] = EMBED_ATTR_RE.sub(rep, ch["html"])

    stat["uniq"] = len(cache)
    return stat


# --------------------------------------------------------------------------- #
# CSS 解析 + 选择器判级
# --------------------------------------------------------------------------- #

def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), css, flags=re.S)


def iter_rules(css: str, base: int = 0):
    """产出 ('rule'|'at'|'stmt', selector, block, sel_off, block_off)，偏移已加上 base。"""
    i, n = 0, len(css)
    while i < n:
        while i < n and css[i] in " \t\r\n;":
            i += 1
        if i >= n:
            break
        j = i
        while j < n and css[j] not in "{};":
            j += 1
        if j >= n:
            break
        if css[j] == ";":
            yield ("stmt", css[i:j].strip(), "", base + i, base + j)
            i = j + 1
            continue
        if css[j] == "}":
            i = j + 1
            continue
        sel = css[i:j].strip()
        depth, k = 1, j + 1
        while k < n and depth:
            if css[k] == "{":
                depth += 1
            elif css[k] == "}":
                depth -= 1
            k += 1
        block = css[j + 1:k - 1]
        if not sel:
            i = k
            continue
        if sel.startswith("@"):
            if re.match(r"@(media|supports|document|layer)\b", sel, re.I):
                yield ("at", sel, block, base + i, base + j + 1)
            else:
                yield ("stmt", sel, block, base + i, base + j)
        else:
            yield ("rule", sel, block, base + i, base + j + 1)
        i = k


CLASS_RE = re.compile(r"^\.[^\s.#:\[\]>+~,*]+$")
TAG_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*$")


def classify_selector(sel: str) -> str:
    parts = [p.strip() for p in sel.split(",") if p.strip()]
    if not parts:
        return "simple"
    for p in parts:
        if p in ("html", "body", ":root"):
            continue
        if TAG_RE.match(p):
            if p.lower() in STRUCT_TAGS:
                continue
            return "complex"
        if CLASS_RE.match(p):
            continue
        return "complex"
    return "simple"


def style_key(path: str) -> str:
    return "Wb_" + re.sub(r"\W+", "_", path)


def rename_body(sel: str) -> str:
    out = []
    for p in sel.split(","):
        p = p.strip()
        p = re.sub(r"^(:root|html|body)\b", ".wr-body", p)
        out.append(p)
    return ", ".join(out)


# --------------------------------------------------------------------------- #
# 体检
# --------------------------------------------------------------------------- #

class Report:
    def __init__(self):
        self.items = {}     # id -> dict
        self.order = []

    def add(self, rid, level, item, css, why, fix, loc, snippet=""):
        if rid not in self.items:
            self.items[rid] = {
                "id": rid, "level": level, "item": item, "css": css,
                "why": why, "fix": fix, "count": 0, "samples": [],
            }
            self.order.append(rid)
        rec = self.items[rid]
        rec["count"] += 1
        if len(rec["samples"]) < 8:
            rec["samples"].append({"loc": loc, "snippet": snippet})

    def groups(self):
        rank = {"dead": 0, "gray": 1, "info": 2}
        return [self.items[i] for i in sorted(self.order, key=lambda k: (rank.get(self.items[k]["level"], 3), k))]

    def counts(self):
        c = {"dead": 0, "gray": 0, "info": 0}
        for r in self.items.values():
            c[r["level"]] = c.get(r["level"], 0) + 1
        return c


DECL_RULES = [
    # (正则, 规则号, 级别, 条目, css, 原因, 替代方案)
    (re.compile(r"text-indent\s*:", re.I), "R01", "dead", "首行缩进", "text-indent",
     "被 App 全局「正文首行缩进」接管，连 text-indent:0 都被缩进", "CSS 不写，交给 App 全局设置"),
    (re.compile(r"line-height\s*:", re.I), "R02", "dead", "行距", "line-height",
     "被 App 全局「行距」接管", "CSS 不写，交给 App 全局设置"),
    (re.compile(r"font-size\s*:\s*[-+]?[\d.]+px", re.I), "R03", "dead", "字号用 px", "font-size: <px>",
     "px 被当固定值压回正文大小", "改用 em / rem / % / pt"),
    (re.compile(r"page-break-(?:after|before|inside)\s*:|break-(?:after|before|inside)\s*:", re.I),
     "R10", "dead", "硬分页", "page-break / break-*",
     "不触发起新页；同文件双 h2 也不分页", "拆成独立 spine 文件"),
    (re.compile(r"display\s*:\s*(?:inline-)?(?:flex|grid)\b", re.I), "R12", "dead", "危险布局", "display:flex/grid",
     "flex 掉成上下堆叠", "普通块级元素 + 扁平单类名"),
    (re.compile(r"\btransform\s*:", re.I), "R12", "dead", "危险布局", "transform", "无效", "不要用"),
    (re.compile(r"text-shadow\s*:", re.I), "R12", "dead", "危险布局", "text-shadow", "无效", "不要用"),
    (re.compile(r"font-variant\s*:[^;]*small-caps", re.I), "R13", "dead", "小型大写", "font-variant:small-caps",
     "无效", "直接写大写字母"),
    (re.compile(r"\bfilter\s*:", re.I), "R12b", "gray", "滤镜（未验证）", "filter", "未验证，风险高", "避免使用"),
    (re.compile(r"\bposition\s*:\s*(?:fixed|absolute|sticky)", re.I), "R12c", "gray", "定位（未验证）", "position",
     "未验证，可能被忽略", "改普通文档流"),
    (re.compile(r"\bfloat\s*:", re.I), "R09", "gray", "图文绕排", "float",
     "语法层生效但被 float 的图片本体不渲染", "改为上下排列"),
    (re.compile(r"opacity\s*:|mix-blend-mode\s*:", re.I), "R12d", "gray", "透明度/混合（未验证）", "opacity",
     "未验证", "避免依赖"),
]

IMG_SIZE_RE = re.compile(r"\b(width|height|max-width|min-width|max-height|min-height)\s*:", re.I)
FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;]+)", re.I)


def lint_css(path: str, css: str, rep: Report, lm: "LineMap" = None, clean: str = None,
             base: int = 0, depth: int = 0):
    if lm is None:
        lm = LineMap(css)
    if clean is None:
        clean = strip_comments(css)
    for kind, sel, block, sel_off, blk_off in iter_rules(clean, base):
        loc = "%s:%d" % (path, lm.line(sel_off))
        if kind == "at":
            if "media" in sel.lower():
                rep.add("R05", "gray", "复杂选择器", "后代 / 伪类 / 伪元素 / @media",
                        "生效不稳定：时而只有 color 生效，时而 color+font-size 都生效",
                        "一律改写成扁平单类名", loc, sel[:80])
                if depth < 3:
                    lint_css(path, css, rep, lm, block, blk_off, depth + 1)
            continue
        if kind == "stmt":
            if sel.startswith("@font-face"):
                rep.add("R15", "gray", "内嵌字体", "@font-face",
                        "微信读书走内置字体识别白名单，自声明字体多半不生效（也会撑大体积）",
                        "改用已知字体栈（见 kaiti_replica），不随书分发字体文件", loc, sel[:80])
            elif sel.startswith("@import"):
                rep.add("R05b", "gray", "外部样式导入", "@import", "解析顺序不稳，可能整份失效",
                        "改成本地 <link> 或直接内联", loc, sel[:80])
            continue

        if classify_selector(sel) == "complex" and not re.match(r"^\s*(\.wr-body\b)", sel):
            rep.add("R05", "gray", "复杂选择器", "后代 / 复合 / 伪类 / 伪元素",
                    "微信读书只稳定认「扁平单类名」，复杂选择器时灵时不灵",
                    "拆成扁平单类名，如 .quote-kai", loc, sel[:80])

        # 逐条声明
        for m in re.finditer(r"([-\w]+)\s*:\s*([^;{}]+)", block):
            prop, val = m.group(1), m.group(2).strip()
            doff = blk_off + m.start()
            dloc = "%s:%d" % (path, lm.line(doff))
            snip = "%s: %s" % (prop, val[:60])
            for rx, rid, lvl, item, cssname, why, fix in DECL_RULES:
                if rx.search(prop + ":" + val):
                    rep.add(rid, lvl, item, cssname, why, fix, dloc, snip)
                    break
            if prop.lower() == "font-family":
                if "汉仪楷体S" not in val and "ETrump" not in val:
                    names = [n.strip().strip('"\'') for n in val.split(",") if n.strip()]
                    if len(names) == 1 or all(n in ("sans-serif", "serif", "monospace", "system-ui") for n in names):
                        rep.add("R04", "gray", "字体族", "font-family",
                                "微信读书走内置字体识别（白名单式）：单一自造名与系统字体名会回退全局字体",
                                "要楷体时整串复制 kaiti_replica 字体栈", dloc, snip)
                elif "汉仪楷体S" in val and "方正仿宋" not in val:
                    rep.add("R04b", "gray", "字体族（栈不完整）", "font-family",
                            "字体栈不完整也可能被回退", "整串照抄 kaiti_replica，不要只改其中几个名字", dloc, snip)
            if prop.lower() in ("width", "height", "max-width", "min-width", "max-height", "min-height"):
                if re.search(r"\bimg\b|\bfigure\b", sel, re.I):
                    rep.add("R07", "gray", "图片尺寸", "img 的定宽/限宽/定高",
                            "CSS 定宽 / 限宽 / 定高在微信读书里不生效",
                            "出图时把尺寸定死成像素", dloc, snip)
            if prop.lower() in ("margin", "margin-right", "margin-left"):
                if re.search(r"\bhr\b", sel, re.I) and "%" in val:
                    rep.add("R16", "dead", "hr 短居中线", "hr + 左右 % 边距",
                            "真机实测：右侧 % 边距被丢弃，短线会贴右（做过短居中线的属性）",
                            "短分隔线一律改成画进透明 PNG 的图片线；通栏线可用裸 hr 全宽", dloc, snip)
            if re.search(r"\bflex\b|\bgrid\b", prop + ":" + val, re.I) and prop.lower() != "display":
                rep.add("R12", "dead", "危险布局", "flex/grid 相关属性", "无效", "普通块级元素", dloc, snip)


INLINE_STYLE_RE = re.compile(r"<[^>]+\sstyle\s*=", re.I)
FOOTNOTE_RE = re.compile(r"epub:type\s*=\s*[\"'][^\"']*(footnote|noteref|endnote)[^\"']*[\"']", re.I)
ANCHOR_RE = re.compile(r'href\s*=\s*["\']#[^"\']+["\']', re.I)
FIGURE_RE = re.compile(r"<figure\b", re.I)
IMG_IN_P_RE = re.compile(r"<p[^>]*>\s*(?:<a[^>]*>\s*)?<img\b", re.I)
EXTPB_RE = re.compile(r"[\U00020000-\U0002FFFF\U00030000-\U0003FFFF]")
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
IMG_WH_ATTR_RE = re.compile(r'\s(width|height)\s*=\s*["\'][^"\']*["\']', re.I)


def lint_xhtml(path: str, raw: str, rep: Report, is_nav: bool = False):
    lm = LineMap(raw)
    body = extract_body(raw)

    # XML 合法性（微信读书是严格 XML 解析，坏文件会白屏）
    try:
        ET.fromstring(raw.encode("utf-8"))
    except Exception as e:
        rep.add("S01", "dead", "XHTML 不合法", "XML 解析失败",
                "微信读书按 XML 解析，解析失败的文件会整章白屏或变成纯文本",
                "用 xmllint --noout 定位并修好标签闭合/实体转义", "%s" % path,
                str(e)[:120])

    for m in INLINE_STYLE_RE.finditer(body):
        rep.add("R06", "dead", "内联样式", "[style] 属性",
                "内联 style 会被引擎拆掉，不能用来兜底",
                "改成扁平单类名", "%s:%d" % (path, lm.line(m.start())),
                body[m.start():m.start() + 70])

    for m in FOOTNOTE_RE.finditer(body):
        rep.add("R11", "dead", "弹注 / 锚点", "epub:type=footnote/noteref",
                "EPUB3 弹注内容会平铺成普通段落",
                "短注改行内小字夹注；长注放章末", "%s:%d" % (path, lm.line(m.start())),
                body[m.start():m.start() + 70])

    if not is_nav:
        anchors = list(ANCHOR_RE.finditer(body))
        if anchors:
            rep.add("R11b", "dead", "章内锚点跳转", 'href="#id"',
                    "自制书的章内锚点点击无反应（商店书能跳不等于自制书能跳）",
                    "角标只作视觉标记；需要跳转的改成视觉引导",
                    "%s（共 %d 处）" % (path, len(anchors)),
                    body[anchors[0].start():anchors[0].start() + 70])

    for m in FIGURE_RE.finditer(body):
        rep.add("R08", "gray", "图片居中", "figure / margin:0 auto",
                "figure 内的 img 仍靠左（figcaption 文字居中可生效）",
                "把居中直接画进图片像素", "%s:%d" % (path, lm.line(m.start())), "<figure>")
        break

    for m in IMG_IN_P_RE.finditer(body):
        rep.add("R17", "gray", "图片被包在 <p> 里", "<p><img></p>",
                "段首缩进是 App 全局接管，图片可能被首行缩进推离左边",
                "把图片段改成 <div class=\"figure-x\"> 或独立段落，避免承接首行缩进",
                "%s:%d" % (path, lm.line(m.start())), body[m.start():m.start() + 70])
        break

    for m in EXTPB_RE.finditer(body):
        ch = m.group(0)
        rep.add("R14", "dead", "生僻字", "CJK 扩展 B 区字符",
                "微信读书渲染成豆腐块", "避免使用，或转成图片",
                "%s:%d" % (path, lm.line(m.start())), "U+%04X %s" % (ord(ch), ch))
        break

    imgs = IMG_TAG_RE.findall(body)
    if imgs:
        wh = [t for t in imgs if IMG_WH_ATTR_RE.search(t)]
        if wh:
            rep.add("R18", "gray", "图片 HTML 尺寸属性", "<img width height>",
                    "与 CSS 定宽同理，尺寸应由出图阶段决定",
                    "删掉 width/height 属性，直接把尺寸做进图片", "%s（共 %d 张）" % (path, len(wh)),
                    wh[0][:90])


def lint_structure(book: Book, rep: Report, stats: dict):
    # nav 混进 spine
    for s in book.spine:
        if book.nav_path and s["path"] == book.nav_path:
            rep.add("S02", "dead", "目录页混进正文", "nav.xhtml 出现在 spine",
                    "目录会作为一章出现在正文里，且点击无反应",
                    "把 nav.xhtml 从 spine 的 itemref 里移除", s["path"], s["href"])

    # manifest <-> 磁盘
    for m in book.manifest.values():
        if not (book.root / m["path"]).exists():
            rep.add("S03", "dead", "清单缺文件", "manifest 声明的文件不存在",
                    "会导致图片/样式缺失", "核对 OPF 里的 href 大小写与真实文件名",
                    m["path"], m["href"])
    referenced = set()
    for s in book.spine:
        f = book.root / s["path"]
        if not f.exists():
            continue
        body = extract_body(f.read_text("utf-8", errors="ignore"))
        base = posixpath.dirname(s["path"])
        for m in ATTR_RE.finditer(body):
            url = m.group(3).strip()
            if not url or url.startswith("#") or SCHEME_RE.match(url):
                continue
            referenced.add(resolve_url(url, base))
    decl = {m["path"] for m in book.manifest.values()}
    missing = sorted(p for p in referenced if not p.endswith(".xhtml") and p not in decl
                     and not p.endswith(".html") and not p.endswith(".ncx"))
    if missing:
        rep.add("S04", "dead", "资源未注册", "被引用但不在 manifest 里",
                "微信读书可能不加载未注册资源（图片/样式直接消失）",
                "把该文件补进 OPF manifest", "共 %d 个，例：%s" % (len(missing), missing[0]), "")

    # 元数据
    if not book.meta.get("title"):
        rep.add("S05", "info", "元数据", "缺 dc:title", "书架显示为空标题", "在 OPF metadata 里补 dc:title", "OPF", "")
    if not book.meta.get("language"):
        rep.add("S05b", "info", "元数据", "缺 dc:language", "可能影响断词与字体回退", "补 <dc:language>zh-CN</dc:language>", "OPF", "")

    # 单文件内多标题（不会自动分页）
    for s in book.spine:
        f = book.root / s["path"]
        if not f.exists():
            continue
        txt = f.read_text("utf-8", errors="ignore")
        n = len(re.findall(r"<h[12]\b", txt, re.I))
        if n >= 2:
            rep.add("R10b", "gray", "章内多一级标题", "同一 spine 文件里多个 h1/h2",
                    "微信读书不会因此分页，长文会连排",
                    "需要分页就拆成独立 spine 文件", "%s（%d 个）" % (s["path"], n), "")

    # 体积
    total = sum(f.stat().st_size for f in book.root.rglob("*") if f.is_file())
    imgs = [m for m in book.manifest.values() if m["media_type"].startswith("image/")]
    img_bytes = 0
    big = []
    for m in imgs:
        f = book.root / m["path"]
        if f.exists():
            sz = f.stat().st_size
            img_bytes += sz
            big.append((sz, m["path"]))
    big.sort(reverse=True)
    stats.update({
        "epub_bytes": total, "img_count": len(imgs), "img_bytes": img_bytes,
        "spine_count": len(book.spine), "largest": big[:5],
    })
    if total > 100 * 1024 * 1024:
        rep.add("S06", "info", "体积", "整本 > 100MB",
                "上传/同步慢，手机端首开卡顿",
                "图片转 8 位调色板 PNG 或 JPEG q80 重新压一遍；1-bit 扫描页可压到 1/4", 
                "%.1f MB" % (total / 1048576), "")
    for sz, p in big[:1]:
        if img_bytes > 30 * 1024 * 1024 and sz > 2 * 1024 * 1024:
            rep.add("S07", "info", "单图过大", "存在 > 2MB 的单张图片",
                    "常见于扫描页存成 PNG", "扫描页改存 JPEG（灰度 q80）通常能降到 1/5",
                    "%s（%.1f MB）" % (p, sz / 1048576), "")

    # 图片分辨率（扫描书关键）
    try:
        small = []
        checked = 0
        for sz, p in big:
            if sz < 120 * 1024 or checked >= 60:
                continue
            checked += 1
            d = image_size(book.root / p)
            if d and d[0] < 800:
                small.append("%s (%dx%d)" % (p, d[0], d[1]))
        if small:
            rep.add("S08", "info", "图片分辨率偏低", "宽度 < 800px",
                    "手机上放大发虚", "出图宽度至少 1000px（微信读书按版心宽等比缩放）",
                    "共 %d 张，例：%s" % (len(small), small[0]), "")
    except Exception:
        pass


def image_size(p: Path):
    try:
        with p.open("rb") as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    b = f.read(1)
                    while b and b != b"\xff":
                        b = f.read(1)
                    while b == b"\xff":
                        b = f.read(1)
                    if not b:
                        return None
                    mk = b[0]
                    if mk in (0xD8, 0x01) or 0xD0 <= mk <= 0xD7:
                        continue
                    ln = int.from_bytes(f.read(2), "big")
                    if 0xC0 <= mk <= 0xCF and mk not in (0xC4, 0xC8, 0xCC):
                        d = f.read(ln - 2)
                        return int.from_bytes(d[3:5], "big"), int.from_bytes(d[1:3], "big")
                    f.seek(ln - 2, 1)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #

SHELL = r"""<!DOCTYPE html>
<html lang="zh-CN" data-ui="__UI__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ · 微信读书本地预览</title>
<style>
:root{
  --bg:#f4f5f7; --panel:#ffffff; --line:#e3e5e9; --fg:#1c1f24; --fg2:#61666e; --fg3:#8b9098;
  --accent:#3b6df5; --dead:#e5484d; --gray:#f5a623;
  --ui-bg:#ffffff; --ui-fg:#1c1f24; --ui-fg2:#61666e; --ui-line:#e6e8ec;
}
html[data-ui="dark"]{
  --ui-bg:#17181b; --ui-fg:#e8e9ec; --ui-fg2:#9aa0a6; --ui-line:#2b2d31;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--ui-bg);color:var(--ui-fg);
  font:13px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:var(--accent)}
.topbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  padding:10px 16px;border-bottom:1px solid var(--ui-line);background:var(--ui-bg);
  position:sticky;top:0;z-index:20}
.brand{font-weight:600;font-size:13px;white-space:nowrap}
.brand small{font-weight:400;color:var(--ui-fg2);margin-left:6px}
.ctrls{display:flex;align-items:center;gap:14px;flex-wrap:wrap;flex:1;min-width:0}
.ctl{display:flex;align-items:center;gap:6px;white-space:nowrap;color:var(--ui-fg2)}
.ctl b{color:var(--ui-fg);font-weight:500}
input[type=range]{width:92px;accent-color:var(--accent)}
select,input[type=text]{font:inherit;color:var(--ui-fg);background:transparent;
  border:1px solid var(--ui-line);border-radius:6px;padding:3px 6px}
.btn{font:inherit;color:var(--ui-fg);background:transparent;border:1px solid var(--ui-line);
  border-radius:6px;padding:4px 10px;cursor:pointer}
.btn:hover{border-color:var(--accent);color:var(--accent)}
/* 面板关闭按钮（与批注面板关闭按钮样式统一） */
.panel-close{border:1px solid #e3e5e9;background:#fff;border-radius:6px;padding:3px 8px;cursor:pointer;font:inherit;color:var(--ui-fg)}
.panel-close:hover{border-color:#3b6df5;color:#3b6df5}
.layout{display:flex;align-items:stretch;height:calc(100vh - 44px);overflow:hidden}
.side{width:260px;flex:0 0 auto;border-right:1px solid var(--ui-line);overflow:auto;padding:10px 6px 40px}
.side h4{margin:8px 10px 6px;font-size:11px;letter-spacing:.08em;color:var(--ui-fg2);font-weight:600}
.side ul{list-style:none;margin:0;padding:0}
.side li a{display:block;padding:5px 10px;border-radius:6px;color:var(--ui-fg);text-decoration:none;
  font-size:12.5px;line-height:1.45;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.side li a:hover{background:rgba(127,127,127,.12)}
.side li a.on{background:rgba(59,109,245,.14);color:var(--accent);font-weight:600}
.side li .num{color:var(--ui-fg3);font-size:11px;margin-right:6px;font-variant-numeric:tabular-nums}
.side li .cnt{float:right;color:var(--ui-fg3);font-size:10.5px;background:rgba(127,127,127,.14);
  border-radius:8px;padding:0 6px;line-height:16px;margin-left:6px}
.stage{flex:1;min-width:0;overflow:auto;padding:22px;display:flex;justify-content:center;
  align-items:flex-start;gap:20px;
  background:
    linear-gradient(0deg,rgba(127,127,127,.06),rgba(127,127,127,.06)),
    repeating-linear-gradient(45deg,transparent 0 12px,rgba(127,127,127,.03) 12px 24px)}
.wrap{order:1;flex:0 0 auto}
/* 机身尺寸对齐 Apple 官方设计稿（Apple iOS 27 UI Kit 的 iPhone 默认画板）：
   屏幕 402 × 874 CSS px（1206 × 2622 @3x，6.3 英寸）。
   黑边按真机实测约 2.45mm 折合 15px（边框过薄会让外壳比例偏长，并排看就「显长」），
   所以外壳宽 = 402 + 15*2 = 432、高 = 874 + 15*2 = 904，外壳高宽比 2.093 ≈ 真机 2.0925。
   高度固定 904 而不是 100vh——否则高宽比随窗口变化，整屏背景/图片的判断不可信；
   窗口太矮时由 .stage 内部滚动。
   版心左右各 20px → 正文列宽 402-40 = 362px，与真机微信读书基本一致。
   zoom:.8 是「物理尺寸矫正」：手机上 1 CSS px ≈ 0.166mm，而显示器上 1 CSS px ≈ 0.20~0.23mm，
   按 1:1 画出来会比真机大 20~40%，并排对比就会觉得「长不少」。缩到 0.8 后外壳的物理大小贴近真机，
   字号看起来也和真机同尺度；如果只想看最大最清楚的排版，把 zoom 改成 1 即可（布局完全不变）。 */
.phone{width:432px;flex:0 0 auto;margin:0 auto;border-radius:42px;padding:15px;
  background:#0e0f11;box-shadow:0 18px 44px rgba(0,0,0,.28);zoom:.8}
html[data-ui="light"] .phone{background:#1d1f23}
.screen{width:100%;height:874px;overflow:auto;
  border-radius:30px;background:var(--wr-bg);color:var(--wr-fg);padding:26px 20px;
  position:relative}
/* v1.1：#wr-host 撑满屏幕，否则 .wr-body{min-height:100%} 无解析基准，
   EPUB 里 html/body 级整页背景（版权页/章名页主题色）在预览里铺不满整屏，
   会被误判成「微信读书里也铺不满」（真机 body 背景会传播到画布）。
   注意必须用 height 而非 min-height——父级只有 min-height 时，
   子元素的百分比 min-height 仍解析为 auto（CSS 规范行为）。 */
#wr-host{height:100%}
.screen.theme-white{--wr-bg:#ffffff;--wr-fg:#1a1a1a}
.banner{width:300px;flex:0 0 auto;order:2;margin:0;padding:10px 12px;border-radius:8px;font-size:12px;
  border:1px solid var(--ui-line);color:var(--ui-fg2);background:rgba(127,127,127,.07)}

/* 宽屏：说明浮到舞台右上角（贴边），手机单独在舞台正中；
   窗口不够宽时退回「手机在左、说明在右」的常规排布，避免压住正文。 */
@media (min-width: 1280px){
  .stage{position:relative}
  .banner{position:absolute;top:8px;right:10px;width:300px}
}

/* ---- 窄屏（手机 / 侧边预览面板）：内容优先，工具面板抽屉化 ---- */
.mtoggle{display:none}
.mtoggle[aria-pressed="true"]{border-color:var(--accent);color:var(--accent);background:rgba(59,109,245,.08)}
.backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.38);z-index:35}
.drawer-head{display:none;align-items:center;justify-content:space-between;gap:8px;
  padding:4px 10px 8px;border-bottom:1px solid var(--ui-line);margin:0 0 6px}

@media (max-width: 900px){
  /* body 高度放开为 auto：sticky 顶栏只能在包含块（body 盒子）内吸附，
     若 body 被限制成一屏高（全局 html,body{height:100%}），滚过一屏后顶栏就跟着滚走了 */
  html,body{height:auto;overflow-x:clip}
  .topbar{gap:8px;padding:8px 10px}
  .brand{font-size:12.5px}
  .brand small{display:none}
  .mtoggle{display:inline-flex}
  .topbar .ctrls{display:none;width:100%;order:9;flex-wrap:wrap;gap:10px 14px}
  /* 布局改为自然文档流，页面本身滚动 */
  .layout{display:block;height:auto;overflow:visible}
  .stage{display:block;padding:0;overflow:visible;background:none;justify-content:flex-start}
  .stage>.wrap{width:100%}
  .banner{display:none}
  body.infoopen .banner{display:block;width:auto;order:0;margin:10px 12px 0}
  /* 机身外壳在窄屏上撤掉，正文铺满 */
  .phone{width:100% !important;max-width:100%;border-radius:0;padding:0;box-shadow:none;background:none;zoom:1}
  .screen{height:auto;min-height:0;border-radius:0;padding:18px 16px 40vh;overflow:visible}
  /* 目录：左侧抽屉 */
  .side{position:fixed;left:0;top:0;bottom:0;width:min(84vw,320px);z-index:40;
    background:var(--ui-bg);border-right:1px solid var(--ui-line);
    transform:translateX(-103%);transition:transform .22s ease;overflow:auto;padding:10px 6px 40px}
  body.navopen .side{transform:none;box-shadow:0 18px 50px rgba(0,0,0,.32)}
  .side h4{display:none}
  .drawer-head{display:flex}
  body.navopen .backdrop{display:block}
}

@media (max-width: 900px) and (min-width: 641px){
  .screen{padding:24px 22px 40vh}
}
</style>
__ANNO_CSS__
</head>
<body>
<header class="topbar">
  <div class="brand">微信读书 · 本地预览<small>__SUB__</small></div>
  <button class="btn mtoggle" id="mnav">目录</button>
  <button class="btn mtoggle" id="minfo">说明</button>
</header>

<div class="backdrop" id="backdrop"></div>
<div class="layout">
  <aside class="side">
    <div class="drawer-head"><b style="font-size:12.5px">目录 / SPINE</b>
      <button class="panel-close" id="closeNav" title="关闭">✕</button></div>
    <h4>目录 / SPINE</h4>
    <ul id="toc"></ul>
  </aside>
  <main class="stage">
    <div class="banner"><b>本地模拟预览</b>，非微信读书原生引擎；已按真机默认行为（模拟层全开）模拟「样式丢弃」：复杂选择器 / @media、内联 style、text-indent / line-height、px 字号、图片按版心缩放、hr 右侧边距均已按真机处理。
      字体栈（汉仪楷体S）本机多半不存在，已自动映射为本机楷体（Kaiti SC）<b>近似显示</b>；
      官方类名 <code>firstTitle/thirdTitle/copyRightTitle</code> 已按真机规律强制显示为宋体——最终效果仍需真机确认。</div>
    <div class="wrap">
      <div class="phone"><div class="screen theme-white" id="wr-screen"><div id="wr-host"></div></div></div>
    </div>
  </main>
</div>

<script type="application/json" id="wr-data">__DATA__</script>
<script>
/* ============================ 预览引擎 ============================ */
(function(){
"use strict";
window.addEventListener('error', function(e){
  var d = document.createElement('div');
  d.style.cssText = 'position:fixed;left:10px;bottom:10px;z-index:999;background:#e5484d;color:#fff;'
    + 'padding:8px 12px;border-radius:8px;font:12px/1.5 ui-monospace,Menlo,monospace;max-width:70vw';
  d.textContent = 'JS 错误: ' + (e.message || '?') + ' @line ' + (e.lineno || '?');
  document.body.appendChild(d);
});
var DATA = JSON.parse(document.getElementById('wr-data').textContent);
var LS = 'wr-preview-cfg-v1';
/* 预览面板常以 data: / 沙箱 iframe 等「不透明源」加载本页，此时访问 localStorage 会抛
   SecurityError（Storage is disabled / Access is denied）。这里全部包 try/catch，
   否则初始化中断、整页打不开（真机浏览器 file:// 下则一切正常，所以容易漏）。 */
var STORE = (function(){
  function raw(){ try { return window.localStorage; } catch(e){ return null; } }
  return {
    get: function(k){ var s = raw(); if(!s) return null; try { return s.getItem(k); } catch(e){ return null; } },
    set: function(k,v){ var s = raw(); if(!s) return; try { s.setItem(k,v); } catch(e){} },
    del: function(k){ var s = raw(); if(!s) return; try { s.removeItem(k); } catch(e){} }
  };
})();
var savedCfg = {};
try { savedCfg = JSON.parse(STORE.get(LS) || '{}') || {}; } catch(e){ savedCfg = {}; }
/* 模拟层全开（固定默认，不再提供开关）；字号/行距/首行缩进使用真机默认全局设置，
   直接应用到 .wr-body，不暴露 UI 调节。 */
var CFG = Object.assign({
  fs:17, lh:1.8, indent:2,
  dropComplex:true, dropInline:true, ignoreTypo:true,
  pxFont:true, imgFit:true, hrMargin:true
}, savedCfg);

var SCREEN = document.getElementById('wr-screen');
var HOST = document.getElementById('wr-host');
var SHADOW = HOST.attachShadow({mode:'open'});
var BOOK_CSS = Array.isArray(DATA.css) ? DATA.css.join('\n') : (DATA.css || '');
var curCSS = null;
/* 每章只挂自己 <link> 的样式表（真机行为）；无 cssIdx 的旧数据兜底为全书同一份 */
function cssFor(i){
  var ch = DATA.chapters[i];
  if(!ch || !ch.cssIdx) return BOOK_CSS;
  var out = [];
  for(var k=0;k<ch.cssIdx.length;k++){ var b = DATA.css[ch.cssIdx[k]]; if(b) out.push(b); }
  return out.length ? out.join('\n') : BOOK_CSS;
}
var active = 0;

/* ---- 选择器判级（与 Python 侧同一套规则）---- */
var STRUCT = __STRUCT__;
function isComplex(sel){
  var parts = sel.split(',').map(function(s){return s.trim()}).filter(Boolean);
  if(!parts.length) return false;
  for(var i=0;i<parts.length;i++){
    var p = parts[i];
    if(p === 'html' || p === 'body' || p === ':root') continue;
    if(/^[a-zA-Z][a-zA-Z0-9]*$/.test(p)){ if(STRUCT[p.toLowerCase()]) continue; return true; }
    if(/^\.[^\s.#:\[\]>+~,*]+$/.test(p)) continue;
    return true;
  }
  return false;
}

/* ---- 重建 shadow：把「模拟层」的处理真的落到 CSSOM 上 ---- */
function build(){
  if(curCSS === null) curCSS = cssFor(active);
  var bookStyle = '<style id="wr-book">' + curCSS.replace(/<\/style/gi,'<\\/style') + '</style>';
  SHADOW.innerHTML = bookStyle + '<style id="wr-emu"></style>'
    + '<div class="wr-body"><div class="wr-secs"></div></div>';

  var el = SHADOW.querySelector('#wr-book');
  var sheet = el && el.sheet;
  if(sheet){
    var drop = [];
    walk(sheet, function(rule, parent, index){
      if(rule.type === 4 || rule.type === 12){          // MEDIA / SUPPORTS
        if(CFG.dropComplex) drop.push([parent, index, null]);
        return;
      }
      if(rule.type !== 1 || !rule.style) return;         // 只处理 STYLE_RULE
      var sel = rule.selectorText || '';
      if(CFG.dropComplex && isComplex(sel)){
        drop.push([parent, index, sel]);
        return;
      }
      if(CFG.ignoreTypo){
        if(rule.style.getPropertyValue('text-indent')){ rule.style.removeProperty('text-indent'); }
        if(rule.style.getPropertyValue('line-height')){ rule.style.removeProperty('line-height'); }
      }
      if(CFG.pxFont){
        var fs = rule.style.getPropertyValue('font-size');
        if(fs && /px\s*$/i.test(fs.trim())){ rule.style.setProperty('font-size','1em'); }
      }
      if(CFG.imgFit && /\bimg\b/i.test(sel)){
        ['width','height','max-width','min-width','max-height','min-height'].forEach(function(p){
          rule.style.removeProperty(p);
        });
      }
      if(CFG.hrMargin && /(^|[\s,>])hr(\b|$)/i.test(sel)){
        if(rule.style.getPropertyValue('margin-right')) rule.style.setProperty('margin-right','0');
      }
    });
    drop.sort(function(a,b){ return b[1]-a[1]; });
    for(var i=0;i<drop.length;i++){
      var d = drop[i];
      try{ d[0].deleteRule(d[1]); }catch(e){}
    }
  }
  applyEmu();
  mount(active);
}

function walk(sheet, fn){
  var rules;
  try{ rules = sheet.cssRules; }catch(e){ return; }
  if(!rules) return;
  for(var i=0;i<rules.length;i++){
    var r = rules[i];
    fn(r, sheet, i);
    if((r.type === 4 || r.type === 12) && r.cssRules) walk(r, fn);
  }
}

function applyEmu(){
  var s = '';
  s += '.wr-body{font-size:' + CFG.fs + 'px;line-height:' + CFG.lh + ';color:var(--wr-fg);min-height:100%}';
  s += '.wr-body *{line-height:' + CFG.lh + ' !important}';
  if(CFG.indent > 0) s += '.wr-body p,.wr-body .p-flat,.wr-body blockquote p{text-indent:' + CFG.indent + 'em !important}';
  else s += '.wr-body p,.wr-body .p-flat,.wr-body blockquote p{text-indent:0 !important}';
  if(CFG.imgFit){
    s += '.wr-body img{max-width:100% !important;height:auto !important}';
    s += '.wr-body figure img{margin:0;display:block}';
  }
  SHADOW.querySelector('#wr-emu').textContent = s;
}

var sec = null;
function mount(i){
  active = i;
  /* 章节切换时按该章 <link> 换样式表（css 变了才重建 shadow） */
  var cssNeed = cssFor(i);
  if(cssNeed !== curCSS){ curCSS = cssNeed; build(); }
  var secs = SHADOW.querySelector('.wr-secs');
  secs.innerHTML = '';
  var ch = DATA.chapters[i];
  if(!ch) return;
  // EPUB 里 <body class="..."> 的类名挂到 .wr-body（body 的等价物），还原 body 级样式（如版权页底色）
  var bodyEl = SHADOW.querySelector('.wr-body');
  if(bodyEl){
    bodyEl.className = 'wr-body' + (ch.bodyClass ? ' ' + String(ch.bodyClass).replace(/\s+/g,' ').trim() : '');
  }
  sec = document.createElement('section');
  sec.className = 'wr-sec';
  sec.dataset.idx = i;   /* 当前章索引：供外部批注层等定位用 */
  sec.innerHTML = ch.html;
  secs.appendChild(sec);

  // 内联 style 被拆（模拟层：真机会把内联 style 拆掉）
  if(CFG.dropInline){
    var styled = sec.querySelectorAll('[style]');
    for(var k=0;k<styled.length;k++){
      styled[k].removeAttribute('style');
    }
  }
  if(CFG.imgFit){
    var imgs = sec.querySelectorAll('img[width],img[height]');
    for(var m=0;m<imgs.length;m++){ imgs[m].removeAttribute('width'); imgs[m].removeAttribute('height'); }
  }
  SCREEN.scrollTop = 0;
  highlightToc(i);
  document.body.classList.remove('navopen');       // 窄屏选完章节收起目录抽屉
  if(isNarrow()) window.scrollTo(0, 0);
}

/* ---- TOC ---- */
function renderToc(){
  var ul = document.getElementById('toc');
  var html = '';
  var list = DATA.toc || [];
  for(var i=0;i<list.length;i++){
    var idx = (typeof list[i].index === 'number') ? list[i].index : i;
    var last = (typeof list[i].last === 'number') ? list[i].last : idx;
    var t = (list[i].title || ('第 ' + (idx+1) + ' 节'));
    var badge = (list[i].count > 1) ? '<span class="cnt">&times;' + list[i].count + '</span>' : '';
    html += '<li><a href="#" data-i="' + idx + '" data-last="' + last + '" title="' + esc(t) + '"><span class="num">' + (i+1) + '</span>' + esc(t) + badge + '</a></li>';
  }
  ul.innerHTML = html;
  ul.addEventListener('click', function(e){
    var a = e.target.closest('a[data-i]');
    if(!a) return;
    e.preventDefault();
    mount(parseInt(a.dataset.i,10));
  });
}
function highlightToc(i){
  var as = document.querySelectorAll('#toc a');
  for(var k=0;k<as.length;k++){
    var lo = parseInt(as[k].dataset.i,10);
    var hi = (as[k].dataset.last === undefined) ? lo : parseInt(as[k].dataset.last,10);
    as[k].classList.toggle('on', i >= lo && i <= hi);   // 合并条目用区间高亮
  }
  var cur = document.querySelector('#toc a.on');
  if(cur && cur.scrollIntoView) cur.scrollIntoView({block:'nearest'});
}
function esc(s){ return String(s).replace(/[&<>"]/g, function(c){ return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]; }); }

/* ---- 外壳控件（已简化：主题 / 字号 / 行距 / 首行缩进 / 版心宽 / 模拟层均固定为默认，仅保留目录 / 说明触发）---- */
function on(el, ev, fn){ var n = document.getElementById(el); if(n) n.addEventListener(ev, fn); }

/* ---- 窄屏抽屉与折叠面板 ---- */
var MQ = window.matchMedia('(max-width: 900px)');
function isNarrow(){ return MQ.matches; }
function closepanels(){
  document.body.classList.remove('navopen','infoopen');
  syncToggle();
}
function toggleBodyClass(name){
  var on1 = document.body.classList.contains(name);
  closepanels();
  if(!on1) document.body.classList.add(name);
  syncToggle();
}
function syncToggle(){
  var b = document.getElementById('minfo');
  if(b) b.setAttribute('aria-pressed', String(document.body.classList.contains('infoopen')));
}
on('mnav','click', function(){ if(document.body.classList.contains('navopen')){ closepanels(); } else { toggleBodyClass('navopen'); } });
on('minfo','click', function(){
  toggleBodyClass('infoopen');
  /* 窄屏：说明展开在正文上方，若用户已向下滚动，平滑回到顶部才看得到 */
  if(isNarrow() && document.body.classList.contains('infoopen')){
    try{ window.scrollTo({top:0, behavior:'smooth'}); }catch(e){ window.scrollTo(0,0); }
  }
});
on('backdrop','click', closepanels);
document.addEventListener('click', function(e){
  if(e.target.closest && e.target.closest('#closeNav, #closeReport')) closepanels();
});
var mqOnChange = function(){
  closepanels();
};
if(MQ.addEventListener) MQ.addEventListener('change', mqOnChange); else if(MQ.addListener) MQ.addListener(mqOnChange);

function showFatal(e){
  var box = document.body;
  var d = document.createElement('div');
  d.style.cssText = 'margin:20px;padding:14px 16px;border-radius:10px;background:#fff4f4;color:#a11;'
    + 'font:12.5px/1.7 -apple-system,"PingFang SC",sans-serif;border:1px solid #f3c2c2';
  d.textContent = '预览渲染失败：' + (e && e.message ? e.message : e) + '（目录仍可用）';
  box.appendChild(d);
}
function boot(){
  try { renderToc(); } catch(e){}
  var start = 0, hm = /^#c=(\d+)$/.exec(location.hash || '');
  if(hm){ start = Math.max(0, Math.min(parseInt(hm[1],10), DATA.chapters.length-1)); }
  try { build(); } catch(e){ showFatal(e); }
  try { mount(start); } catch(e){}
}
boot();
})();
</script>
__ANNO_HTML__
__ANNO_JS__
</body>
</html>
"""


def collapse_toc(chapters) -> list:
    """目录抽屉用：把「连续同名」的 spine 文件合并成一条。

    为绕过微信读书不支持 CSS 硬分页，常把一个逻辑章节拆成多个 spine
    文件（color01.xhtml … colorNN.xhtml，标题都是「文前」）。按 spine
    逐文件列目录会出现 N 行同名条目，这里把它们合并为一条（带 ×N 角标），
    点击跳到该组首页。真机目录（toc.ncx / nav.xhtml）本来就只有一条。
    """
    out = []
    for i, ch in enumerate(chapters):
        t = (ch.get("title") or "").strip()
        if out and t and out[-1]["title"] == t:
            out[-1]["last"] = i
            out[-1]["count"] += 1
        else:
            out.append({"title": t, "index": i, "last": i, "count": 1})
    return out


def render(book: Book, chapters, css_blocks, report: Report, stats: dict, ui: str, annotate: bool = True) -> str:
    data = {
        "meta": book.meta,
        "css": css_blocks,
        "chapters": chapters,
        "toc": collapse_toc(chapters),
        "report": {"groups": report.groups(), "counts": report.counts(), "stats": stats},
        "generated": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    title = book.meta.get("title") or "未命名"
    out = SHELL.replace("__DATA__", payload)
    out = out.replace("__TITLE__", title)
    out = out.replace("__SUB__", "· %d 章 · %s" % (len(book.spine), data["generated"]))
    out = out.replace("__UI__", ui)
    out = out.replace("__STRUCT__", json.dumps({t.lower(): 1 for t in STRUCT_TAGS}))
    if annotate:
        out = out.replace("__ANNO_CSS__", ANNO_CSS)
        out = out.replace("__ANNO_HTML__", ANNO_HTML)
        out = out.replace("__ANNO_JS__", ANNO_JS)
    else:
        out = out.replace("__ANNO_CSS__", "")
        out = out.replace("__ANNO_HTML__", "")
        out = out.replace("__ANNO_JS__", "")
    return out


def render_markdown(book: Book, report: Report, stats: dict) -> str:
    c = report.counts()
    title = book.meta.get("title") or "未命名"
    L = []
    L.append("# 微信读书合规体检：%s" % title)
    L.append("")
    L.append("- 规则来源：`weread-epub-style-support`（真机实测 24 条）")
    L.append("- 规模：spine %d 个文件 · 图片 %d 张（%.1f MB）· 整本 %.1f MB"
             % (stats.get("spine_count", 0), stats.get("img_count", 0),
                stats.get("img_bytes", 0) / 1048576, stats.get("epub_bytes", 0) / 1048576))
    L.append("- 结果：**死 %d 类 · 灰 %d 类 · 提示 %d 类**" % (c["dead"], c["gray"], c["info"]))
    L.append("")
    if c["dead"] == 0 and c["gray"] == 0:
        L.append("没有发现死/灰规则，可以进真机验收。")
    for r in report.groups():
        L.append("## [%s] %s（%d 处）" % (r["id"], r["item"], r["count"]))
        L.append("")
        L.append("- 涉及：`%s`" % r["css"])
        L.append("- 为什么不行：%s" % r["why"])
        L.append("- 怎么改：**%s**" % r["fix"])
        for s in r["samples"][:6]:
            L.append("  - `%s`%s" % (s["loc"], ("　`%s`" % s["snippet"].replace("\n", " ")) if s["snippet"] else ""))
        if r["count"] > len(r["samples"][:6]):
            L.append("  - …另有 %d 处" % (r["count"] - len(r["samples"][:6])))
        L.append("")
    L.append("---")
    L.append("")
    L.append("## 出书前自检清单")
    L.append("")
    for item in [
        "样式全是扁平单类名，没有任何内联 style 属性",
        "CSS 里没有 text-indent / line-height / px 字号 / 单一字体名依赖",
        "图片尺寸按目标版心定死像素，需要居中的已画进图片",
        "需要硬分页处已拆成独立 spine 文件（nav.xhtml 不要放进 spine）",
        "注释不依赖弹注与锚点跳转",
        "表格保留原生 HTML",
        "全稿无 flex / transform / text-shadow / small-caps / 扩展 B 区生僻字",
        "短分隔线用画进透明 PNG 的图片线",
    ]:
        L.append("- [ ] %s" % item)
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description="微信读书 EPUB 本地预览 + 合规体检")
    ap.add_argument("book", help="EPUB 文件路径，或已解包的目录")
    ap.add_argument("--out", help="预览目录（默认 <epub同级>/<书名>.preview/）")
    ap.add_argument("--open", action="store_true", help="生成后自动用浏览器打开")
    ap.add_argument("--force", action="store_true", help="强制重新解包")
    ap.add_argument("--ui", choices=["light", "dark"], default="light", help="预览外壳配色")
    ap.add_argument("--annotate", dest="annotate", action="store_true", default=True,
                    help="预览内置「选中即批注」层（默认开；用 --no-annotate 关闭）")
    ap.add_argument("--no-annotate", dest="annotate", action="store_false",
                    help="预览不内置批注层（纯预览）")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--embed-img", dest="embed_img", action="store_true", default=True,
                    help="把图片内嵌成 data URI，让 index.html 单文件自包含（默认开；"
                         "预览面板只服务单个 HTML 时，相对路径图片会 404）")
    ap.add_argument("--no-embed-img", dest="embed_img", action="store_false",
                    help="不内嵌图片，保留 OEBPS/images/ 相对路径（file:// 直开时用）")
    ap.add_argument("--img-max", type=int, default=720,
                    help="内嵌图片最长边上限 px，超出等比缩小（默认 720；0=不缩放）")
    ap.add_argument("--version", action="version", version="weread_preview %s" % VERSION)
    args = ap.parse_args(argv)

    src = Path(args.book).expanduser().resolve()
    if not src.exists():
        raise SystemExit("找不到：%s" % src)

    if src.is_dir():
        root = src
        html_path = root / "weread-preview.html"
        report_path = root / "weread-report.md"
        if not (root / "META-INF").exists() and not list(root.glob("*.opf")):
            raise SystemExit("目录里没找到 EPUB 结构（META-INF / *.opf）：%s" % root)
    else:
        root = Path(args.out).expanduser().resolve() if args.out else (src.parent / (src.stem + ".preview"))
        root.mkdir(parents=True, exist_ok=True)
        unpack(src, root, force=args.force)
        html_path = root / "index.html"
        report_path = root / "weread-report.md"

    book = Book(root)
    rep = Report()

    # CSS
    css_parts = []
    for p in book.css_files():
        f = root / p
        if not f.exists():
            continue
        txt = f.read_text("utf-8", errors="ignore")
        css_parts.append("/* ==== %s ==== */\n" % p + txt)
        lint_css(p, txt, rep)

    # XHTML 内联 style / 结构
    chapters = []
    for s in book.spine:
        f = root / s["path"]
        if not f.exists():
            continue
        raw = f.read_text("utf-8", errors="ignore")
        lint_xhtml(s["path"], raw, rep, is_nav=(book.nav_path == s["path"]))
        for sm in HEAD_STYLE_RE.finditer(raw):
            lint_css("%s <style>" % s["path"], sm.group(1), rep)

    stats = {}
    lint_structure(book, rep, stats)

    # 组装预览用 CSS（body/html/:root -> .wr-body）
    cleaned = []
    cleaned_paths = []
    for part in css_parts:
        m = re.match(r"(/\* ==== .*? ==== \*/)\n(.*)$", part, re.S)
        header, body_css = (m.group(1), m.group(2)) if m else ("", part)
        path = header.replace("/* ==== ", "").replace(" ==== */", "") or "style.css"
        base_dir = posixpath.dirname(path)
        body_css = rewrite_css_urls(strip_comments(body_css), base_dir)
        out = []
        for kind, sel, block, so, bo in iter_rules(body_css):
            if kind == "rule":
                out.append("%s {%s}" % (rename_body(sel), block))
            elif kind == "at":
                out.append("%s {%s}" % (sel, block))
        cleaned.append("/* ==== %s ==== */\n" % path + "\n".join(out))
        cleaned_paths.append(path)
    cleaned = [remap_song_for_display(remap_kai_for_display(b)) for b in cleaned]
    engine_css = engine_serif_css()

    chapters = book.chapters()
    # 每章只挂载自己 <link> 的 CSS（修复：以前把 manifest 里全部 CSS 注入每一页，
    # 「整页背景」类独立 CSS 会在预览里全书误染，导致误判真机效果）
    css_index = {p: i for i, p in enumerate(cleaned_paths)}
    engine_idx = len(cleaned)
    for ch in chapters:
        idxs = [css_index[r] for r in ch.pop("cssRef", []) if r in css_index]
        if not idxs:
            idxs = list(range(len(cleaned)))       # 未 link 任何样式 -> 兜底全量（旧行为）
        idxs.append(engine_idx)                    # 引擎内置（官方类名模拟）恒挂
        ch["cssIdx"] = idxs
    css_blocks = cleaned + [engine_css]
    css_all = "\n".join(css_blocks)
    embed_stat = None
    if args.embed_img:
        embed_stat = embed_images(chapters, root, args.img_max)
    html = render(book, chapters, css_blocks, rep, stats, args.ui, annotate=args.annotate)
    html_path.write_text(html, "utf-8")

    md = render_markdown(book, rep, stats)
    report_path.write_text(md, "utf-8")
    (report_path.with_suffix(".json")).write_text(
        json.dumps({"meta": book.meta, "counts": rep.counts(), "groups": rep.groups(), "stats": stats},
                   ensure_ascii=False, indent=2), "utf-8")

    c = rep.counts()
    if not args.quiet:
        print("✅ 预览已生成")
        print("   书名    : %s" % (book.meta.get("title") or "未命名"))
        print("   预览页面: %s" % html_path)
        print("   体检报告: %s" % report_path)
        print("   体检结果: 死 %d 类 / 灰 %d 类 / 提示 %d 类 · spine %d 文件 · 图片 %d 张(%.1fMB)"
              % (c["dead"], c["gray"], c["info"], stats.get("spine_count", 0),
                 stats.get("img_count", 0), stats.get("img_bytes", 0) / 1048576))
        if embed_stat is not None:
            print("   图片内嵌: %d 处 / %d 个资源 → %.2fMB（HTML %.2fMB，最长边上限 %s）"
                  % (embed_stat["ok"], embed_stat["uniq"], embed_stat["out"] / 1048576,
                     html_path.stat().st_size / 1048576,
                     (args.img_max if args.img_max else "不限")))
            if embed_stat["miss"]:
                print("   ⚠ 有 %d 处图片读不到，已保留原相对路径：%s"
                      % (embed_stat["miss"], "、".join(embed_stat["misses"])))
        top = [r for r in rep.groups() if r["level"] != "info"][:6]
        if top:
            print("   Top 问题:")
            for r in top:
                print("     [%s] %s × %d → %s" % (r["id"], r["item"], r["count"], r["fix"]))

    if args.open:
        import webbrowser
        webbrowser.open(html_path.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
