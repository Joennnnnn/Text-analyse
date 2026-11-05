"""规则引擎：不需要模型，用于快速基线 / 断网 / 交叉核对。

芯片手册的表在 PDF 文本层里是"竖着"展开的（标签一行、值在下面几行），
所以这里用 label-walk：命中标签行后，往下找第一个符合取值模式的行。
"""
from __future__ import annotations

import re

from .parsing import DocIndex
from .verify import Fact, verify_facts


# --------------------------------------------------------------------------
# 规则引擎：不需要 API key，用于快速基线 / 断网 / 交叉核对
# 芯片手册的表在 PDF 文本层里是"竖着"展开的（标签一行、值在下面几行），
# 所以这里用 label-walk：命中标签行后，往下找第一个符合取值模式的行。
# --------------------------------------------------------------------------

CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
          "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "eight": 8}

_NUM = r"(\d+|one|two|three|four|five|six|eight|一|二|两|三|四|五|六)"

COUNT_PATTERNS: dict[str, re.Pattern] = {
    "can": re.compile(r"^[\s•\-–—]*" + _NUM + r"\s*(?:个|条|路)?\s*.{0,40}?(?:eCAN|CAN)\b", re.I),
    "uart": re.compile(r"^[\s•\-–—]*" + _NUM + r"\s*(?:个|条|路)?\s*.{0,50}?(?:串行通信接口|SCI|UART)", re.I),
    "spi": re.compile(r"^[\s•\-–—]*" + _NUM + r"\s*(?:个|条|路)?\s*.{0,50}?(?:串行外设接口|SPI)\b", re.I),
    "i2c": re.compile(r"^[\s•\-–—]*" + _NUM + r"\s*(?:个|条|路)?\s*.{0,50}?(?:内部集成电路|I2C|I²C|Inter-integrated)", re.I),
}


def _num(tok: str) -> str:
    return str(CN_NUM.get(tok.lower(), tok))


def _walk(pages: list[str], label: re.Pattern, value: re.Pattern,
          max_skip: int = 10) -> list[tuple[int, str, str]]:
    """标签行 → 往下找值。返回 (page, value, quote)。"""
    hits = []
    for pno, text in enumerate(pages, start=1):
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if len(ln) > 140 or not label.search(ln):
                continue
            for j in range(i, min(i + max_skip, len(lines))):
                m = value.search(lines[j])
                if m:
                    quote = (ln if j == i else ln + " … " + lines[j])[:140]
                    hits.append((pno, m.group(0).strip(), quote))
                    break
    return hits


def _first_per_field(hits: list[tuple[int, str, str]], fid: str) -> Fact | None:
    if not hits:
        return None
    page, value, quote = hits[0]
    return Fact(field=fid, value=value, page=page, quote=quote, source="rules")


