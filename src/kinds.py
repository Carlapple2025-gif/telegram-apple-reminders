#!/usr/bin/env python3
"""
数据结构：消息属于哪个 App、以及它的内容。

## 为什么单独一个模块

这三个数据结构原本住在 `classify.py` 里 —— 而那个模块要做的事是
**从自然语言猜类型**（约 700 字词表 + 405 行规则），已被整体删除
（见 [`docs/SYMBOL-SCHEME.md`](../docs/SYMBOL-SCHEME.md)）。

但**数据结构本身是对的**，下游（intake / daemon / report）都在用，
所以把它们搬出来。删除的是"猜"，不是"类型"。

## 类型现在从哪来

**从你写的符号来**，不由代码推断：

    # 内容      → Kind.MEMO
    @内容      → Kind.EVENT
    - [ ] 内容  → Kind.TODO
    裸内容      → Kind.TODO（最高频，零符号）

见 `parse.strip_leading_marker()`（认符号）与 `routes.py`（决定落点）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import whens


class Kind(str, Enum):
    """一条消息的归属。**由符号声明，不推断。**"""
    TODO = "todo"
    EVENT = "event"
    MEMO = "memo"


@dataclass
class Item:
    """
    一条已定型的消息：属于哪里、正文是什么、时间（若有）。

    字段对应关系：
      · `text`       —— 写进 App 的正文（符号已剥掉、时间短语已剥掉）
      · `raw`        —— 原始输入（逐字保留，便于核对与记 journal）
      · `when`       —— 解析出的时间（仅日程必要）
      · `recurrence` —— iCal RRULE 片段（"每周一"这类）
    """
    kind: Kind
    text: str
    raw: str
    when: whens.When | None = None
    recurrence: str = ""
    # 只说了时刻、而该时刻今天已过 → 已顺延到次日。
    # 回执要如实说明，否则用户看到的时间与自己说的对不上会困惑。
    rolled: bool = False
