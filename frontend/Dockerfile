FROM node:24-alpine AS builder

WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:alpine

# Use auto-detected worker count instead of the default 1.
RUN sed -i 's/worker_processes\s*1/worker_processes auto/' /etc/nginx/nginx.conf

# Issue #29：nginx.conf 模板化——client_max_body_size 在容器启动时由
# /docker-entrypoint.d/15-set-body-size.envsh（被 entrypoint source，
# export 才能传给 envsubst）依据后端 MAX_FILE_SIZE 计算，再由官方镜像
# 自带的 20-envsubst-on-templates.sh 渲染为本文件。
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY docker-entrypoint.d/15-set-body-size.envsh /docker-entrypoint.d/15-set-body-size.envsh
COPY --from=builder /app/dist /usr/share/nginx/html

# Create a non-root user and fix permissions for nginx runtime directories.
# conf.d 需对 appuser 可写：20-envsubst-on-templates.sh 以容器用户（appuser）
# 身份在启动时把渲染结果写入 /etc/nginx/conf.d/default.conf。
RUN adduser -D -u 1001 appuser && \
    chown -R appuser:appuser /usr/share/nginx/html /var/cache/nginx /var/log/nginx /etc/nginx/conf.d && \
    touch /var/run/nginx.pid && chown appuser:appuser /var/run/nginx.pid

# Non-privileged port for the non-root nginx worker.
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget -qO /dev/null http://localhost:8080/ || exit 1

USER appuser

CMD ["nginx", "-g", "daemon off;"]
