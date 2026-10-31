// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { VisualServiceBadge } from './visual-service-badge';

/**
 * Issue#217 PR②：识别项旁服务状态徽标的三态钉子。
 * 用户裁定：上传页就要看见服务在不在——在线时保持干净、未启动时亮标、
 * 快照未拉到时不闪误报。
 */
describe('VisualServiceBadge（Issue#217 三态）', () => {
  it('在线（live 档 status）⇒ 不渲染', () => {
    const container = render(<VisualServiceBadge status="online" />).container;
    expect(container.querySelector('[data-testid="playground-visual-service-down"]')).toBeNull();
  });

  it('degraded（可达但未就绪）⇒ 不渲染（不是「未启动」，评审 M5）', () => {
    const { container } = render(<VisualServiceBadge status="degraded" />);
    expect(container.querySelector('[data-testid="playground-visual-service-down"]')).toBeNull();
  });

  it.each(['online'])('存活档 %s 同样不渲染', (status) => {
    const container = render(<VisualServiceBadge status={status} />).container;
    expect(container.querySelector('[data-testid="playground-visual-service-down"]')).toBeNull();
  });

  it('未启动（offline）⇒ 渲染徽标且带指引 title', () => {
    render(<VisualServiceBadge status="offline" />);
    const badge = screen.getByTestId('playground-visual-service-down');
    expect(badge).not.toBeNull();
    expect(badge.textContent).toContain('服务未启动');
    expect(badge.getAttribute('title')).toContain('--with-visual');
  });

  it('未知（undefined / null，快照未拉到）⇒ 不渲染', () => {
    const c1 = render(<VisualServiceBadge status={undefined} />).container;
    const c2 = render(<VisualServiceBadge status={null} />).container;
    expect(c1.querySelector('[data-testid="playground-visual-service-down"]')).toBeNull();
    expect(c2.querySelector('[data-testid="playground-visual-service-down"]')).toBeNull();
  });
});
