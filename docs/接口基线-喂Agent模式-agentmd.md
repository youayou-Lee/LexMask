# 接口基线：喂 Agent 模式（agent-md，五端点 + MinerU sidecar 16581）

> 对应 Issue：#75（喂Agent并行模式——MinerU+NER→脱敏MD+映射表，可直接投喂云端 AI）
> 服务位置：LexMask backend `/api/v1/agent-md/*`（`backend/app/api/agent_md.py`，业务逻辑在 `app/services/agent_md_pipeline_service.py`）；外部依赖 MinerU 3.4 sidecar（实例内 `127.0.0.1:16581`，独立 venv 常驻）。
> 本文档是 **喂 Agent 模式的接口基线**：前端独立页（路由 `/agent-md`）与第三方调用方按此对接，schema 变更须同步更新本文并走评审。
> 设计约束：**并行新模式，现有管线零改**——`main.py` 仅 import + `include_router` 两行注册，playground/批量链路不受影响。

## 1. 服务概要

| 项 | 值 |
|---|---|
| 功能 | PDF →（MinerU 解析 + 语义 NER）→ 脱敏 Markdown + 映射表 + 保留字段清单，三件套产物可直接投喂云端 AI |
| 流程 | 上传建任务 → Stage1 后台（解析→NER→映射草稿）→ 人工复核映射表 → 确认出稿 → 下载三件套 |
| 传输 | HTTP，backend 统一前缀 `/api/v1`；鉴权与既有端点一致（开启认证时 Bearer JWT 或 `X-API-Key`，`AUTH_ENABLED=false` 时匿名） |
| 解析依赖 | MinerU sidecar `http://127.0.0.1:16581`（`MINERU_API_BASE_URL`；MinerU 3.4 `mineru-api`，必须 `backend=pipeline`） |
| 部署 | sidecar 启停命令见 §6；`start_all_dcu.sh` 待加段落原文亦见 §6（实际实例操作在验收阶段执行） |
| 配置项 | `MINERU_API_BASE_URL`（默认 `http://127.0.0.1:16581`）· `MINERU_POLL_INTERVAL`（2.0s）· `MINERU_TASK_TIMEOUT`（7200s）· `AGENT_MD_CHUNK_CHARS`（6000，NER 分块） |

## 2. 接口定义（五端点）

### POST /api/v1/agent-md/upload

上传 PDF 建任务。`multipart/form-data`：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | file | 是 | **仅 `.pdf`**；非 PDF → `400 UNSUPPORTED_FILE_TYPE` |
| `password` | string | 否 | 加密 PDF 密码；仅本请求即用即弃——不落日志、不进错误响应、不存任务上下文 |

响应 `200`：

```json
{"task_id": "<uuid>"}
```

> 注意：`task_id` 是**管线任务 id**，与上传文件在 `UPLOAD_DIR` 的落盘 uuid 文件名不是同一个。任务建立后 Stage1（解析→NER）后台进行，调用方轮询 §2.2。

加密 PDF 复用 Issue#30 file_parser 链路：未给密码时，未加密原样通过、仅权限密码（空密码可解）自动解除、设了打开密码 → `409 PDF_ENCRYPTED_NEEDS_PASSWORD`；给了密码时，密码错误 → `409 PDF_WRONG_PASSWORD`，正确则解密副本进入管线。两个错误码均为 `409`（补密码可重试），前端据此弹密码框。

### GET /api/v1/agent-md/{task_id}/status

任务状态轮询。响应 `200`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | 管线任务 id |
| `state` | string | `uploaded / parsing / ner_running / mapping_ready / completed / failed`（见 §4 状态机） |
| `stage` | string | 当前阶段：`mineru` / `ner` |
| `pages_done` | int | 已完成页数（MinerU 解析期由 sidecar 进度回调填充；sidecar 无页级进度时保持 0） |
| `pages_total` | int | 总页数（fitz 本地计数，建任务即有值，最小 1） |
| `message` | string | 失败原因（`failed` 时为异常摘要）或空 |
| `warnings_count` | int | 适配阶段告警条数（截图未提取、OCR 降级等） |

