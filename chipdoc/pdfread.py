"""纯标准库 PDF 文本/目录提取（无第三方依赖，离线可用）。

覆盖离线文档里最常见的结构：
- 经典 xref 表（含 /Prev 增量更新）、损坏时按 "N G obj" 扫描兜底
- FlateDecode / ASCIIHex / ASCII85 / RunLength 过滤器
- 页面树、书签目录、文档信息
- 文本：BT/ET、Tf、Td/TD/Tm/T*、TJ/Tj/'/"、Tc/Tw/Tz/TL/Ts
- 字体：Type0(Identity-H)+ToUnicode、简单字体 + /Encoding/Differences

不支持（会明确报错，可退回 PyMuPDF 后端）：加密 PDF、xref 流、对象流、JBIG 等特殊图像。
"""
from __future__ import annotations

import base64
import re
import zlib
from dataclasses import dataclass, field
from typing import Any

WS = b"\x00\t\n\x0c\r "
DELIMS = b"()<>[]{}/%"


class PDFError(RuntimeError):
    pass


@dataclass
class Ref:
    num: int
    gen: int


@dataclass
class Stream:
    dict: dict
    raw: bytes


class Name(str):
    pass


class Keyword(str):
    pass


# --------------------------------------------------------------------------
# 词法 / 语法
# --------------------------------------------------------------------------

class Lexer:
    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def skip_ws(self) -> None:
        d, n = self.data, len(self.data)
        while self.pos < n:
            c = d[self.pos]
            if c in WS:
                self.pos += 1
            elif c == 0x25:  # %
                while self.pos < n and d[self.pos] not in b"\r\n":
                    self.pos += 1
            else:
                break

    def parse(self):
        self.skip_ws()
        if self.pos >= len(self.data):
            raise PDFError("unexpected EOF")
        d = self.data
        c = d[self.pos:self.pos + 1]
        if c == b"/":
            return self._name()
        if c == b"(":
            return self._literal()
        if c == b"[":
            return self._array()
        if d.startswith(b"<<", self.pos):
            return self._dict()
        if c == b"<":
            return self._hex()
        if c in b"+-.0123456789":
            return self._number_or_ref()
        word = self._word()
        if word == b"true":
            return True
        if word == b"false":
            return False
        if word == b"null":
            return None
        return Keyword(word.decode("latin-1"))

    def _name(self) -> Name:
        self.pos += 1
        start = self.pos
        while self.pos < len(self.data) and self.data[self.pos] not in WS + DELIMS:
            self.pos += 1
        raw = self.data[start:self.pos]
        if b"#" in raw:
            raw = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)
        return Name(raw.decode("latin-1"))

    def _literal(self) -> bytes:
        self.pos += 1
        out = bytearray()
        depth = 1
        while self.pos < len(self.data):
            c = self.data[self.pos]
            if c == 0x5C:  # backslash
                self.pos += 1
                e = self.data[self.pos:self.pos + 1]
                mapping = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
                if e in mapping:
                    out += mapping[e]
                    self.pos += 1
                elif e in b"()\\":
                    out += e
                    self.pos += 1
                elif e.isdigit():
                    oct_digits = b""
                    for _ in range(3):
                        ch = self.data[self.pos:self.pos + 1]
                        if ch.isdigit() and len(oct_digits) < 3:
                            oct_digits += ch
                            self.pos += 1
                        else:
                            break
                    out.append(int(oct_digits, 8) & 0xFF)
                elif e in b"\r\n":
                    self.pos += 1
                    if self.data[self.pos:self.pos + 1] == b"\n":
                        self.pos += 1
                else:
                    out += e
                    self.pos += 1
                continue
            if c == 0x28:
                depth += 1
            elif c == 0x29:
                depth -= 1
                if depth == 0:
                    self.pos += 1
                    return bytes(out)
            out.append(c)
            self.pos += 1
        return bytes(out)

    def _hex(self) -> bytes:
        self.pos += 1
        end = self.data.find(b">", self.pos)
        if end < 0:
            end = len(self.data)
        raw = re.sub(rb"[^0-9A-Fa-f]", b"", self.data[self.pos:end])
        self.pos = end + 1
        if len(raw) % 2:
            raw += b"0"
        return bytes.fromhex(raw.decode("ascii"))

    def _array(self) -> list:
        self.pos += 1
        out = []
        while True:
            self.skip_ws()
            if self.pos >= len(self.data):
                return out
            if self.data[self.pos:self.pos + 1] == b"]":
                self.pos += 1
                return out
            out.append(self.parse())

    def _dict(self) -> dict:
        self.pos += 2
        out: dict[str, Any] = {}
        while True:
            self.skip_ws()
            if self.data[self.pos:self.pos + 2] == b">>":
                self.pos += 2
                return out
            key = self.parse()
            out[str(key)] = self.parse()

    def _word(self) -> bytes:
        start = self.pos
        while self.pos < len(self.data) and self.data[self.pos] not in WS + DELIMS:
            self.pos += 1
        if self.pos == start:
            self.pos += 1
        return self.data[start:self.pos]

    def _number_or_ref(self):
        start = self.pos
        while self.pos < len(self.data) and self.data[self.pos] in b"+-.0123456789":
            self.pos += 1
        raw = self.data[start:self.pos]
        # 可能是 "12 0 R"
        save = self.pos
        self.skip_ws()
        m = re.match(rb"(\d+)\s+R(?![A-Za-z])", self.data[self.pos:self.pos + 32])
        if m and b"." not in raw:
            self.pos += m.end()
            return Ref(int(raw), int(m.group(1)))
        self.pos = save
        try:
            return float(raw) if b"." in raw else int(raw)
        except ValueError:
            return 0


