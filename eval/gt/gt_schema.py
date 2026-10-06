"""GT 模式常量单一事实源（Issue#56 M1 / Task 6，先收编页型枚举）。

Task 6 校验器（``validate_pagepack`` / ``write_gt_jsonl``）随后续提交落地于本
模块；当前先落 **PAGE_TYPES 单一事实源收编**（T4 评审 Minor#3）：``body``/
``table``/``seal_handwriting``/``edge`` 四页型此前以 ``gt.compare`` 模块常量
为事实源（Task 4 落地时的占位裁定），本模块接管后 ``gt.compare`` 改为
re-export，``gt.arbitrate`` 经由 compare 的既有导入不受影响——任何模块都
不再自带页型字面量。
"""
from __future__ import annotations

# 有效页型（GT 管线唯一事实源；compare/arbitrate 均自本模块取用）
PAGE_TYPES = {"body", "table", "seal_handwriting", "edge"}
