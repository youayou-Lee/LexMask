# CHANGELOG

## 2026-09-29

- fix(cloud-deploy): bootstrap.sh 在拉起 frontend 前补 dist 构建守卫——同步部署（bundle clone，镜像保存点无 dist）后 vite preview 对空目录起服务，静态页整站 404 而 /health 走代理仍 200，健康检查全绿掩盖问题（#7，PR #8）。实例实测：构建后 / 由 404 恢复 200；构建日志落 `$LOG/frontend-build.log`。DCU 实例持久卷 `start_all_dcu.sh` 同步打同款守卫（私有运维件）。
- fix(cloud-deploy): 清除 4 个脚本的行首 `\x01` 脏字符（start_cloud.sh #14/PR #15；setup_cloud.sh、setup_dtk.sh、start_dtk.sh #17/PR #18）——行首 `\x01` 使 `#` 失去注释作用，运行时报 command not found。全目录扩展口径控制符扫描（C0/DEL/C1/零宽/双向/BOM/NBSP）零命中。
