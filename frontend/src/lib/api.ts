import type { components } from '../types/api'

export type HealthResponse = components['schemas']['HealthResponse']
export type ComponentStatus = components['schemas']['ComponentStatus']

// TODO(Phase 11): derive the base from ROOT_PATH for subpath reverse proxies.
const API_BASE = '/api'

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}

async function request<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`)
  if (!response.ok) {
    throw new ApiError(response.status, `${response.status} ${response.statusText}`)
  }
  return (await response.json()) as T
}

export function fetchHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/health')
}
