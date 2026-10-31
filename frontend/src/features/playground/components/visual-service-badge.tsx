import { type FC } from 'react';
import { Badge } from '@/components/ui/badge';
import { useT } from '@/i18n';

/**
 * Issue#217 PR②：视觉识别项旁的服务状态徽标（三态）。
 *
 * - status 处于存活档（online/busy/...）⇒ 不渲染，界面保持干净；
 * - status 明确非存活（offline）⇒ 渲染「服务未启动」徽标（琥珀色，附怎么开）；
 * - status 未知（undefined/null，健康快照尚未拉到）⇒ 不渲染——避免加载期闪现误报。
 *
 * 语义边界：这是「拉取那一刻」的快照，任务提交时后端预检兜底（fail-closed），
 * 两层知情互不替代。
 */
export const VisualServiceBadge: FC<{ status?: string | null }> = ({ status }) => {
  const t = useT();
  // 三态语义（评审 M5 补）：status 值域=online/degraded/offline（/health/services）。
  // degraded = 可达但未就绪/CPU 回退风险（LA 正在加载中）——不是「未启动」，不渲染
  // 此徽标（侧边栏与横幅的 affectedServices 已如实反映降级）；只有明确 offline 才亮标。
  if (status !== 'offline') {
    return null;
  }
  return (
    <Badge
      variant="outline"
      className="rounded-full border-amber-500/60 bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-700 shadow-none dark:text-amber-400"
      data-testid="playground-visual-service-down"
      title={`${t('playground.visualServiceDown')} — ./start_local.sh --with-visual / --visual cloud`}
    >
      {t('playground.visualServiceDown')}
    </Badge>
  );
};
