# Notebook 自动开关机脚本

通过 Playwright 无头浏览器自动登录超算互联网控制台，对指定 Notebook 实例执行**开机**或**关机**，适合配 cron/定时任务做每日自动开机、下班自动关机，避免实例空转计费或占用配额。

- 脚本位置：`scripts/notebook-power-start.mjs`（开机）、`scripts/notebook-power-stop.mjs`（关机），公共逻辑在 `scripts/notebook-power-lib.mjs`。
- 已在控制台实测通过（账号密码登录 → 实例列表 → 启动/关机图标 → 确认弹窗 → 状态校验）。

## 1. 安装依赖

```bash
cd scripts
npm install playwright
npx playwright install chromium
```

要求 Node.js ≥ 18。

## 2. 凭据配置（不要提交进仓）

在**仓外**任意位置建一个 JSON 文件（如 `~/.notebook-power/login.json`）：

```json
{
  "url": "https://<你的控制台地址>/ui/console/index.html#/notebook",
  "username": "<你的手机号/邮箱>",
  "password": "<你的密码>",
  "instance": "main"
}
```

`url` 填控制台 Notebook 列表页地址（云服务商官网，此处不硬编码）。默认操作实例为 `instance` 字段指定的名称（缺省 `main`），运行时也可用命令行参数覆盖。

> ⚠️ 该文件含明文密码，请放在私有目录并设置 `chmod 600`。**不要**提交到本仓。

## 3. 使用方式

```bash
export NOTEBOOK_POWER_CONF=~/.notebook-power/login.json   # 必须设置，指向上面的凭据文件

node scripts/notebook-power-start.mjs          # 开机，默认操作配置里的 instance（缺省 main）
node scripts/notebook-power-stop.mjs           # 关机，同上
node scripts/notebook-power-start.mjs BW       # 显式指定实例名（列表里"资源组/实例名称"一列的名称）
```

每次运行输出带时间戳的日志，退出码：`0` 成功（含幂等跳过），`1` 失败/需人工检查。

### 幂等与保护逻辑

- 实例已处于目标状态（如已在运行时再开机）→ 打印「已处于目标状态，无需操作」，退出 0，**不会重复操作**。
- 关机时若实例仍在「开机中（镜像拉取）」→ 拒绝操作并退出 1（平台此时不允许关机）。
- 登录触发图形验证码 → 立即报错退出，请人工在浏览器登录一次后再试。

### 定时任务示例

crontab（工作日 07:50 自动开机、20:00 自动关机）：

```cron
50 7 * * 1-5 NOTEBOOK_POWER_CONF=/home/me/.notebook-power/login.json node /path/to/repo/scripts/notebook-power-start.mjs >> /tmp/notebook-start.log 2>&1
0 20 * * 1-5 NOTEBOOK_POWER_CONF=/home/me/.notebook-power/login.json node /path/to/repo/scripts/notebook-power-stop.mjs >> /tmp/notebook-stop.log 2>&1
```

## 4. 实现要点（踩坑记录，改代码前必读）

1. **locale 必须是 `zh-CN`**：登录页按浏览器语言渲染中/英文，无头默认英文会导致占位符匹配失败。
2. **不能用 DOM click**：该站（Element Plus + Vue）对 `anchor.click()` / Playwright `locator.click()` 无响应，必须用真实鼠标事件（`page.mouse.click`），坐标通过 `scrollIntoView` + `getBoundingClientRect` 运行时计算，**不要写死视口坐标**。
3. 操作图标是内联 SVG，`id` 不在 `<svg>` 元素上而在内部 `clipPath` 上：选择器为 `svg [id^="instance-start"]`（启动）/ `svg [id^="instance-shut-down"]`（关机）。
4. Element Plus 表格会渲染 3 份克隆（主表/左固定/右固定），操作列图标只在右固定列那份真正可见；列表头元素 `visible` 判定会因隐藏克隆误报，等待用 `state: "attached"`。
5. SSO 登录有多跳重定向，`waitForURL` 匹配 hash 路由不可靠，直接等列表渲染（`text=资源组/实例名称`）。
6. 列表页右下角 Tips 浮窗是 iframe 组件，不在主文档、无法 DOM 关闭；因使用运行时坐标的真实鼠标点击，浮窗遮挡不影响操作。

## 5. 已知限制

- 平台前端改版（图标 id、弹窗文案、登录页结构变化）会使选择器失效，届时需按 §4 重新定位。
- 不支持需要图形/短信验证码的无人值守登录。
- 关机确认弹窗的「保存环境」选项维持默认，不额外配置。