def _rule_scan(index: DocIndex) -> list[Fact]:
    pages = index.pages
    # 概览区：datasheet 的 Features / Device Comparison 通常在前 12 页，
    # 先在这里找，找不到再全篇找，避免后面章节的偶然命中。
    ov = pages[:12]
    out: list[Fact] = []

    # 型号 / 内核 / 主频：从首页与特性页取，主频取该范围内最大值
    m = re.search(r"(TMS320[A-Za-z0-9]{3,}|STM32[A-Za-z0-9]{3,})", pages[0]) if pages else None
    if m:
        out.append(Fact("device", m.group(1), 1, pages[0].splitlines()[0][:100], source="rules"))
    core = _walk(pages[:6], re.compile(r"C28x|Cortex-M|ARM Cortex|CPU", re.I),
                 re.compile(r"(C28x|Cortex-M\d\+?|ARM Cortex-M\d\+?)"))
    if core:
        out.append(_first_per_field(core, "core"))

    best = (0, 0, "")
    for pno in range(1, min(4, len(pages) + 1)):
        for ln in pages[pno - 1].splitlines():
            for mm in re.finditer(r"(\d{1,4}(?:\.\d+)?)\s*MHz", ln):
                v = float(mm.group(1))
                if 1 <= v <= 500 and v > best[0]:
                    best = (v, pno, ln.strip()[:100])
    if best[0]:
        out.append(Fact("max_freq", f"{best[0]:g} MHz", best[1], best[2], source="rules"))

    flash = _walk(ov, re.compile(r"on-?chip\s+flash|闪存|Flash\s+memory|^\s*flash\s*$|程序存储器", re.I),
                  re.compile(r"(\d{1,4})\s*(K|M)B?"))
    if flash:
        out.append(_first_per_field(flash, "flash"))

    ram = _walk(ov, re.compile(r"SARAM|on-?chip\s+RAM|\bRAM\b|随机存储器", re.I),
                re.compile(r"(\d{1,4})\s*(K|M)\b"))
    if ram:
        out.append(_first_per_field(ram, "ram"))

    for pno, text in enumerate(ov, start=1):
        m = re.search(r"(\d{1,4})\s*(K|M)B\s*(?:\([^)]{0,20}\))?\s*(?:of\s+)?(?:on-chip\s+)?(?:S?RAM)", text, re.I)
        if m:
            out.append(Fact("ram", f"{m.group(1)}{m.group(2)}B", pno,
                            m.group(0)[:100], source="rules"))
            break
        m = re.search(r"(\d{1,4})\s*(K|M)B\s*(?:of\s+)?(?:on-chip\s+)?[Ff]lash", text, re.I)
        if m:
            out.append(Fact("flash", f"{m.group(1)}{m.group(2)}B", pno,
                            m.group(0)[:100], source="rules"))
            break

    pwm = _walk(ov, re.compile(r"ePWM\s*(channels?|通道)|PWM\s*(channels?|通道)", re.I),
                re.compile(r"\b(\d{1,3})\b"))
    if pwm:
        out.append(_first_per_field(pwm, "pwm"))

    pkg = _walk(ov, re.compile(r"package\s*type|封装", re.I),
                re.compile(r"\d{1,3}[- ]pin\s+[A-Z]{2}\s*(?:LQFP|TQFP|QFP|BGA|QFN)?"))
    if pkg:
        page, value, quote = pkg[0]
        out.append(Fact("package", value, page, quote, source="rules"))

    for pno, text in enumerate(ov, start=1):
        if not text:
            continue
        for line in text.splitlines():
            flat = line.strip()
            if not flat or len(flat) > 200:
                continue
        # 模拟/电源/温度：单行内即可判定
            if re.search(r"12[- ]?[Bb]it\s*(?:SAR\s*)?ADC|12\s*位\s*ADC", flat):
                out.append(Fact("adc", "12-bit ADC", pno, flat[:120], source="rules"))
            if re.search(r"3\.3\s*V", flat) and not re.search(r"3\.63|4\.6", flat):
                out.append(Fact("supply", "3.3 V", pno, flat[:120], source="rules"))
            m = re.search(r"[-\u2013]\s*40[^\n]{0,20}?(105|125|150)\s*(?:°C|℃|C\b)", flat)
            if m:
                out.append(Fact("temp", f"-40 to {m.group(1)} °C", pno, flat[:120], source="rules"))
            if "Type " in flat or "compatible" in flat.lower():
                continue
            for fid, rx in COUNT_PATTERNS.items():
                mm = rx.search(flat)
                if mm and len(flat) < 160:
                    out.append(Fact(fid, _num(mm.group(1)), pno, flat[:120], source="rules"))
    return out


def extract_rules(index: DocIndex) -> tuple[list[Fact], list[Fact]]:
    raw = _rule_scan(index)
    kept, dropped = verify_facts(raw, index)
    picked: dict[str, Fact] = {}
    for f in kept:
        if f.field not in picked:
            picked[f.field] = f
    rest = [f for f in kept if picked.get(f.field) is not f]
    return list(picked.values()), dropped + rest
