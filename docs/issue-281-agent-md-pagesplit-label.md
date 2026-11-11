# Issue#281 供料产物替换缺陷：跨页切开实体漏替换 + 类型标签叠字

> 关联 Issue：<https://github.com/youayou-Lee/LexMask/issues/281>
> 状态：修复 + 单测 + 本地真卷重跑验证完成（分支 `fix/issue281-agent-md-pagesplit`），待评审/合并。

## 背景

供料模式（Agent MD）把 zip 官方结构化 md 作为产物载体、把拍平 segs 作为检测面（Issue#248 替换式改造）。两者在页边界上的表示不同：

- 结构化 md：页间是「页脚数字 + `<!-- page N of M -->`」；
- 检测面 segs：页间只有 `\n\n`。

实体原文恰好被页边界切成两半时（上一页末前半、下一页首后半），产物侧不存在连续形态，`render_outputs_on_structure` 的字节级 `replace` 与空白柔性正则（flex）都匹配不到。

## 根因（三条）

1. **表示失配**：页边界噪声穿插在实体中间，plain/flex 两种匹配都只认「连续/仅空白差」形态。
2. **两道闸双假绿**：覆盖闸判「映射项原文是否出现在 md」，残留闸判「替换后原文是否仍在 md」。同一事实的 OCR 空白变体项（更长的 flex 项）按长度降序先替换掉唯一连续形态后，被切开的项既「不缺席」（连续形态存在于别处）也「不残留」（切开形态不是逐字原文）——真实标识因此出厂。
3. **标签叠字**：`build_mapping_draft` 的 replacement 固定为「类型短名+序号」；OCR 原文自带同款短名（「案号（2023）…号」）时，替换后成「案号案号N」。

## 方案

`backend/app/services/agent_md_replace.py` 两段纯逻辑，均在结构化渲染主路径生效，不改变映射表与审阅口径：

1. `_apply_page_split(md, items)`：
   - 对每个非 excluded 映射项，在**去空白后**的字符序列上枚举全部切分点 `i`；
   - 正则 = head 字符（`\s*` 连接）+ 页边界噪声（`\s*(?:\d{1,3}\s*)?<!-- page N of M -->\s*`，命名组 `pbnoise`）+ tail 字符（`\s*` 连接）；
   - 替换值 = `replacement + pbnoise`——**页边界噪声原样回插**，否则 `verify_structure_integrity` 的页标记序列对账会判结构破坏（这是本方案的关键约束）；
   - 返回 `{映射项 id: 用过的正则}`，供覆盖闸/残留闸判定。
2. `_collapse_label_duplication(md, items)`：replacement 形如「短名+序号」且「短名+replacement」在产物中真实存在时，收口为单个标签（`案号案号5` → `案号5`）；映射表 replacement 不变。legacy `render_outputs` 同样调用。
3. 闸门同步：覆盖闸对 `_split_applied` 中的项不再按「原文连续出现」判缺席；残留闸对这类项改用其切分正则复检。

## 验收

- AC1 跨页切开形态被替换为映射占位符，页标记序列/结构对账不变，连续形态替换行为不变；
- AC2 产物无「案号案号N」叠字，映射表 replacement 仍为「案号N」；
- AC3 excluded 项不受影响（跨页形态保留原文，保留清单仍记账）；
- AC4 单测全绿、零回归。

单测：`backend/tests/test_issue281_pagesplit_label.py`（6 例）。本地真卷重跑：同一 6 页扫描件、同一 30 条映射，修复后跨页项落地「案号4」（修复前 0 次）、叠字收口、6/6 页标记与结构对账通过，映射表/保留清单逐字节相同。

## 范围外（另案拍板）

- 同一事实的 OCR 空白变体各自占用一个占位符（同一案号出现两种占位符），下游 Agent 会误判为两个案件——是否在映射生成期合并，涉及映射表可见行为，待定。
- 机构名保留（ORG 默认 excluded）是既有定稿策略，本 Issue 不动；统一社会信用代码类型缺口另案。