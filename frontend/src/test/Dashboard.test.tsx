import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { HealthResponse } from '../lib/api'
import Dashboard from '../pages/Dashboard'

const healthFixture: HealthResponse = {
  status: 'ok',
  app: 'GameStakes',
  version: '0.1.0',
  database: { status: 'up', detail: null },
  redis: { status: 'disabled', detail: 'REDIS_URL not configured' },
  providers: [],
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
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(healthFixture), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    renderDashboard()

    expect(await screen.findByText('Database')).toBeInTheDocument()
    expect(screen.getByText('up')).toBeInTheDocument()
    expect(screen.getByText('disabled')).toBeInTheDocument()
    expect(vi.mocked(fetch)).toHaveBeenCalledWith('/api/health')
  })

  it('shows an error state when the API is unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))

    renderDashboard()

    expect(await screen.findByText('API unreachable')).toBeInTheDocument()
  })
})
