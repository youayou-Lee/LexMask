"""T2 桶人类可读元数据：内容类型、全量/抽样说明（报告与 CLI 展示用）。

权威桶定义在旧 eval/benchmarks/t2/spec.py（冻结）；本表只补"给人看"的说明，
不参与判卷。sampling 语义：
- "抽样"：原始集规模远大于桶配额，固定种子抽取（可复现）；
- "全量"：桶条数即全部（合成生成/人工沉淀入库的所有条目）。
"""

BUCKET_META = {
    "cluener-person": {
        "content": "公开集 CLUENER·人名（新闻/百科体短句）",
        "sampling": "抽样：原集千级条目固定种子抽 200",
    },
    "cluener-address": {
        "content": "公开集 CLUENER·地址（含场景名并入）",
        "sampling": "抽样：固定种子抽 200",
    },
    "cluener-organization": {
        "content": "公开集 CLUENER·机构名",
        "sampling": "抽样：固定种子抽 200",
    },
    "resume-person": {
        "content": "公开集 Resume·人名（简历体）",
        "sampling": "抽样：固定种子抽 200",
    },
    "resume-native-place": {
        "content": "公开集 Resume·籍贯（简历体）",
        "sampling": "抽样：固定种子抽 200",
    },
    "digit-confusion": {
        "content": "合成·数字陷阱：身份证/案号/电话/银行卡抄错一位（一票否决主考场）",
        "sampling": "全量：程序生成 50 条即全部",
    },
    "quoted-entity": {
        "content": "合成·引号边界：引语中的实体（\u201c李四\u201d这类）",
        "sampling": "全量：程序生成 50 条即全部",
    },
    "long-entity": {
        "content": "合成·超长实体（拼接式机构名）",
        "sampling": "全量：程序生成 50 条即全部",
    },
    "lowfreq-type": {
        "content": "合成·低频类型（民族/宗教信仰/政治面貌/国籍）",
        "sampling": "全量：程序生成 50 条即全部",
    },
    "context-distractor": {
        "content": "合成·上下文干扰：干扰词紧贴真实体",
        "sampling": "全量：程序生成 50 条即全部",
    },
    "hardcase": {
        "content": "真实案卷难例·人工沉淀入库（整页长文/表格，区分度最高）",
        "sampling": "全量：人工录入的全部 65 条都在桶内",
    },
    "leven-judicial-person": {
        "content": "BLOCKED：LEVEN 原集无实体标注，桶未建成",
        "sampling": "—",
    },
    "leven-judicial-org": {
        "content": "BLOCKED：LEVEN 原集无实体标注，桶未建成",
        "sampling": "—",
    },
}
