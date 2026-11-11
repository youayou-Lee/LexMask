// @vitest-environment jsdom

// Issue#262 二期反馈（Task 12b）：识别项设置列表搜索。
// 80+ 类型靠翻页找效率低 → 列表卡片头部加搜索框：显示名 OR type id、大小写不敏感
// substring；搜索态忽略分页（全部命中一次性渲染 + 匹配计数）、清空恢复分页与原页码。
// 惯例同 entity-type-list.clamp-tooltip.test.tsx：全站 vitest 默认 node，逐文件切 jsdom。

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useI18n } from '@/i18n';
import type { EntityTypeConfig } from '../hooks/use-entity-types';
import { EntityTypeList } from './entity-type-list';

/** jsdom 无 ResizeObserver；useTruncated 有 undefined 兜底，给哑实现即可。 */
class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// 10 个自定义类型共享「合同」关键词（搜索态不分页断言需要 >9 个命中）
const CUSTOM_TEN: EntityTypeConfig[] = Array.from({ length: 10 }, (_, i) => ({
  id: `custom_p${i + 1}`,
  name: `合同条款${i + 1}`,
  color: '#000000',
}));

// 12 个类型 = 2 系统（ID_CARD→身份证号 / PERSON→姓名，走 getEntityTypeName）
// + 10 自定义（显示名 = type.name）→ 非搜索态 9/页共 2 页
const TYPES: EntityTypeConfig[] = [
  { id: 'ID_CARD', name: 'id_card', color: '#000000' },
  { id: 'PERSON', name: 'person', color: '#000000' },
  ...CUSTOM_TEN,
];

function renderList(types: EntityTypeConfig[] = TYPES) {
  const handlers = { onEdit: vi.fn(), onDelete: vi.fn(), onAdd: vi.fn(), onReset: vi.fn() };
  const { container } = render(
    <EntityTypeList types={types} variant="llm" onOverrideChange={vi.fn()} {...handlers} />,
  );
  return { handlers, container };
}

const searchBox = () => screen.getByTestId('settings-entity-search') as HTMLInputElement;
const cardCount = (container: HTMLElement) => container.querySelectorAll('article').length;
const hasCard = (id: string) => screen.queryByTestId(`edit-type-${id}`) !== null;
const gotoNextPage = () =>
  fireEvent.click(
    within(screen.getByTestId('pagination-rail')).getByRole('button', { name: '下一页' }),
  );

beforeEach(() => {
  useI18n.setState({ locale: 'zh' });
  vi.stubGlobal('ResizeObserver', FakeResizeObserver);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Task 12b：识别项设置列表搜索', () => {
  it('搜索框存在（testid + aria-label）；非搜索态仍分页 9/页、第2页看剩余 3 个', () => {
    const { container } = renderList();
    expect(searchBox()).not.toBeNull();
    expect(searchBox().getAttribute('aria-label')).toBe('搜索识别项');
    expect(cardCount(container)).toBe(9);
    expect(screen.queryByTestId('edit-type-custom_p8')).toBeNull();
    expect(screen.getByTestId('pagination-rail')).not.toBeNull();

    gotoNextPage();
    expect(cardCount(container)).toBe(3);
    expect(hasCard('custom_p8')).toBe(true);
    expect(hasCard('ID_CARD')).toBe(false);
  });

  it('搜显示名「身份」→ 只剩身份证号卡片 + 匹配计数 + 分页控件隐藏', () => {
    const { container } = renderList();
    fireEvent.change(searchBox(), { target: { value: '身份' } });

    expect(hasCard('ID_CARD')).toBe(true);
    expect(hasCard('PERSON')).toBe(false);
    expect(hasCard('custom_p1')).toBe(false);
    // 搜索态不分页：命中数 < 9 也全部渲染（此处 1 = 全部命中）
    expect(cardCount(container)).toBe(1);
    expect(container.textContent).toContain('匹配 1 项');
    expect(screen.queryByTestId('pagination-rail')).toBeNull();
  });

  it('搜 id 小写「person」→ 命中 PERSON（id 匹配、大小写不敏感）', () => {
    const { container } = renderList();
    fireEvent.change(searchBox(), { target: { value: 'person' } });

    expect(hasCard('PERSON')).toBe(true);
    expect(hasCard('ID_CARD')).toBe(false);
    expect(cardCount(container)).toBe(1);
    expect(container.textContent).toContain('匹配 1 项');
  });

  it('搜索态不分页：10 个命中一次性全部渲染（非搜索态需翻 2 页才看全）', () => {
    const { container } = renderList();
    fireEvent.change(searchBox(), { target: { value: '合同' } });

    expect(cardCount(container)).toBe(10);
    for (let i = 1; i <= 10; i += 1) {
      expect(hasCard(`custom_p${i}`)).toBe(true);
    }
    expect(container.textContent).toContain('匹配 10 项');
  });

  it('从第 2 页发起搜索 → 清空恢复分页且回到第 2 页（原页码保持）', () => {
    const { container } = renderList();
    gotoNextPage();
    expect(hasCard('custom_p8')).toBe(true);

    fireEvent.change(searchBox(), { target: { value: '身份' } });
    expect(screen.queryByTestId('pagination-rail')).toBeNull();

    fireEvent.change(searchBox(), { target: { value: '' } });
    expect(screen.getByTestId('pagination-rail')).not.toBeNull();
    // 恢复的是搜索前的第 2 页（custom_p8 在第 2 页）
    expect(hasCard('custom_p8')).toBe(true);
    expect(hasCard('ID_CARD')).toBe(false);
    expect(cardCount(container)).toBe(3);
  });

  it('0 命中 → 「无匹配」空态 + 匹配 0 项；清空后恢复列表', () => {
    const { container } = renderList();
    fireEvent.change(searchBox(), { target: { value: '不存在的类型词' } });

    expect(cardCount(container)).toBe(0);
    expect(container.textContent).toContain('无匹配');
    expect(container.textContent).toContain('匹配 0 项');

    fireEvent.change(searchBox(), { target: { value: '' } });
    expect(cardCount(container)).toBe(9);
    expect(container.textContent).not.toContain('无匹配');
  });

  it('compact 变体同样可用：搜索过滤 + 匹配计数生效', () => {
    const { container } = render(
      <EntityTypeList
        types={TYPES}
        variant="llm"
        compact
        onEdit={vi.fn()}
        onDelete={vi.fn()}
        onAdd={vi.fn()}
        onReset={vi.fn()}
      />,
    );
    fireEvent.change(searchBox(), { target: { value: 'person' } });
    expect(hasCard('PERSON')).toBe(true);
    expect(cardCount(container)).toBe(1);
    expect(container.textContent).toContain('匹配 1 项');
    expect(screen.queryByTestId('pagination-rail')).toBeNull();
  });
});
