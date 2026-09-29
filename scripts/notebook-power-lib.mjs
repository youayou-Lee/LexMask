// 超算互联网 Notebook 开关机公共模块（公开版）
// 凭据与站点 URL 不进仓：运行前把 NOTEBOOK_POWER_CONF 环境变量指向本地 JSON 配置文件
// （格式见 docs/notebook-自动开关机.md）：{ "url": "...", "username": "...", "password": "...", "instance": "..." }
// 全程使用 DOM 定位 + 运行时计算的元素坐标，不依赖写死的视口坐标，窗口大小不影响结果。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname } from "node:path";
import { chromium } from "playwright";

const __dirname = dirname(fileURLToPath(import.meta.url));

function loadConf() {
  const confPath = process.env.NOTEBOOK_POWER_CONF;
  if (!confPath) {
    throw new Error(
      "缺少配置：请设置环境变量 NOTEBOOK_POWER_CONF 指向本地凭据 JSON（格式见 docs/notebook-自动开关机.md），凭据不要提交进仓。",
    );
  }
  const conf = JSON.parse(readFileSync(confPath, "utf8"));
  if (!conf.url || !conf.username || !conf.password) {
    throw new Error("配置不完整：至少需要 url / username / password 三个字段。");
  }
  return conf;
}

const log = (m) => console.log(`[${new Date().toISOString()}] ${m}`);

