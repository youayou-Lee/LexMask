// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// 2026-11-16 用户实测缺陷回归：「手拉框后导出没包含手改结果」。
//
// 根因：编辑页底部「逐页结果与选页导出」按钮打开 PlaygroundExportDialog，弹窗里没有
// 把宿主画布框（含 source=manual 的手动框）透传给 PlaygroundPageExport ⇒ 导出请求
// 少了 bounding_boxes，后端只能回落到草稿/file_store ⇒ 手动框丢失。
// 结果页顶部那条链路本来就传，本文件钉住弹窗这条也必须传。

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('./playground-page-export', () => ({
  PlaygroundPageExport: (props: { boundingBoxes?: unknown }) => (
    <div
      data-testid="page-export-stub"
      data-boxes={props.boundingBoxes ? JSON.stringify(props.boundingBoxes) : 'absent'}
    />
  ),
}));

import { PlaygroundExportDialog } from './playground-export-dialog';

afterEach(cleanup);

describe('PlaygroundExportDialog 框账透传（2026-11-16 手拉框导出）', () => {
  it('boundingBoxes 透传到选页导出块（含手动框）', () => {
    const boxes = [
      {
        id: 'manual_1',
        page: 1,
        x: 0.1,
        y: 0.2,
        width: 0.3,
        height: 0.1,
        type: 'CUSTOM',
        selected: true,
      },
    ];
    render(
      <PlaygroundExportDialog open onOpenChange={vi.fn()} fileId="file-1" boundingBoxes={boxes} />,
    );

    const stub = screen.getByTestId('page-export-stub');
    expect(stub.getAttribute('data-boxes')).toBe(JSON.stringify(boxes));
  });

  it('未传 boundingBoxes 时不伪造空数组（空数组=用户清空所有框，语义不同）', () => {
    render(<PlaygroundExportDialog open onOpenChange={vi.fn()} fileId="file-1" />);

    expect(screen.getByTestId('page-export-stub').getAttribute('data-boxes')).toBe('absent');
  });
});