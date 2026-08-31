import type { components } from '../types/api'

type Schemas = components['schemas']

export type HealthResponse = Schemas['HealthResponse']
export type ComponentStatus = Schemas['ComponentStatus']
export type ProviderStatus = Schemas['ProviderStatus']
export type Sport = Schemas['SportOut']
export type League = Schemas['LeagueOut']
export type Fixture = Schemas['FixtureOut']
export type FixturePage = Schemas['FixturePage']
export type FixtureStatus = Schemas['FixtureStatus']
export type ProviderInfo = Schemas['ProviderInfo']
export type ProviderHealth = Schemas['ProviderHealth']
export type JobRun = Schemas['JobRun']
export type ScheduledJob = Schemas['ScheduledJob']
export type ResolutionQueueItem = Schemas['QueueItemOut']
export type BuilderResponse = Schemas['BuilderResponse']
export type TierOut = Schemas['TierOut']
export type TrackedBet = Schemas['TrackedBetOut']
export type TrackBetIn = Schemas['TrackBetIn']
export type Metrics = Schemas['MetricsResponse']
export type Prediction = Schemas['PredictionOut']
export type BacktestResult = Schemas['BacktestOut']
export type BacktestRequest = Schemas['BacktestRequest']

// TODO(Phase 11): derive the base from ROOT_PATH for subpath reverse proxies.
const API_BASE = '/api'

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public detail?: string,
  ) {
    super(message)
  }
}

type Query = Record<string, string | number | boolean | undefined | null>

function buildUrl(path: string, query?: Query): string {
  const url = new URL(`${API_BASE}${path}`, window.location.origin)
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, String(value))
    }
  }
  return `${url.pathname}${url.search}`
}

async function request<T>(path: string, init?: RequestInit & { query?: Query }): Promise<T> {
  const { query, ...rest } = init ?? {}
  const response = await fetch(buildUrl(path, query), rest)
  if (!response.ok) {
    let detail: string | undefined
    try {
      detail = (await response.json())?.detail
    } catch {
      // non-JSON error body; the status line is all we have
    }
    throw new ApiError(response.status, detail ?? `${response.status} ${response.statusText}`, detail)
  }
  return (await response.json()) as T
}

export const fetchHealth = () => request<HealthResponse>('/health')
export const fetchSports = () => request<Sport[]>('/sports')

export const fetchLeagues = (sport?: string) =>
  request<League[]>('/leagues', { query: { sport } })

export interface FixtureFilters {
  start?: string
  end?: string
  sport?: string
  league?: string
  status?: string
  team_id?: number
}

export const fetchFixtures = (filters: FixtureFilters) =>
  request<FixturePage>('/fixtures', { query: filters as Query })

export const fetchFixture = (id: number) => request<Fixture>(`/fixtures/${id}`)

export const fetchProviders = () => request<ProviderInfo[]>('/providers')

export const testProvider = (name: string) =>
  request<ProviderHealth>(`/providers/${encodeURIComponent(name)}/test`, { method: 'POST' })

export const fetchJobRuns = (limit = 25) =>
  request<JobRun[]>('/jobs/runs', { query: { limit } })

export const fetchJobSchedule = () => request<ScheduledJob[]>('/jobs/schedule')

export const runJob = (id: string) =>
  request<{ status: string; job: string }>(`/jobs/${id}/run`, { method: 'POST' })

export const fetchResolutionQueue = () =>
  request<ResolutionQueueItem[]>('/resolution/queue')

export const resolveQueueItem = (id: number, entityId: number) =>
  request<ResolutionQueueItem>(`/resolution/queue/${id}/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entity_id: entityId }),
  })

export const ignoreQueueItem = (id: number) =>
  request<ResolutionQueueItem>(`/resolution/queue/${id}/ignore`, { method: 'POST' })

export const fetchBuilder = (params: { hours_ahead?: number; sport?: string; league?: string }) =>
  request<BuilderResponse>('/bets/builder', { query: params as Query })

export const fetchTrackedBets = (status?: string) =>
  request<TrackedBet[]>('/bets', { query: { status } })

export const trackBet = (body: TrackBetIn) =>
  request<TrackedBet>('/bets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

export const fetchMetrics = () => request<Metrics>('/tracker/metrics')

export const runSettlement = () =>
  request<{ predictions: number; bets: number }>('/tracker/settle', { method: 'POST' })

export const fetchPrediction = (fixtureId: number) =>
  request<Prediction>(`/fixtures/${fixtureId}/prediction`)

export const runBacktest = (body: BacktestRequest) =>
  request<BacktestResult>('/backtest/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
