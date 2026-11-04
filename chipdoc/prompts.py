"""prompt 构造：抽取 / 分节浓缩。与具体模型无关。"""
from __future__ import annotations

from .schema import FieldSpec

PROMPT_VERSION = "v4"

EXTRACT_SYSTEM = """你是芯片数据手册的信息抽取器。输入是某份 PDF 的连续片段，每页以 <<<p.N>>> 开头。
硬性规则：
1. 只抽取文本中明确写出的事实，不推断、不补全、不使用领域常识。
2. 每条事实必须给出 page 与 quote：page 必须是片段里真实出现的 <<<p.N>>> 中的 N；quote 必须是该页原文的逐字片段（10~80 字，可截断）。
3. 同一字段出现多个型号或多种条件时，选覆盖全系列的那个；必须带条件时，在 value 中保留条件限定。
4. 数值保留原文写法与单位（如 64K、3.3 V、-40 to 125 °C）。
5. 片段中没出现的字段不要输出。
6. 只输出 JSON，不要解释。"""

SUMMARY_SYSTEM = """你在为芯片手册写"可溯源的内容浓缩"。输入是某章节原文，每页以 <<<p.N>>> 开头。
要求：
1. 输出 2~6 条要点，每条不超过 40 个汉字（英文文档用一句话）。
2. 每条要点末尾用 [p.N] 标注依据页码，页码必须来自输入中真实出现的标记。
3. 只写原文明确写了的内容，不要写"该章节介绍了..."这类空话。
4. 只输出 JSON：{"bullets": ["... [p.12]", "... [p.13][p.14]"]}"""


def field_block(specs: tuple[FieldSpec, ...]) -> str:
    lines = []
    for s in specs:
        unit = f"，单位 {s.unit}" if s.unit and "单位" not in s.hint else ""
        lines.append(f"- {s.id}（{s.label}）：{s.hint}{unit}")
    return "\n".join(lines)


def extract_prompt(specs: tuple[FieldSpec, ...], chunk_text: str) -> str:
    return (f"字段清单：\n{field_block(specs)}\n\n"
            f"文档片段（页码以 <<<p.N>>> 标注）：\n{chunk_text}\n\n"
            '输出 JSON：{"facts":[{"field":"...","value":"...","unit":"...","page":12,"quote":"..."}]}')


def summary_prompt(title: str, block_text: str) -> str:
    return (f"章节：{title}\n\n{block_text}\n\n"
            '输出 JSON：{"bullets":["... [p.12]"]}')
