"""字段 schema：定义要从芯片文档里抽取哪些字段。

设计原则
- 字段是"可横向对比"的最小集合：不同型号/不同厂商的同一类芯片都能填。
- hint 直接进入 LLM prompt，必须写成"去哪找、长什么样"。
- 所有值保留原始字符串（含单位），归一化只用于比较，不覆盖原文。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    id: str
    label: str          # 中文短标签，用于表格
    hint: str           # 给模型的定位提示
    unit: str = ""
    aliases: tuple[str, ...] = ()


DATASHEET_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("device", "型号", "文档覆盖的器件型号/系列，写主型号，如 TMS320F28054F", ""),
    FieldSpec("core", "内核", "CPU 内核架构，如 C28x、ARM Cortex-M4F", ""),
    FieldSpec("max_freq", "最高主频", "系统时钟最高频率，单位 MHz", "MHz"),
    FieldSpec("flash", "片上 Flash", "Flash 容量，注意 16-bit word 还是 byte", "KB"),
    FieldSpec("ram", "片上 RAM", "SARAM/RAM 容量", "KB"),
    FieldSpec("adc", "ADC", "ADC 分辨率与通道数，如 12-bit, 16 ch", ""),
    FieldSpec("pwm", "PWM 通道", "ePWM/PWM 通道数", "ch"),
    FieldSpec("can", "CAN", "CAN/eCAN 模块数量", ""),
    FieldSpec("uart", "UART/SCI", "SCI/UART/LIN 数量", ""),
    FieldSpec("spi", "SPI", "SPI 模块数量", ""),
    FieldSpec("i2c", "I2C", "I2C 模块数量", ""),
    FieldSpec("supply", "供电电压", "IO/内核供电范围，如 3.3V single supply", "V"),
    FieldSpec("temp", "工作温度", "环境/结温范围，如 -40 to 125 °C", "°C"),
    FieldSpec("package", "封装", "封装类型，如 80-pin LQFP", ""),
    FieldSpec("pins", "引脚数", "封装引脚数", ""),
    FieldSpec("security", "安全特性", "安全启动/加密/zone security 等", ""),
    FieldSpec("special", "特色外设", "CLA、DMA、USB、以太网、比较器等值得对比的外设", ""),
)

TRM_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("device", "适用器件", "TRM 适用的器件系列", ""),
    FieldSpec("cpu", "CPU 子系统", "CPU 架构、FPU、TMU、VCU 等", ""),
    FieldSpec("memory_map", "存储器映射", "Flash/RAM/外设帧的地址范围", ""),
    FieldSpec("clocking", "时钟系统", "时钟源、PLL、分频、SYSCLK 上限", ""),
    FieldSpec("interrupts", "中断", "PIE 中断数、向量表、优先级", ""),
    FieldSpec("boot", "引导", "Boot ROM、引导模式选择", ""),
    FieldSpec("peripherals", "外设清单", "TRM 覆盖的外设模块列表", ""),
    FieldSpec("registers", "寄存器", "寄存器帧/访问方式/保护机制", ""),
    FieldSpec("power", "低功耗模式", "IDLE/STANDBY/HALT 等模式与唤醒源", ""),
    FieldSpec("debug", "调试与安全", "JTAG、ERAD、安全 zone、CSM", ""),
)


GENERIC_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("doc_type", "文档类型", "datasheet / TRM / 应用笔记 / 勘误表 / 用户指南 / 编程手册", ""),
    FieldSpec("device", "适用器件", "文档覆盖的器件或系列", ""),
    FieldSpec("version", "版本", "修订号与日期，如 Rev. F, 2021-09", ""),
    FieldSpec("scope", "适用范围", "这份文档解决什么问题/覆盖哪些模块", ""),
    FieldSpec("key_params", "关键参数", "性能、电气、存储等核心指标", ""),
    FieldSpec("interfaces", "接口", "总线、外设、引脚/信号", ""),
    FieldSpec("timing", "时序/频率", "时钟、时序要求、频率上限", ""),
    FieldSpec("power", "电源/功耗", "供电范围、电流、低功耗模式", ""),
    FieldSpec("environment", "环境/封装", "温度范围、封装、可靠性等级", ""),
    FieldSpec("registers", "寄存器/地址", "寄存器映射、基地址、访问方式", ""),
    FieldSpec("errata", "限制与勘误", "已知问题、使用限制、注意事项", ""),
)


def fields_for(kind: str) -> tuple[FieldSpec, ...]:
    return {"datasheet": DATASHEET_FIELDS, "trm": TRM_FIELDS}.get(kind, GENERIC_FIELDS)


def load_fields(path: str) -> tuple[FieldSpec, ...]:
    """自定义字段集：JSON 数组 [{id,label,hint,unit?}]，用于某一类文档。"""
    import json
    from pathlib import Path
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for d in data:
        out.append(FieldSpec(id=str(d["id"]), label=str(d.get("label", d["id"])),
                             hint=str(d.get("hint", "")), unit=str(d.get("unit", ""))))
    if not out:
        raise ValueError(f"字段集为空: {path}")
    return tuple(out)


def field_ids(kind: str) -> tuple[str, ...]:
    return tuple(f.id for f in fields_for(kind))


# --------------------------------------------------------------------------
# 值归一化：仅用于比较 / 去重，绝不回写原文
# --------------------------------------------------------------------------

_UNIT_ALIASES = {
    "兆赫": "mhz", "mhz": "mhz", "m h z": "mhz", "兆": "m",
    "千字节": "kb", "kb": "kb", "k": "kb", "kib": "kb",
    "伏": "v", "v": "v", "毫安": "ma", "ma": "ma",
    "摄氏度": "c", "°c": "c", "℃": "c", "c": "c",
}

_NUM = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*([a-zA-Z%°℃]{0,6})")


def _nfkc(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    return (t.replace("–", "-").replace("—", "-").replace("−", "-")
             .replace("，", ",").replace("、", ",").replace("　", " "))


def clean_value(value: str, limit: int = 120) -> str:
    v = _nfkc(str(value)).strip()
    v = re.sub(r"\s+", " ", v)
    v = v.strip(" .;；,，:：")
    return v[:limit]


def normalize_for_compare(value: str) -> str:
    """把 '64K (16-bit word)' / '64 KB' / '64K' 归一到 '64kb'。"""
    v = _nfkc(clean_value(value)).lower()
    v = re.sub(r"[\s_]+", "", v)
    v = re.sub(r"(?:×|x)\s*16-?bit(?:word)?", "", v)
    m = _NUM.search(v)
    if not m:
        return v
    num, unit = m.group(1).replace(",", "."), m.group(2)
    unit = _UNIT_ALIASES.get(unit, unit)
    if unit == "kb" and num.replace(".", "").isdigit() and float(num) >= 100000:
        # 128000 -> 128k
        pass
    return f"{num}{unit}"


def equivalent(a: str, b: str) -> bool:
    """两个值是否等价（用于增量 diff）。"""
    na, nb = normalize_for_compare(a), normalize_for_compare(b)
    if na == nb:
        return True
    # 处理 64K vs 64 KB vs 65536 这类换算
    ma, mb = _NUM.search(na), _NUM.search(nb)
    if ma and mb and ma.group(2) == mb.group(2):
        try:
            return abs(float(ma.group(1)) - float(mb.group(1))) < 1e-9
        except ValueError:
            return False
    return False
