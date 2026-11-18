// Copyright 2026 LexMask Contributors

// Issue#355：vitest 环境默认钉 classic 主题。
// 存量测试全部编写于双轨主题之前，钉的是 classic 产品行为（页签计数列、
// 无 ds 指标条/标题区/面包屑等）；ds 专属行为由各自的 ds 测试显式切换
// （useUiTheme.setState 或覆写 localStorage）验证。
// ui-theme.test 在 beforeEach 清 localStorage，不受本钉影响。
import { STORAGE_KEYS } from './constants/storage-keys';

// node 环境测试文件（纯逻辑）没有 localStorage，也用不到主题；只钉 jsdom 环境。
if (typeof localStorage !== 'undefined') {
  localStorage.setItem(STORAGE_KEYS.UI_THEME, 'classic');
}