### GET /api/v1/agent-md/{task_id}/mapping

映射表草稿。**仅 `mapping_ready` 可读**，否则 `409 TASK_NOT_READY`；未知 id → `404 TASK_NOT_FOUND`。

响应 `200`：

```json
{"items": [{"id": "m1", "original_text": "张三", "entity_type": "PERSON",
            "replacement": "[PERSON_1]", "excluded": false}]}
```

| 字段 | 说明 |
|---|---|
| `id` | 映射行 id（confirm 决策以此定位） |
| `original_text` / `entity_type` | 原文与实体类型；同实体同占位符 |
| `replacement` | 拟替换占位符（`custom` 决策可改写） |
| `excluded` | `true` = 用户选择保留原文（不替换，计入 retained_fields） |

### POST /api/v1/agent-md/{task_id}/confirm

确认决策并出稿。请求体 `application/json`：

```json
{"decisions": [{"id": "m1", "action": "keep"},
               {"id": "m2", "action": "exclude"},
               {"id": "m3", "action": "custom", "replacement": "[甲方]"}]}
```

`action` 语义：`keep`＝替换（默认）；`exclude`＝保留原文；`custom`＝自定义替换文本（空值回退原占位符）；未知 id 静默忽略。未在 `decisions` 中出现的行按草稿默认值执行。

非 `mapping_ready` 状态调用 → 任务置 `failed`（message=`task not ready`），此后状态机不再推进；接口层同时回 `409 TASK_NOT_READY`。

响应 `200`：

```json
{"output_file_id": "<uuid>",
 "downloads": {"md": "/api/v1/agent-md/<task_id>/artifacts/md",
               "mapping": "/api/v1/agent-md/<task_id>/artifacts/mapping",
               "retained": "/api/v1/agent-md/<task_id>/artifacts/retained"}}
```

### GET /api/v1/agent-md/{task_id}/artifacts/{kind}

下载产物，`kind ∈ md | mapping | retained`（对应三件套 §5）。仅 `completed` 且产物文件在盘可取；否则 `404 ARTIFACT_NOT_FOUND`。响应为文件下载（`FileResponse`）。

## 3. 错误码表

结构化错误统一走 AppError envelope（前端 `localizeError` 按 `error_code` 映射）：

```json
{"error_code": "TASK_NOT_READY", "message": "映射表尚未就绪", "detail": {}, "request_id": "..."}
```

| error_code | HTTP | 触发 | 调用方动作 |
|---|---|---|---|
| `UNSUPPORTED_FILE_TYPE` | 400 | 上传非 `.pdf` | 换文件 |
| `TASK_NOT_FOUND` | 404 | task_id 不存在 | 停止轮询 |
| `TASK_NOT_READY` | 409 | mapping/confirm 早于 `mapping_ready` | 继续轮询 status |
| `PDF_ENCRYPTED_NEEDS_PASSWORD` | 409 | PDF 设了打开密码且未给密码 | 弹密码框重传 |
| `PDF_WRONG_PASSWORD` | 409 | 给了密码但解不开 | 提示重输 |
| `ARTIFACT_NOT_FOUND` | 404 | 任务未完成或产物文件缺失 | 回查 status |

## 4. 状态机与行为约定

```
uploaded → parsing → ner_running → mapping_ready → completed
                └──────────┴──────────→ failed（任一阶段异常；confirm 过早也会判死）
```