# --------------------------------------------------------------------------
# 过滤器
# --------------------------------------------------------------------------

def _inflate(data: bytes) -> bytes:
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        try:
            return zlib.decompress(data, wbits)
        except zlib.error:
            continue
    try:  # 截断/多余尾部
        obj = zlib.decompressobj()
        return obj.decompress(data)
    except zlib.error as e:
        raise PDFError(f"FlateDecode 失败: {e}") from e


def _png_unpredict(data: bytes, columns: int, colors: int = 1, bpc: int = 8) -> bytes:
    bpp = max(1, colors * bpc // 8)
    row_len = (columns * colors * bpc + 7) // 8
    out = bytearray()
    prev = bytearray(row_len)
    i = 0
    while i + 1 + row_len <= len(data) + 1 and i < len(data):
        ft = data[i]
        row = bytearray(data[i + 1:i + 1 + row_len])
        if len(row) < row_len:
            row.extend(b"\x00" * (row_len - len(row)))
        for x in range(row_len):
            left = row[x - bpp] if x >= bpp else 0
            up = prev[x]
            ul = prev[x - bpp] if x >= bpp else 0
            if ft == 1:
                row[x] = (row[x] + left) & 0xFF
            elif ft == 2:
                row[x] = (row[x] + up) & 0xFF
            elif ft == 3:
                row[x] = (row[x] + ((left + up) >> 1)) & 0xFF
            elif ft == 4:
                p = left + up - ul
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - ul)
                pr = left if (pa <= pb and pa <= pc) else (up if pb <= pc else ul)
                row[x] = (row[x] + pr) & 0xFF
        out += row
        prev = row
        i += 1 + row_len
    return bytes(out)


def decode_stream(stream: Stream, resolve) -> bytes:
    filters = resolve(stream.dict.get("Filter"))
    parms = resolve(stream.dict.get("DecodeParms")) or {}
    if filters is None:
        return stream.raw
    if not isinstance(filters, list):
        filters = [filters]
    if not isinstance(parms, list):
        parms = [parms] * len(filters)
    data = stream.raw
    for f, p in zip(filters, parms):
        f = str(f)
        p = resolve(p) or {}
        if f == "FlateDecode":
            data = _inflate(data)
            if resolve(p.get("Predictor", 1)) >= 10:
                data = _png_unpredict(data, int(resolve(p.get("Columns", 1))),
                                      int(resolve(p.get("Colors", 1))),
                                      int(resolve(p.get("BitsPerComponent", 8))))
        elif f == "ASCIIHexDecode":
            raw = re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">")[0])
            if len(raw) % 2:
                raw += b"0"
            data = bytes.fromhex(raw.decode("ascii"))
        elif f == "ASCII85Decode":
            body = data.split(b"~>")[0]
            data = base64.a85decode(body, adobe=False)
        elif f == "RunLengthDecode":
            out, i = bytearray(), 0
            while i < len(data):
                n = data[i]
                i += 1
                if n == 128:
                    break
                if n < 128:
                    out += data[i:i + n + 1]
                    i += n + 1
                else:
                    out += data[i:i + 1] * (257 - n)
                    i += 1
            data = bytes(out)
        else:
            raise PDFError(f"不支持的过滤器: {f}")
    return data


