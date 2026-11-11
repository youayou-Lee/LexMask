# Copyright 2026 LexMask Contributors
"""E2E harness shared helpers.

House rules: ALWAYS headed real Chrome (never headless/bundled Chromium).
Target selected by env:
  E2E_BASE_URL   default http://localhost:8000  (tunnelled target instance -- currently cloud SCNet DCU scnet-main -- or local stack)
  E2E_USERNAME / E2E_PASSWORD   a regular (non-admin) account; created via
                                /auth/register on first run if missing.
"""
from __future__ import annotations

import os

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:8000").rstrip("/")
USERNAME = os.environ.get("E2E_USERNAME", "e2e_user")
PASSWORD = os.environ.get("E2E_PASSWORD", "E2eUser!2026")
SLOW_MO = int(os.environ.get("E2E_SLOWMO_MS", "0"))


def launch(p):
    return p.chromium.launch(channel="chrome", headless=False, slow_mo=SLOW_MO)


def ensure_account() -> None:
    """Register the E2E account if it does not exist yet (idempotent)."""
    import httpx

    # trust_env=False：否则 httpx 读 Windows 注册表系统代理（Mihomo）把
    # localhost 请求塞进代理返回空 502（老坑，见部署记忆）。
    with httpx.Client(base_url=BASE_URL, timeout=30.0, trust_env=False) as c:
        r = c.post("/api/v1/auth/login", json={"username": USERNAME, "password": PASSWORD})
        if r.status_code == 200:
            return
        r = c.post("/api/v1/auth/register", json={"username": USERNAME, "password": PASSWORD})
        if r.status_code != 200:
            raise RuntimeError(f"cannot login or register e2e account: {r.status_code} {r.text[:200]}")


def login(page: Page) -> None:
    page.goto(f"{BASE_URL}/auth")
    page.locator('input[autocomplete="username"]').fill(USERNAME)
    page.locator('input[type="password"]').first.fill(PASSWORD)
    page.locator('button[type="submit"]').first.click()
    page.wait_for_url(lambda url: "/auth" not in url, timeout=20_000)
    # Issue#262 二期收口：登录落 /single（sanitizeNextPath 默认），首次进入会弹右下角
    # 引导浮卡（OnboardingGuide，fixed z-40，localStorage 未记录完成时每个新浏览器
    # profile 必现，覆盖 /single 与 /batch）——实测会拦截页尾右侧控件的点击（识别
    # 范围入口等）。这里统一点掉「跳过引导」X（aside aria-labelledby 定位，locale
    # 无关）：同 profile 内 localStorage 置位后不再弹。浮卡未弹（旧部署/已完成引导）
    # 时 1.5s 超时直通，不影响既有脚本节奏。
    try:
        page.locator('aside[aria-labelledby="onboarding-title"] button').first.click(timeout=1_500)
    except PlaywrightTimeoutError:
        pass


def run(scenario) -> None:
    """Boilerplate: headed Chrome, one context, scenario(page), banner result."""
    ensure_account()
    with sync_playwright() as p:
        browser = launch(p)
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        try:
            scenario(page)
            print(f"E2E_PASS {scenario.__name__}")
        except Exception:
            art = os.path.join(os.path.dirname(__file__), ".artifacts")
            os.makedirs(art, exist_ok=True)
            page.screenshot(path=os.path.join(art, f"{scenario.__name__}_fail.png"), full_page=True)
            with open(
                os.path.join(art, f"{scenario.__name__}_fail.txt"), "w", encoding="utf-8"
            ) as fh:
                fh.write(page.url + "\n\n" + page.locator("body").inner_text())
            print(f"E2E_FAIL {scenario.__name__} -> artifacts in e2e/.artifacts/")
            raise
        finally:
            browser.close()
