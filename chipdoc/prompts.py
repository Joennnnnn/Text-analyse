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


BATCH_RULES = """你会收到若干互不相关的文本块，每块以 "## 块 cNNN（第 X-Y 页）" 开头。
对每个块，只依据该块原文抽取事实，输出一个 JSON（不要解释、不要 markdown 代码围栏）：
{"facts":[{"cid":"c000","field":"字段id","value":"原文值","unit":"单位或空","page":12,"quote":"该页逐字片段"}]}
规则：
- cid 必须等于块的编号；page 必须来自该块内 <<<p.N>>> 标记。
- quote 必须是该页原文的逐字片段（10~80 字），不要改写。
- 块里没有的字段不要输出；拿不准的宁可不写。
- 同一字段在多个块出现时，各写各的，不要合并。"""

SUMMARY_BATCH_RULES = """你会收到若干章节，每个以 "## 小节 s000（第 X 页）TITLE" 开头。
对每个小节输出 2~6 条要点，每条 ≤40 字，末尾用 [p.N] 标注页码，只写原文明确内容。
输出一个 JSON（不要解释）：
{"sections":[{"sid":"s000","bullets":["要点 [p.12]","..."]}]}"""


def batch_extract_markdown(specs, chunks: list[dict], batch_id: str) -> str:
    parts = [f"# 抽取任务批次 {batch_id}", "", BATCH_RULES, "",
             "## 字段清单", "", field_block(specs), ""]
    for c in chunks:
        pages = c["pages"]
        span = f"{pages[0]}-{pages[-1]}" if len(pages) > 1 else str(pages[0])
        parts += [f"## 块 {c['cid']}（第 {span} 页）", "", c["text"], ""]
    parts += ["## 输出", "", '把上面的结果保存为 JSON 文件，内容形如 {"facts":[...]}。', ""]
    return "\n".join(parts)


def batch_summary_markdown(sections: list[dict], batch_id: str) -> str:
    parts = [f"# 浓缩任务批次 {batch_id}", "", SUMMARY_BATCH_RULES, ""]
    for s in sections:
        parts += [f"## 小节 {s['sid']}（第 {s['page']} 页）{s['title']}", "",
                  s["text"], ""]
    parts += ["## 输出", "", '保存为 {"sections":[{"sid":"s000","bullets":[...]}]}。', ""]
    return "\n".join(parts)