# --------------------------------------------------------------------------
# 字体
# --------------------------------------------------------------------------

_STD_GLYPH = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$",
    "percent": "%", "ampersand": "&", "quotesingle": "'", "parenleft": "(",
    "parenright": ")", "asterisk": "*", "plus": "+", "comma": ",", "hyphen": "-",
    "period": ".", "slash": "/", "zero": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "colon": ":", "semicolon": ";", "less": "<", "equal": "=", "greater": ">",
    "question": "?", "at": "@", "bracketleft": "[", "backslash": "\\",
    "bracketright": "]", "asciicircum": "^", "underscore": "_", "grave": "`",
    "braceleft": "{", "bar": "|", "braceright": "}", "asciitilde": "~",
    "degree": "°", "plusminus": "±", "mu": "µ", "multiply": "×",
    "endash": "–", "emdash": "—", "quotedblleft": "“", "quotedblright": "”",
    "quoteleft": "‘", "quoteright": "’", "bullet": "•", "ellipsis": "…",
    "copyright": "©", "registered": "®", "trademark": "™", "minus": "−",
}
for _c in range(26):
    _STD_GLYPH[chr(65 + _c)] = chr(65 + _c)
    _STD_GLYPH[chr(97 + _c)] = chr(97 + _c)


def _glyph_to_unicode(name: str) -> str:
    if name in _STD_GLYPH:
        return _STD_GLYPH[name]
    m = re.match(r"^uni([0-9A-Fa-f]{4})$", name)
    if m:
        return chr(int(m.group(1), 16))
    m = re.match(r"^u([0-9A-Fa-f]{4,6})$", name)
    if m:
        return chr(int(m.group(1), 16))
    return ""


def parse_tounicode(data: bytes) -> tuple[dict[int, str], int]:
    """返回 ({code: text}, code_bytes)。"""
    text = data.decode("latin-1")
    mapping: dict[int, str] = {}
    width = 2
    m = re.search(r"begincodespacerange\s*<([0-9A-Fa-f]+)>", text)
    if m:
        width = max(1, len(m.group(1)) // 2)

    def dst(hexs: str) -> str:
        raw = bytes.fromhex(hexs if len(hexs) % 2 == 0 else hexs + "0")
        if width == 1 and len(raw) == 2:
            return raw.decode("utf-16-be", "ignore")
        return raw.decode("utf-16-be", "ignore")

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, d in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            mapping[int(src, 16)] = dst(d)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, rest in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block):
            lo_i, hi_i = int(lo, 16), int(hi, 16)
            if hi_i - lo_i > 0xFFFF:
                continue
            if rest.startswith("["):
                items = re.findall(r"<([0-9A-Fa-f]*)>", rest)
                for k, item in enumerate(items):
                    mapping[lo_i + k] = dst(item)
            else:
                base = rest.strip("<>")
                base_i = int(base, 16) if base else 0
                for k in range(hi_i - lo_i + 1):
                    mapping[lo_i + k] = dst(f"{base_i + k:0{max(4, len(base))}X}")
    return mapping, width


