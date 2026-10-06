# T2 Benchmark 桶级对比

- 引擎：has
- 生成时间：2026-10-06T22:21:01
- 数据目录：/root/private_data/benchmarks/t2/buckets

| 桶 | 引擎 | P | R | F1 | 数字exact |
|---|---|---|---|---|---|
| cluener-address | has | 0.7760 | 0.6191 | 0.6888 | N/A |
| cluener-organization | has | 0.8195 | 0.7241 | 0.7689 | N/A |
| cluener-person | has | 0.8125 | 0.7263 | 0.7670 | N/A |
| context-distractor | has | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| digit-confusion | has | 0.9633 | 0.9633 | 0.9633 | 1.0000 |
| hardcase | has | 0.7400 | 0.5968 | 0.6607 | 0.1429 |
| long-entity | has | 0.5306 | 0.6933 | 0.6012 | 1.0000 |
| lowfreq-type | has | 1.0000 | 1.0000 | 1.0000 | N/A |
| quoted-entity | has | 1.0000 | 1.0000 | 1.0000 | N/A |
| resume-native-place | has | 0.7781 | 0.7459 | 0.7616 | N/A |
| resume-person | has | 0.6421 | 0.6254 | 0.6337 | N/A |

## 数字分级明细（exact / near_miss / miss）
- context-distractor / has / 电话：exact=50 near_miss=0 miss=0 exact_rate=1.0000
- digit-confusion / has / 电话：exact=50 near_miss=0 miss=0 exact_rate=1.0000
- digit-confusion / has / 身份证号：exact=50 near_miss=0 miss=0 exact_rate=1.0000
- digit-confusion / has / 银行卡号：exact=50 near_miss=0 miss=0 exact_rate=1.0000
- hardcase / has / 电话：exact=14 near_miss=0 miss=0 exact_rate=1.0000
- hardcase / has / 身份证号：exact=25 near_miss=0 miss=8 exact_rate=0.7576
- hardcase / has / 银行卡号：exact=5 near_miss=0 miss=30 exact_rate=0.1429
- long-entity / has / 身份证号：exact=50 near_miss=0 miss=0 exact_rate=1.0000
