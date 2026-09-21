"""报告生成与回放共用的数字单位边界，不进行换算或口径推断。"""

import re

MEASUREMENT = re.compile(
    r"(?<![\d.,])\d+(?:\.\d+)?\s*(?:万亿|亿|万|千)?\s*(?:%|％|人次|人|条|次|起|件|所|元|万元|亿元|小时|天|分钟|页|倍)"
)


def measurement_values(text: str) -> set[str]:
    return {match.group() for match in MEASUREMENT.finditer(text)}
