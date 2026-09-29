# CHANGELOG

## 2026-09-29

- fix(cloud-deploy): bootstrap.sh 在拉起 frontend 前补 dist 构建守卫——同步部署（bundle clone，镜像保存点无 dist）后 vite preview 对空目录起服务，静态页整站 404 而 /health 走代理仍 200，健康检查全绿掩盖问题（#7，PR #8）。实例实测：构建后 / 由 404 恢复 200；构建日志落 `$LOG/frontend-build.log`。DCU 实例持久卷 `start_all_dcu.sh` 同步打同款守卫（私有运维件）。
