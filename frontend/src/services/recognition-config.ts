// Copyright 2026 LexMask Contributors

import { fetchWithTimeout } from '@/utils/fetchWithTimeout';
import { fetchPresets, type RecognitionPreset } from './presetsApi';
import type {
  EntityTypeConfig as PlaygroundEntityType,
  PipelineConfig as PlaygroundPipeline,
} from '@/features/playground/types';

export type RecognitionEntityType = PlaygroundEntityType;
export type RecognitionPipeline = PlaygroundPipeline;

// Issue#267：目录接口超时族单一来源（权威定义点）。目录接口经远程隧道单程可达数百
// 毫秒，短超时会间歇性 abort——各消费方统一 25s：快在缓存，不靠掐超时。
// 消费面：use-entity-types / use-redaction-presets / use-playground-recognition
// （三 hook 原 1.2s/3.5s/本地 25s 定义均已改 import）与 word-pools 类型名 effect。
// 服务层默认参数 3_500 保持不动（ blast radius 零），显式传参方一律用本常量；
// 批量向导 use-batch-config 同接口传 25s/10s 字面量，属既有口径不在本 issue 动。
export const RECOGNITION_FETCH_TIMEOUT_MS = 25_000;

export async function fetchRecognitionEntityTypes(
  enabledOnly: boolean,
  timeoutMs = 3_500,
): Promise<RecognitionEntityType[]> {
  const res = await fetchWithTimeout(`/api/v1/custom-types?enabled_only=${enabledOnly}`, {
    timeoutMs,
  });
  if (!res.ok) {
    throw new Error('Failed to load entity types');
  }
  const data = await res.json().catch(() => ({}));
  return Array.isArray(data?.custom_types) ? data.custom_types : [];
}

export async function fetchRecognitionPipelines(timeoutMs = 3_500): Promise<RecognitionPipeline[]> {
  const res = await fetchWithTimeout('/api/v1/vision-pipelines', { timeoutMs });
  if (!res.ok) {
    throw new Error('Failed to load vision pipelines');
  }
  const data = await res.json().catch(() => []);
  return Array.isArray(data) ? data : [];
}

export async function fetchRecognitionPresets(): Promise<RecognitionPreset[]> {
  return fetchPresets();
}