- **两阶段串行**：Stage1（解析→NER→映射草稿）后台异步进行；Stage2（确认出稿）仅在 `mapping_ready` 受理。Stage1 进行中（等锁/解析/NER 任一窗口）收到 confirm 都判死不复活。
- **全局串行解析锁**：多任务共用一把 asyncio Lock，MinerU 解析排队执行（sidecar 自身亦单并发）；NER 阶段不占锁。
- **sidecar 契约要点**（Task 0 实测校准）：提交必须显式 `backend=pipeline`（默认 hybrid-engine 在 DCU 实例必失败）+ `response_format_zip=true` + `return_content_list=true` + `return_images=true`；状态只有 `pending/processing/completed/failed` 字面量、**无页级进度字段**；结果 zip 内 `<stem>/<parse_method>/` 下取 `*.md`、`*_content_list_v2.json`、`images/*`；任务结果保留 24h，failed 取件 409、未知 id 404。
- **适配规则**：image 块 zip 切图补 OCR，失败降级为哨兵占位并计入 warnings；表格剥行保结构；`\(..\)`/`$..$` 配对剥除公式定界符（保案号，跨行不剥）；未处理块类型告警不静默。
- **吞吐量级**：basic 管线 8.5~25s/页（Task 0 实测量级），350 页级案卷在小时级；解析期无页级进度，前端展示「阶段+已耗时+排队数」。

## 5. 产物三件套与还原指引

确认出稿后在 `OUTPUT_DIR` 落三件（同一 `output_file_id` 前缀）：

| 产物 | 文件 | 内容 |
|---|---|---|
| 脱敏稿 | `<output_file_id>.md` | Markdown 全文，`excluded=false` 的实体已替换为占位符（段间 `\n\n`，阅读序保持） |
| 映射表 | `<output_file_id>.mapping.json` | `{"items": [{id, original_text, entity_type, replacement, excluded}]}` |
| 保留字段 | `<output_file_id>.retained_fields.json` | `{"retained_fields": [{text, type, page}]}`——所有 `exclude` 保留的原文清单 |

**还原指引**：脱敏 MD + 映射表 JSON 直接送既有还原工具 `POST /api/v1/vlmd/restore`（Issue#70/#50 T7；映射表格式与 #66 产物同构、兼容），即可还原原文——「换身份留关系」双向可逆。

## 6. MinerU sidecar 启停（16581）

独立 venv 常驻（MinerU 3.4，免 Docker 装法；`mineru[gradio]` 不带 extras 以保厂商 torch/vllm）：

```bash
# 启动（实例内，DCU 环境）
ssh <实例> 'source /opt/dtk/env.sh; export MINERU_MODEL_SOURCE=local http_proxy= https_proxy=; \
  nohup /root/.venvs/mineru/bin/mineru-api --host 127.0.0.1 --port 16581 > /root/mineru-api.log 2>&1 & sleep 8; \
  curl -s -m3 http://127.0.0.1:16581/docs -o /dev/null -w "%{http_code}\n"'   # 期望 200

# 停止
ssh <实例> 'pkill -f "mineru-api --host 127.0.0.1 --port 16581"'
```

`start_all_dcu.sh` 待加段落（照抄该脚本现有 5 服务段风格，**验收阶段执行**；自检行同步扩为 6 端口 +16581 `/docs`）：

```bash
echo "==> 6/6 MinerU sidecar (16581, 喂Agent模式 Issue#75)"
spawn mineru-api "source /opt/dtk/env.sh && export MINERU_MODEL_SOURCE=local http_proxy= https_proxy= && /root/.venvs/mineru/bin/mineru-api --host 127.0.0.1 --port 16581"
wait_health http://127.0.0.1:16581/docs "MinerU-API" 120
```

> 健康探针：`GET http://127.0.0.1:16581/docs` 回 200 即受理请求。sidecar 仅实例内 `127.0.0.1` 监听，勿公网暴露。

## 7. 变更记录

| 日期 | 变更 | 依据 |
|---|---|---|
| 2026-10-08 | 五端点 + sidecar 契约初版落库（Task 0 实测校准 + Tasks 1–10 落码） | Issue#75 |