// action: "start"（开机）| "stop"（关机）；instanceName 缺省取配置里的 instance，再缺省 main
export async function run(action, instanceName) {
  const conf = loadConf();
  const INSTANCE = instanceName || conf.instance || "main";
  // 已关机 → 点 instance-start 图标；开机中/运行中 → 点 instance-shut-down 图标
  const iconPrefix = action === "start" ? "instance-start" : "instance-shut-down";
  const expectStatus = action === "start" ? /开机中|运行中/ : /已关机|关机中/;
  const confirmText = action === "start" ? "确认开机" : "确认关机";
  const bootingStatus = /开机中/;

  const browser = await chromium.launch({ headless: true });
  try {
    // locale 必须用 zh-CN：登录页按浏览器语言渲染中/英文，默认 en 会拿到英文文案
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, locale: "zh-CN" });
    log(`打开 ${conf.url}`);
    await page.goto(conf.url, { waitUntil: "domcontentloaded" });

    // 等登录表单或已登录列表出现（SSO 需要若干跳重定向）
    const userInput = page.getByPlaceholder(/请输入用户名\/邮箱|Enter account name\/email/);
    await userInput.waitFor({ state: "visible", timeout: 30000 }).catch(() => {});
    if (await userInput.count()) {
      log("填写账号密码登录");
      await userInput.fill(conf.username);
      await page.getByPlaceholder(/请输入密码|Enter password/).fill(conf.password);
      await page.getByRole("button", { name: /登录|Login/ }).click();
      // 验证码兜底：若出现图形验证码，无人值守无法处理，立即报错
      const captcha = page.getByPlaceholder(/请输入图形验证码|captcha/i);
      if (await captcha.isVisible().catch(() => false)) {
        throw new Error("登录触发了图形验证码，请人工登录一次后再试");
      }
    }
    // SSO 重定向链不可靠 waitForURL，直接等 Notebook 列表渲染
    // state=attached：前端组件会渲染隐藏的克隆表格，visible 判定会误报
    await page.waitForSelector("text=资源组/实例名称", { state: "attached", timeout: 90000 });
    log("已进入 Notebook 列表");

    // 目标实例行（含 "<实例名> ID:" 的详情行）。前端会渲染多份克隆表格
    // （主表/左固定/右固定），操作列图标只在右固定表中真正可见。
    // 图标是内联 svg，id 不在 <svg> 上而在内部 clipPath：svg [id^="instance-start|instance-shut-down"]
    const rowRe = new RegExp(`\\s${INSTANCE}\\s+ID:`);
    // 列表数据异步加载，轮询查找行 + 图标，最长 30s
    let rowInfo = null;
    for (let i = 0; i < 10; i++) {
      rowInfo = await page.evaluate(
        ({ re, iconPrefix }) => {
          const re2 = new RegExp(re.re);
          const rows = Array.from(document.querySelectorAll("table.el-table__body tr"));
          const row = rows.find(
            (r) =>
              re2.test(r.textContent) &&
              r.querySelector(`a.el-link svg [id^="${iconPrefix}"]`),
          );
          if (!row) {
            const any = rows.find((r) => re2.test(r.textContent));
            return {
              found: false,
              text: any ? any.textContent.replace(/\s+/g, " ").slice(0, 160) : "",
            };
          }
          const anchor = row
            .querySelector(`a.el-link svg [id^="${iconPrefix}"]`)
            .closest("a.el-link");
          if (!(anchor.offsetParent || anchor.getClientRects().length)) {
            return { found: true, visible: false };
          }
          // 该站 DOM click（anchor.click()/locator.click()）触发不了 Vue 处理器，
          // 必须真实鼠标事件：滚动到视口内后取包围盒中心。坐标运行时计算，与窗口大小无关。
          anchor.scrollIntoView({ block: "center" });
          const b = anchor.getBoundingClientRect();
          return {
            found: true,
            visible: true,
            x: b.x + b.width / 2,
            y: b.y + b.height / 2,
            text: row.textContent.replace(/\s+/g, " ").slice(0, 120),
          };
        },
        { re: { re: rowRe.source }, iconPrefix },
      );
      if (rowInfo.found || rowInfo.text) break;
      await page.waitForTimeout(3000);
    }
    if (!rowInfo.found) {
      if (!rowInfo.text) throw new Error(`列表中找不到实例 ${INSTANCE}`);
      if (expectStatus.test(rowInfo.text)) {
        log(`${INSTANCE} 已处于目标状态，无需操作`);
        return 0;
      }
      if (action === "stop" && bootingStatus.test(rowInfo.text)) {
        log(`${INSTANCE} 仍在开机中（镜像拉取），此时不能关机`);
        return 1;
      }
      log(`未找到 ${INSTANCE} 的${action === "start" ? "启动" : "关机"}图标。行信息: ${rowInfo.text}`);
      return 1;
    }
    if (!rowInfo.visible) {
      log(`警告: ${INSTANCE} 的图标存在但不可见（页面布局异常）`);
      return 1;
    }
    log(`点击 ${INSTANCE} 的${action === "start" ? "启动" : "关机"}图标`);
    await page.mouse.click(rowInfo.x, rowInfo.y);

    // 确认弹窗：正文含「当前实例【XX】…确认开机/关机？」，点「确认」
    const dialog = page.locator(".el-dialog", { hasText: confirmText });
    await dialog.waitFor({ state: "visible", timeout: 15000 });
    log("确认弹窗已出现，点击「确认」");
    await dialog.getByRole("button", { name: "确认" }).click();

    await page.waitForSelector("text=操作成功", { timeout: 20000 });
    log("操作成功，等待状态刷新");

    // 状态轮询，最长 60s
    for (let i = 0; i < 12; i++) {
      await page.waitForTimeout(5000);
      const rowText = await page.evaluate((reSrc) => {
        const re = new RegExp(reSrc);
        const row = Array.from(document.querySelectorAll("table.el-table__body tr")).find((r) =>
          re.test(r.textContent),
        );
        return row ? row.textContent : "";
      }, rowRe.source);
      const m = rowText.match(/开机中|运行中|已关机|关机中/);
      if (m && expectStatus.test(m[0])) {
        log(`${INSTANCE} 状态已更新: ${m[0]}`);
        return 0;
      }
    }
    log(`警告: 60s 内未确认 ${INSTANCE} 达到目标状态，请人工检查`);
    return 1;
  } catch (err) {
    log(`失败: ${err.message}`);
    return 1;
  } finally {
    await browser.close();
  }
}
