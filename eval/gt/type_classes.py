"""实体类型分域（GT 闸门口径三分法 + 文本子域，Issue#56 P0）。

设计意图（2026-10-14 定稿，见
``LexMask-workspace/docs/plans/2026-10-14-gt-agent-loop-automation.md`` §6/§6.1）：

GT 闸门判定"两条通道的读数能不能自动放行"时，**不能用「差异大不大」当尺子**
——因为最危险的差异恰恰看起来不大：``110101199001011234`` 与 ``…1284`` 的字符
相似度（94%）和 ``2026年1月5日`` 与 ``2026 年 1 月 5 日`` 几乎无法区分，但前者是
值差、后者是形态差。可用的尺子是**差异的性质**，而差异的性质由实体所属**域**决定：

- ``identifier`` 强标识符 —— 归一化后**必须逐位相等**，不等即值差，一律不自动放行。
  证据（hardcase 桶数字分级）：电话 100% exact / 身份证 75.8% / **银行卡 14.3%**；
  "差一位"是真实重灾区，对这类做模糊放行等于把真错静默洗成"通过"。
- ``datetime`` 日期时刻 —— 归一化到 (Y, M, D) 元组后比，**粒度差容忍**
  （§6.1 口径：``2023年`` ≡ ``2023年6月15日``），但须记录 ``granularity_gap``
  与方向——"更粗"是丢细节，"更细"可能是凭空补全。
- ``quantity`` 数值数量 —— 归一化到 (数值, 单位) 后**必须相等**，无粒度容忍
  （``10000元`` 与 ``1万元`` 归一后必须相等，但 ``10000`` 与 ``100000`` 是值差）。
- ``text`` 文本 —— 再分三个子域，放行规则各不相同：
  - ``enum``    枚举/字段值（性别、民族、国籍…）：归一化到枚举值后必须相等；
  - ``name``    名称型（姓名、地址、机构名…）：语义等价，形态差可放行，
    **近形字警惕**（李/季、己/已、术/木）；
  - ``passage`` 长描述（法律记录、诉讼请求、病史类…）：判等按实体对齐 + 内容重叠率，
    不按逐字——两条通道读同一段话必然多处不同，逐字比只会制造假分歧。

**模糊归属取严**（fail-safe）：某类型两种域都说得通时归入更严的域
（``identifier`` > ``quantity`` > ``datetime`` > ``text``）：多出的成本是人审，安全；
反向则可能静默放行真错。实例——``定位位置``（GPS_LOCATION）的样例既含经纬度
（强标识符形态）又含"签到定位：深圳市南山区科技园"（文本形态），按取严归
``identifier``。

**单一事实源**：本模块只提供"类型名 → 域"的**策略映射**；类型名全集仍由
``gt_schema.PRESET_TYPE_NAMES``（动态读 preset）供给。``coverage_report``
返回"preset 未分类"与"本模块多出"两侧清单，单测断言两者皆空——preset 增删类型时
本模块**显式失配**，而不是静默漏掉一个类型（类型表硬编码曾是 Issue#100 的成因之一）。
"""
from __future__ import annotations

from typing import Iterable

# 域（唯一事实源；gate.py 按此取用）
DOMAINS = ("identifier", "datetime", "quantity", "text")

# 文本子域（仅 domain == "text" 时取值）
TEXT_SUBDOMAINS = ("enum", "name", "passage")

# 取严次序：索引越小越严（模糊归属时取最严者）
_STRICTNESS_ORDER = ("identifier", "quantity", "datetime", "text")

# ---- 类型名 → 域 策略映射（preset 中文名，68 类全覆盖） ---------------------------

# 强标识符：归一化后必须逐位相等
_IDENTIFIER = (
    "身份证号", "护照号", "社保号", "电话", "邮箱", "邮政编码", "定位位置",
    "登录账号", "密码", "银行卡号", "银行账号", "设备号", "IP地址", "网址",
    "统一社会信用代码", "税号", "编号", "车牌号", "车架号", "案号", "客户号",
    "交易流水号", "商户号", "病历号", "文书编号", "证号", "登记号", "住院号",
)

