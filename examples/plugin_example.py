"""示例插件：复制到 plugins/ 或 CHIPDOC_PLUGINS 指向的目录即可生效。

- fields(kind)         追加字段，会进入抽取 prompt 和对比表
- on_facts(facts, idx) 校验之后、归并之前的过滤/补全
- on_profile(profile)  归并后的画像，可改 dict
- markdown(profile)    追加到 summary.md 末尾
"""
from __future__ import annotations


def fields(kind: str):
    if kind != "datasheet":
        return []
    return [{
        "id": "pin_count",
        "label": "引脚数",
        "hint": "封装引脚数量，如 80",
        "unit": "pin",
    }]


def on_facts(facts, index):
    # 例：把 "80-pin" 拆出纯数字引脚数
    import re
    out = list(facts)
    for f in facts:
        if f.field == "package":
            m = re.search(r"(\d{1,3})[-\s]?pin", f.value, re.I)
            if m:
                from chipdoc.verify import Fact
                out.append(Fact(field="pin_count", value=m.group(1), page=f.page,
                                quote=f.quote, source="plugin", confidence=f.confidence))
    return out


def markdown(profile) -> str:
    f = profile.fields.get("pin_count") if hasattr(profile, "fields") else None
    if not f:
        return ""
    return f"## 插件备注\n\n- 引脚数（由插件从封装推导）：{f['value']} [p.{f['page']}]"
