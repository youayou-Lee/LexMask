# 交付运维 Runbook（裸机双 GPU 版）

> [!warning] 形态定位：**历史/备用拓扑的运维手册**
> 本手册描述的是 `deploy/dual-5090/` 这套**裸机双 GPU** 脚本形态。**当前交付与验收实例为云 SCNet DCU K100**（见 `cloud-deploy/` 与 `docs/性能与质量白皮书.md` §3），不是本手册的双卡拓扑；性能承诺一律以 DCU 口径为准（白皮书 v1.1 §3），本手册不含任何性能数字。

适用拓扑：单机双卡（`deploy/dual-5090/` 存档形态），全部服务 `~/*.sh` + `start_all.sh` 管理。

## 1. 全新安装（从零）

1. 前置：Ubuntu 22/24、NVIDIA 驱动、CUDA 12.8+、`~/anaconda3/envs/lexmask`（python+node）、
   `~/rvenv/vllm` 与 `~/rvenv/la` 两个 venv——按 `deploy/dual-5090/bootstrap.sh` 与
   `build_*_venv.sh` 执行；模型经魔搭下载（`dl_models.sh`，HF 网络不可用时的默认路径）。
2. 代码落位：`~/redaction-deploy/`（backend + frontend/dist）。
3. 服务脚本落位：`deploy/dual-5090/*.sh` 拷到 `~/`（含 GPU 钉卡与显存配额，勿改数值）。
4. `backend_g0.sh` 里设置 **JWT_SECRET_KEY**（必改）、AUTH_ENABLED=true、并发参数（见脚本内注释）。
5. 启动：`bash ~/start_all.sh`（自愈式：只拉起 DOWN 的端口）。VL 服务就绪要 1-3 分钟。
6. 验收：`curl localhost:8000/health/services` → `all_online: true`；跑 `e2e/` Tier-1 两条冒烟。

## 2. 版本升级（带自动回滚）

本地出包 → 传服务器 → 一条命令升级：

```bash
# 本地（仓库根）
python deploy/release.py            # 产出 dist-release/redaction-release-<ver>-<时间>.tar.gz
# 传到服务器家目录后（scp/base64 均可）
bash deploy/upgrade.sh ~/redaction-release-*.tar.gz
```

`upgrade.sh` 流程：备份现网（`~/upgrade-backups/<时间戳>/`）→ 应用 → 只重启后端 :8000
（GPU 模型服务不动）→ **90 秒健康门禁，失败自动回滚到备份并再次拉起**。
退出码：0=升级成功；1=已自动回滚；2=回滚后健康未确认（看 `~/backend.log`）。
自动保留最近 5 份升级备份。

## 3. 备份与恢复

- **每小时自动**：后端进程内置 SQLite 备份（jobs/file_store/token_blacklist，`db_backup`）。
- **每日建议**（crontab）：`tar -czf ~/backups/data-$(date +%F).tar.gz -C ~/redaction-deploy/backend data uploads outputs`
- **恢复**：停后端（`fuser -k 8000/tcp`）→ 解包覆盖 `backend/data|uploads|outputs` → 拉起
  `bash ~/backend_g0.sh`。auth.json 损坏时后端会自动用同目录 `.bak` 恢复。

## 4. 常见故障

| 症状 | 处理 |
|---|---|
| all_online=false，某模型服务 DOWN | `bash ~/start_all.sh`（只拉起挂掉的）；vLLM 类服务杀残留要按 `nvidia-smi --query-compute-apps` 查孤儿 EngineCore PID `kill -9`，别按名字 pkill |
| 重启服务器后全栈恢复 | `bash ~/start_all.sh` 一条命令（脚本自带依赖顺序与就绪等待） |
| 显存不足/OOM | 先查孤儿进程；配额（gpu-util 等）都在 `~/*.sh` 内注释处，改前先看注释红线 |
| 升级后异常 | `upgrade.sh` 已自动回滚；手动回滚=用 `~/upgrade-backups/<最新>/` 覆盖后重启 :8000 |
| 忘记管理员密码 | 停后端，编辑 `backend/data/auth.json` 删除该用户条目后重启，用 /auth/setup 重建（仅剩余 0 用户时）或由另一管理员改密 |

## 5. 升级影响面速查

- 只动 `backend/app`、`backend/config`、`backend/scripts`、`frontend/dist` → 重启 :8000，秒级中断
- 动 `~/*.sh`（GPU 服务配置）→ 需重启对应模型服务，遵循 `start_all.sh` 依赖顺序，避开业务高峰
- 动 `backend_g0.sh` 环境变量 → 重启 :8000 即生效

## 6. 内嵌文本层文档级链（Issue#115 / #129，默认开）

扫描件自带**可用**内嵌文本层时，本链接管该页的「文本实体」部分：**整卷抽一次文本层 → 按 ~2000 字不跨词分块 → 文档级 HaS**，逐页 OCR 归零；印章/签名仍走 LA 逐页链。无可用文本层的页、卷级门不达标、建卷异常 ⇒ **逐页回落旧视觉线**。

实测（60 页真实卷，同机同会话）：**6.72s/页 → 3.03s/页**，OCR 边车调用 58 → 0，HaS 调用 394 → 191。

**值守要记住的两件事：**

1. **首屏要等整卷建流**：任一页的**首次**请求会同步跑完整卷（60 页约 115–171s、189 页约 231s），之后各页 memo 命中、秒级。因此
   - 网关超时必须覆盖它：`frontend/nginx.conf.template` 的 `proxy_read_timeout` 已设为 **900s**；若前面另有一层反代（SLB/宝塔/云厂商网关），**其默认 60s 超时必须同步抬高**，否则首屏 504（后端仍在建流，重试即命中）。
   - 超大卷有兜底闸：`TEXT_LAYER_DOCLINE_MAX_BUILD_SEC`（默认 600s ≈ 375 页）——预估超预算的卷**直接不接管**，逐页走旧线，不会打爆网关。
2. **怎么关**：`TEXT_LAYER_DOCLINE_ENABLED=false`（`backend_g0.sh` 或 `.env`），重启 :8000 即整链回退到 #115 之前的逐页视觉线，**无需改代码**。低显存 / 前面有短超时网关 / 只想快速出首屏的部署建议关。

**出问题时的日志特征**（`~/backend.log`）：
- `text_layer_docline built file=… pages=… chunks=… coverage=… calls=… elapsed=…` — 建流成功，看 `elapsed` 是否符合预期
- `text_layer_docline skipped: …` — 未接管及原因（`layer …` 层不合格 / `no text layer` / `estimated build … > budget …` / `coverage …` / `truncated …` / `failed_chunks …`），此时该卷走旧线，属**预期行为**
- `Vision detect stored file=… page=… boxes=…` — 每页落盘（两条链共用）

**排查顺序**：① 该卷是否 `skipped`（是 ⇒ 旧线，看旧线问题）；② `elapsed` 是否远超预算；③ 网关是否 504（对比 `backend.log` 里是否其实已经建完）。