# 日期时刻：归一到日期元组后比，粒度差容忍
_DATETIME = ("日期", "时间", "出生日期")

# 数值数量：归一到 (数值, 单位) 后必须相等
_QUANTITY = ("金额", "年龄", "生命体征")

# 文本 / 枚举：归一到枚举值后必须相等
_TEXT_ENUM = (
    "性别", "民族", "国籍", "婚姻状态", "政治面貌", "宗教信仰", "性取向", "风险评级",
)

# 文本 / 名称型：语义等价，近形字警惕
_TEXT_NAME = (
    "姓名", "地址", "开户行", "组织机构", "公司名称", "机构名称", "机关单位",
    "工作单位", "部门名称", "项目名称", "职业", "籍贯", "科室",
)

# 文本 / 长描述：实体对齐 + 内容重叠率，不按逐字
_TEXT_PASSAGE = (
    "生物特征", "健康信息", "法律记录", "诉讼请求", "诊断", "用药", "检查结果",
    "主诉", "现病史", "既往史", "过敏史", "医疗操作", "医嘱",
)


def _build_map() -> dict[str, tuple[str, str | None]]:
    mapping: dict[str, tuple[str, str | None]] = {}
    groups: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
        ("identifier", None, _IDENTIFIER),
        ("datetime", None, _DATETIME),
        ("quantity", None, _QUANTITY),
        ("text", "enum", _TEXT_ENUM),
        ("text", "name", _TEXT_NAME),
        ("text", "passage", _TEXT_PASSAGE),
    )
    for domain, subdomain, names in groups:
        for name in names:
            if name in mapping:  # 构建期自检：同名不得落两域
                raise AssertionError(f"类型名 {name!r} 被映射到多个域")
            mapping[name] = (domain, subdomain)
    return mapping


# 类型名 → (domain, subdomain)；subdomain 仅 text 域非 None
TYPE_DOMAIN_MAP: dict[str, tuple[str, str | None]] = _build_map()


def classify(name: str) -> tuple[str, str | None]:
    """返回 ``(domain, subdomain)``；未登记的类型名 raise ``KeyError``。

    ``subdomain`` 仅当 ``domain == "text"`` 时非 None（∈ TEXT_SUBDOMAINS）。
    """
    try:
        return TYPE_DOMAIN_MAP[name]
    except KeyError:
        raise KeyError(
            f"实体类型名 {name!r} 未登记分域——preset 新增类型后须同步 "
            f"type_classes.TYPE_DOMAIN_MAP（见模块 docstring「单一事实源」）"
        ) from None


def domain_of(name: str) -> str:
    """只取域（忽略子域）。"""
    return classify(name)[0]


def is_identifier(name: str) -> bool:
    """强标识符判定——闸门对其一律不做模糊放行。"""
    return classify(name)[0] == "identifier"


def strictness_rank(name: str) -> int:
    """取严次序的秩（越小越严）；用于模糊归属时挑最严的候选。"""
    return _STRICTNESS_ORDER.index(domain_of(name))


def most_strict(candidates: Iterable[str]) -> str:
    """在一组候选域/类型名里取最严者（模糊归属取严的实现入口）。

    入参可为域名字符串或类型名；空序列 raise ``ValueError``。
    """
    items = list(candidates)
    if not items:
        raise ValueError("most_strict 需至少一个候选")

    def rank(item: str) -> int:
        domain = item if item in DOMAINS else domain_of(item)
        return _STRICTNESS_ORDER.index(domain)

    return min(items, key=rank)


def coverage_report(preset_names: Iterable[str]) -> dict[str, list[str]]:
    """比对预设类型名集与本模块映射，返回两侧失配清单。

    返回 ``{"unclassified": [...], "unknown": [...]}``：

    - ``unclassified`` preset 里有、本模块没登记（闸门会 KeyError，必须补）；
    - ``unknown``      本模块登记了、preset 里已不存在（preset 删类型后的残留）。

    单测对真实 preset 断言两者皆空，使预设变更显式暴露而非静默漏类型。
    """
    preset = set(preset_names)
    mapped = set(TYPE_DOMAIN_MAP)
    return {
        "unclassified": sorted(preset - mapped),
        "unknown": sorted(mapped - preset),
    }