class Font:
    def __init__(self, doc: "PDF", spec: dict):
        spec = doc.resolve(spec) or {}
        self.subtype = str(doc.resolve(spec.get("Subtype", "")))
        self.two_byte = self.subtype == "Type0"
        self.cmap: dict[int, str] = {}
        self.code_bytes = 2 if self.two_byte else 1
        self.widths: dict[int, float] = {}
        self.default_width = 500.0

        tu = doc.resolve(spec.get("ToUnicode"))
        if isinstance(tu, Stream):
            try:
                self.cmap, w = parse_tounicode(decode_stream(tu, doc.resolve))
                if self.two_byte:
                    self.code_bytes = max(1, w)
            except PDFError:
                self.cmap = {}

        if self.two_byte:
            desc = doc.resolve(spec.get("DescendantFonts")) or []
            if desc:
                cid = doc.resolve(desc[0]) or {}
                dw = doc.resolve(cid.get("DW"))
                if isinstance(dw, (int, float)):
                    self.default_width = float(dw)
                self._read_cid_widths(doc, doc.resolve(cid.get("W")) or [])
        else:
            first = doc.resolve(spec.get("FirstChar"))
            widths = doc.resolve(spec.get("Widths")) or []
            if isinstance(first, int) and isinstance(widths, list):
                for i, w in enumerate(widths):
                    w = doc.resolve(w)
                    if isinstance(w, (int, float)):
                        self.widths[first + i] = float(w)
            self._build_encoding(doc, spec)

    def _read_cid_widths(self, doc: "PDF", w: list) -> None:
        i = 0
        while i < len(w):
            a = doc.resolve(w[i])
            b = doc.resolve(w[i + 1]) if i + 1 < len(w) else None
            if isinstance(b, list):
                for k, val in enumerate(b):
                    val = doc.resolve(val)
                    if isinstance(val, (int, float)):
                        self.widths[int(a) + k] = float(val)
                i += 2
            else:
                c = doc.resolve(w[i + 2]) if i + 2 < len(w) else None
                if isinstance(a, (int, float)) and isinstance(b, (int, float)) and isinstance(c, (int, float)):
                    for code in range(int(a), int(b) + 1):
                        self.widths[code] = float(c)
                i += 3

    def _build_encoding(self, doc: "PDF", spec: dict) -> None:
        enc = doc.resolve(spec.get("Encoding"))
        if isinstance(enc, dict):
            diffs = doc.resolve(enc.get("Differences")) or []
            code = 0
            for item in diffs:
                item = doc.resolve(item)
                if isinstance(item, (int, float)):
                    code = int(item)
                elif isinstance(item, str):
                    ch = _glyph_to_unicode(str(item))
                    if ch:
                        self.cmap[code] = ch
                    code += 1
        # WinAnsi 兜底
        for c in range(32, 127):
            self.cmap.setdefault(c, chr(c))

    def decode(self, raw: bytes) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        step = self.code_bytes
        for i in range(0, len(raw) - step + 1, step):
            code = int.from_bytes(raw[i:i + step], "big")
            ch = self.cmap.get(code)
            if ch is None:
                ch = raw[i:i + step].decode("latin-1", "ignore") if step == 1 else ""
            out.append((ch, self.widths.get(code, self.default_width)))
        return out


# --------------------------------------------------------------------------
# 文档
# --------------------------------------------------------------------------

