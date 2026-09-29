# CHANGELOG

## 2026-09-29

- fix(upload): 请求体上限中间件跟随 `MAX_FILE_SIZE`（0=非 JSON 不拦截，配置时 +10MB multipart 开销）——原 `MaxBodySizeMiddleware` 硬编码 60MB 与配置脱钩，是 #10 清理时漏网的第六处限制（不经 settings、grep 不可见），用户以 100MB 实测单文件整包上传秒 413 暴露；JSON 1MB 上限不变，断点续传 5MB 分块路径本就不受影响。实例实测：本机回环整包 100MB→200(0.75s)、隧道分块 100MB 全链路→complete 200；新增中间件三分支单测（#27）。
- docs(workflow): 恢复 preview 分支线（#20 方案 B，用户拍板）——§7 分支模型改为 功能分支 base=preview → squash 合入 → 晋级 main 打 tag；Step 3/5/6/7、§5 命令、门禁规范 §0⑥/§5 措辞同步；AGENTS.md、协同开发规范对齐；ci.yml base-guard 即按此模型执行，无需改动。preview 分支已自 main(2655be7) 建立（#20，PR #22）。
- docs(workflow): 设计阶段(Step 2)新增「验收方案(谁来验收:AI 自验收/人工)」「影响面/环境计划(只起影响面内服务+端口段规划)」必填项;门禁规范 §0/§1/§4/§5 配套修订验收人分级——AI 自验收(浏览器/API 实测逐条留证)通过后直接进独立 review,人工验收流程与 merge 放行权不变,未写方案按人工从严(#16,PR #19)。
- feat(upload): 去除单文件 50MB 上传限制——`MAX_FILE_SIZE` 默认 0=不限制（环境变量可恢复上限），整包/断点续传/inbox/SFTP/DICOM 各校验点改为「配置上限才校验」（顺修 DICOM `max(1,0)` 把不限量卡成 1 字节的陷阱），前端 playground/batch 两处 dropzone `maxSize` 与 50MB 文案移除，断点续传单测改「默认不限+可配上限」双例，用户文档与 .env 注释同步（#10，PR #11）。
- fix(cloud-deploy): bootstrap.sh 在拉起 frontend 前补 dist 构建守卫——同步部署（bundle clone，镜像保存点无 dist）后 vite preview 对空目录起服务，静态页整站 404 而 /health 走代理仍 200，健康检查全绿掩盖问题（#7，PR #8）。实例实测：构建后 / 由 404 恢复 200；构建日志落 `$LOG/frontend-build.log`。DCU 实例持久卷 `start_all_dcu.sh` 同步打同款守卫（私有运维件）。
- fix(cloud-deploy): 清除 4 个脚本的行首 `\x01` 脏字符（start_cloud.sh #14/PR #15；setup_cloud.sh、setup_dtk.sh、start_dtk.sh #17/PR #18）——行首 `\x01` 使 `#` 失去注释作用，运行时报 command not found。全目录扩展口径控制符扫描（C0/DEL/C1/零宽/双向/BOM/NBSP）零命中。
