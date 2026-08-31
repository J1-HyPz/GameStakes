import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { HealthResponse, Sport } from '../lib/api'
import Dashboard from '../pages/Dashboard'

const healthFixture: HealthResponse = {
  status: 'ok',
  app: 'GameStakes',
  version: '0.1.0',
  database: { status: 'up', detail: null },
  redis: { status: 'disabled', detail: 'REDIS_URL not configured' },
  providers: [],
}

const sportsFixture: Sport[] = [
  { id: 1, slug: 'football', name: 'Football', kind: 'team', league_count: 20 },
  { id: 5, slug: 'mma', name: 'MMA', kind: 'combat', league_count: 3 },
]

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string) => {
      if (url === '/api/health') return Promise.resolve(jsonResponse(healthFixture))
      if (url === '/api/sports') return Promise.resolve(jsonResponse(sportsFixture))
      return Promise.resolve(new Response('not found', { status: 404 }))
    }),
  )
}

function renderDashboard() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <Dashboard />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Dashboard', () => {
  it('renders component statuses from /api/health', async () => {
    stubApi()

    renderDashboard()

    expect(await screen.findByText('Database')).toBeInTheDocument()
    expect(screen.getByText('up')).toBeInTheDocument()
    expect(screen.getByText('disabled')).toBeInTheDocument()
    expect(vi.mocked(fetch)).toHaveBeenCalledWith('/api/health')
  })

  it('renders sport coverage from /api/sports', async () => {
    stubApi()

    renderDashboard()

    expect(await screen.findByText('Coverage')).toBeInTheDocument()
    expect(screen.getByText('Football')).toBeInTheDocument()
    expect(screen.getByText('20 leagues')).toBeInTheDocument()
  })

  it('shows an error state when the API is unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))

    renderDashboard()

    expect(await screen.findByText('API unreachable')).toBeInTheDocument()
  })
})