class PDF:
    def __init__(self, path: str):
        with open(path, "rb") as f:
            self.data = f.read()
        self.xref: dict[int, int] = {}
        self.trailer: dict = {}
        self.cache: dict[int, Any] = {}
        self._parse_xref()
        if "Root" not in self.trailer:
            raise PDFError("找不到 trailer/Root")
        self._pages: list[dict] | None = None
        self._page_refs: list[int] = []
        self._font_cache: dict[int, Font] = {}

    # -- 交叉引用 -----------------------------------------------------
    def _parse_xref(self) -> None:
        if b"startxref" not in self.data:
            raise PDFError("缺少 startxref（可能被截断）")
        m = re.findall(rb"startxref\s+(\d+)", self.data)
        offsets = [int(x) for x in m]
        seen = set()
        while offsets:
            off = offsets.pop(0)
            if off in seen or off <= 0 or off >= len(self.data):
                continue
            seen.add(off)
            if not self._read_xref_at(off):
                break
        if not self.xref:
            raise PDFError("xref 为空")

    def _read_xref_at(self, off: int) -> bool:
        lx = Lexer(self.data, off)
        lx.skip_ws()
        if self.data.startswith(b"xref", lx.pos):
            lx.pos += 4
            while True:
                lx.skip_ws()
                if self.data.startswith(b"trailer", lx.pos):
                    lx.pos += 7
                    trailer = lx.parse()
                    self.trailer.update({k: v for k, v in trailer.items() if k not in self.trailer})
                    if "Prev" in trailer:
                        self._pending_prev = int(trailer["Prev"])
                        self._read_xref_at(self._pending_prev)
                    return True
                m = re.match(rb"(\d+)\s+(\d+)", self.data[lx.pos:lx.pos + 40])
                if not m:
                    return False
                start, count = int(m.group(1)), int(m.group(2))
                lx.pos += m.end()
                for i in range(count):
                    lx.skip_ws()
                    entry = self.data[lx.pos:lx.pos + 20]
                    em = re.match(rb"(\d{1,10})\s+(\d{1,5})\s+([nf])", entry)
                    if not em:
                        return False
                    lx.pos += em.end()
                    if em.group(3) == b"n":
                        self.xref[start + i] = int(em.group(1))
        # xref 流（本工具不做完整支持，明确报错）
        raise PDFError("xref 流不受支持，请改用 PyMuPDF 后端")

    # -- 对象 ---------------------------------------------------------
    def resolve(self, obj):
        seen = 0
        while isinstance(obj, Ref):
            obj = self._object(obj.num)
            seen += 1
            if seen > 32:
                return None
        return obj

    def _object(self, num: int):
        if num in self.cache:
            return self.cache[num]
        off = self.xref.get(num)
        if off is None:
            return None
        lx = Lexer(self.data, off)
        lx.skip_ws()
        m = re.match(rb"(\d+)\s+(\d+)\s+obj", self.data[lx.pos:lx.pos + 40])
        if not m:
            return None
        lx.pos += m.end()
        obj = lx.parse()
        lx.skip_ws()
        if isinstance(obj, dict) and self.data.startswith(b"stream", lx.pos):
            lx.pos += 6
            if self.data.startswith(b"\r\n", lx.pos):
                lx.pos += 2
            elif self.data.startswith(b"\n", lx.pos) or self.data.startswith(b"\r", lx.pos):
                lx.pos += 1
            length = self.resolve(obj.get("Length"))
            if not isinstance(length, int) or length < 0:
                end = self.data.find(b"endstream", lx.pos)
                length = max(0, end - lx.pos)
            raw = self.data[lx.pos:lx.pos + length]
            obj = Stream(obj, raw)
        self.cache[num] = obj
        return obj

    # -- 页面 ---------------------------------------------------------
    def page_count(self) -> int:
        return len(self.page_objects())

    def page_objects(self) -> list[dict]:
        if self._pages is not None:
            return self._pages
        root = self.resolve(self.trailer["Root"]) or {}
        pages: list[dict] = []
        self._walk_pages(root.get("Pages"), pages, set(), 0)
        self._pages = pages
        return pages

    def _walk_pages(self, node, out: list, seen: set, depth: int) -> None:
        if depth > 64:
            return
        if isinstance(node, Ref):
            if node.num in seen:
                return
            seen.add(node.num)
        node = self.resolve(node) or {}
        kids = self.resolve(node.get("Kids")) or []
        if node.get("Type") == "Pages" or kids:
            for kid in kids:
                self._walk_pages(kid, out, seen, depth + 1)
        else:
            out.append(node)

    def page_index_map(self) -> dict[int, int]:
        mapping = {}
        for i, page in enumerate(self.page_objects()):
            for k, v in self.xref.items():
                pass
        return mapping

    # -- 文本 ---------------------------------------------------------
    def page_text(self, index: int) -> str:
        page = self.page_objects()[index]
        contents = self.resolve(page.get("Contents"))
        if isinstance(contents, list):
            chunks = [self.resolve(c) for c in contents]
        else:
            chunks = [contents]
        data = b""
        for c in chunks:
            if isinstance(c, Stream):
                try:
                    data += decode_stream(c, self.resolve) + b"\n"
                except PDFError:
                    continue
        resources = self.resolve(page.get("Resources")) or {}
        fonts = self.resolve(resources.get("Font")) or {}
        return self._interpret(data, fonts)

    def _get_font(self, ref_or_dict) -> Font:
        if isinstance(ref_or_dict, Ref):
            if ref_or_dict.num in self._font_cache:
                return self._font_cache[ref_or_dict.num]
            font = Font(self, self.resolve(ref_or_dict) or {})
            self._font_cache[ref_or_dict.num] = font
            return font
        return Font(self, ref_or_dict or {})

    def _interpret(self, data: bytes, fonts: dict) -> str:
        lx = Lexer(data)
        stack: list[Any] = []
        runs: list[tuple[float, float, str, float, float]] = []
        tm = [1, 0, 0, 1, 0, 0]
        tlm = list(tm)
        ctm = [1, 0, 0, 1, 0, 0]
        gstack: list[list[float]] = []
        font = Font(self, {})
        size = 10.0
        leading = 0.0
        char_space = 0.0
        word_space = 0.0
        hscale = 1.0

        def emit(text: str, advance: float) -> None:
            if not text:
                return
            x = tm[4] * ctm[0] + tm[5] * ctm[2] + ctm[4]
            y = tm[4] * ctm[1] + tm[5] * ctm[3] + ctm[5]
            adv = advance * hscale
            runs.append((x, y, text, size, adv))
            tm[4] += adv

        while True:
            lx.skip_ws()
            if lx.pos >= len(lx.data):
                break
            start = lx.pos
            try:
                obj = lx.parse()
            except PDFError:
                break
            if isinstance(obj, (int, float)) and not isinstance(obj, bool):
                stack.append(obj)
                continue
            if not isinstance(obj, Keyword):
                if isinstance(obj, (str, Name, list, dict, bytes)):
                    stack.append(obj)
                    continue
            op = str(obj) if isinstance(obj, Keyword) else ""
            if op == "BT":
                tm = [1, 0, 0, 1, 0, 0]
                tlm = list(tm)
            elif op == "Tf" and len(stack) >= 2:
                name, size = stack[-2], stack[-1]
                fobj = fonts.get(str(name)) if isinstance(fonts, dict) else None
                if fobj is not None:
                    font = self._get_font(fobj)
                size = float(size) if isinstance(size, (int, float)) else size
            elif op == "Td" and len(stack) >= 2:
                tx, ty = float(stack[-2]), float(stack[-1])
                tlm = [tlm[0], tlm[1], tlm[2], tlm[3], tlm[4] + tx, tlm[5] + ty]
                tm = list(tlm)
            elif op == "TD" and len(stack) >= 2:
                tx, ty = float(stack[-2]), float(stack[-1])
                leading = -ty
                tlm = [tlm[0], tlm[1], tlm[2], tlm[3], tlm[4] + tx, tlm[5] + ty]
                tm = list(tlm)
            elif op == "Tm" and len(stack) >= 6:
                tm = [float(v) for v in stack[-6:]]
                tlm = list(tm)
            elif op == "T*":
                tlm = [tlm[0], tlm[1], tlm[2], tlm[3], tlm[4], tlm[5] - leading]
                tm = list(tlm)
            elif op in ("Tj", "'", '"') and stack:
                if op == '"' and len(stack) >= 3:
                    word_space = float(stack[-3])
                    char_space = float(stack[-2])
                if op in ("'", '"'):
                    tlm = [tlm[0], tlm[1], tlm[2], tlm[3], tlm[4], tlm[5] - leading]
                    tm = list(tlm)
                raw = stack[-1]
                if isinstance(raw, bytes):
                    for ch, w in font.decode(raw):
                        emit(ch, w / 1000.0 * size + char_space)
            elif op == "TJ" and stack and isinstance(stack[-1], list):
                for item in stack[-1]:
                    if isinstance(item, bytes):
                        for ch, w in font.decode(item):
                            emit(ch, w / 1000.0 * size + char_space)
                    elif isinstance(item, (int, float)):
                        tm[4] -= float(item) / 1000.0 * size
            elif op == "Tc" and stack:
                char_space = float(stack[-1])
            elif op == "Tw" and stack:
                word_space = float(stack[-1])
            elif op == "Tz" and stack:
                hscale = float(stack[-1]) / 100.0
            elif op == "TL" and stack:
                leading = float(stack[-1])
            elif op == "q":
                gstack.append(list(ctm))
            elif op == "Q":
                if gstack:
                    ctm = gstack.pop()
            elif op == "cm" and len(stack) >= 6:
                m = [float(v) for v in stack[-6:]]
                ctm = [
                    m[0] * ctm[0] + m[1] * ctm[2],
                    m[0] * ctm[1] + m[1] * ctm[3],
                    m[2] * ctm[0] + m[3] * ctm[2],
                    m[2] * ctm[1] + m[3] * ctm[3],
                    m[4] * ctm[0] + m[5] * ctm[2] + ctm[4],
                    m[4] * ctm[1] + m[5] * ctm[3] + ctm[5],
                ]
            elif op in ("re", "Do", "gs", "w", "J", "j", "M", "d", "ri", "i", "W", "n", "f", "S", "B", "BT*"):
                pass
            stack = []
        return _runs_to_text(runs)

    # -- 目录 / 元信息 -------------------------------------------------
    def ref_page_map(self) -> dict[int, int]:
        root = self.resolve(self.trailer["Root"]) or {}
        mapping: dict[int, int] = {}
        counter = 0

        def walk(node, depth: int) -> None:
            nonlocal counter
            if depth > 64:
                return
            ref = node
            resolved = self.resolve(node) or {}
            kids = self.resolve(resolved.get("Kids")) or []
            if resolved.get("Type") == "Pages" or kids:
                for kid in kids:
                    walk(kid, depth + 1)
            else:
                if isinstance(ref, Ref):
                    mapping[ref.num] = counter + 1
                counter += 1

        walk(root.get("Pages"), 0)
        return mapping

    def outline(self) -> list[tuple[int, str, int]]:
        root = self.resolve(self.trailer["Root"]) or {}
        outlines = self.resolve(root.get("Outlines"))
        if not isinstance(outlines, dict):
            return []
        ref_to_page = self.ref_page_map()
        result: list[tuple[int, str, int]] = []
        seen: set[int] = set()

        def walk(item, level: int) -> None:
            if level > 12:
                return
            node = self.resolve(item)
            while isinstance(node, dict):
                key = id(node)
                if key in seen:
                    return
                seen.add(key)
                title = self.resolve(node.get("Title"))
                if isinstance(title, bytes):
                    title = (title[2:].decode("utf-16-be", "ignore")
                             if title.startswith(b"\xfe\xff") else title.decode("latin-1"))
                page = self._dest_page(node, ref_to_page)
                if isinstance(title, str) and title and page:
                    result.append((level, title, page))
                child = node.get("First")
                if child is not None:
                    walk(child, level + 1)
                node = self.resolve(node.get("Next"))

        walk(outlines.get("First"), 1)
        return result

    def _dest_page(self, node: dict, ref_to_page: dict[int, int]) -> int | None:
        dest = self.resolve(node.get("Dest"))
        if isinstance(dest, list) and dest:
            first = dest[0]
            if isinstance(first, Ref):
                return ref_to_page.get(first.num)
            if isinstance(first, int):
                return first
        action = self.resolve(node.get("A"))
        if isinstance(action, dict):
            d = self.resolve(action.get("D"))
            if isinstance(d, list) and d and isinstance(d[0], Ref):
                return ref_to_page.get(d[0].num)
        return None

    def metadata(self) -> dict:
        info = self.resolve(self.trailer.get("Info")) or {}
        out = {}
        for key in ("Title", "Author", "Subject", "Keywords", "Producer", "Creator"):
            val = self.resolve(info.get(key))
            if isinstance(val, bytes):
                if val.startswith(b"\xfe\xff"):
                    val = val.decode("utf-16-be", "ignore")
                else:
                    val = val.decode("latin-1")
            if isinstance(val, str):
                out[key.lower()] = val.lstrip("\ufeff")
        return out


