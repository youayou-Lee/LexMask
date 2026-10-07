// Copyright 2026 LexMask Contributors

import { post } from '@/services/api-client';
import type { RestoreRequestBody } from './lib/request';
import type { RestoreResponse } from './lib/report-shape';

/** T7 还原 API（Issue#70/#50，require_auth，cookie+CSRF 由 api-client 统一带） */
export function restoreText(body: RestoreRequestBody): Promise<RestoreResponse> {
  return post<RestoreResponse>('/vlmd/restore', body);
}