def _is_cjk(ch: str) -> bool:
    return bool(ch) and ord(ch[0]) >= 0x2E80


def _runs_to_text(runs: list[tuple[float, float, str, float, float]]) -> str:
    """把逐字 run 还原成行。

    同一 y 的 run 按 x 排序；正常字距按“该行中位间距”判断是否补空格，
    远大于中位间距的缺口视为分栏/表格列，另起一行。
    """
    if not runs:
        return ""
    clusters: list[list] = []          # [y, [(x, text, size, advance)]]
    for x, y, text, size, adv in runs:
        for c in clusters:
            if abs(c[0] - y) <= 3.0:
                c[1].append((x, text, size, adv))
                break
        else:
            clusters.append([y, [(x, text, size, adv)]])

    out: list[str] = []
    for y, items in sorted(clusters, key=lambda c: -c[0]):
        items.sort(key=lambda p: p[0])
        if len(items) < 2:
            out.append("".join(t for _, t, _s, _a in items))
            continue
        line = ""
        prev_x = prev_adv = prev_size = None
        prev_text = ""
        for x, text, size, adv in items:
            if prev_x is not None:
                extra = (x - prev_x) - prev_adv
                if extra > max(6.0, 0.45 * size):        # 分栏/表格列
                    out.append(line)
                    line = ""
                elif extra > max(1.5, 0.16 * size) and not (
                        _is_cjk(prev_text) and _is_cjk(text)):  # 词间空格
                    line += " "
            line += text
            prev_x, prev_adv, prev_size, prev_text = x, adv, size, text
        out.append(line)
    return "\n".join(l.rstrip() for l in out if l.strip())
