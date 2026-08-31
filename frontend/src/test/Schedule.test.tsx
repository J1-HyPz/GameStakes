import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Fixture, FixturePage, Sport } from '../lib/api'
import Schedule from '../pages/Schedule'

const fixture: Fixture = {
  id: 1,
  sport_slug: 'football',
  league_slug: 'premier-league',
  league_name: 'Premier League',
  start_time: '2026-09-12T14:00:00Z',
  status: 'scheduled',
  round: '4',
  event_name: null,
  venue: 'Old Trafford',
  home: { side: 'home', name: 'Manchester United', team_id: 1, player_id: null, logo_url: null, score: null },
  away: { side: 'away', name: 'Chelsea', team_id: 2, player_id: null, logo_url: null, score: null },
  has_prediction: false,
}

const sports: Sport[] = [
  { id: 1, slug: 'football', name: 'Football', kind: 'team', league_count: 20 },
]

function stub(page: FixturePage) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string) => {
      const body = url.startsWith('/api/sports') ? sports : page
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    }),
  )
}

function renderSchedule() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Schedule />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Schedule', () => {
  it('lists fixtures grouped under their date', async () => {
    stub({ fixtures: [fixture], total: 1, start: '2026-09-12', end: '2026-09-18' })

    renderSchedule()

    expect(await screen.findByText(/Manchester United/)).toBeInTheDocument()
    expect(screen.getByText(/Premier League · 4/)).toBeInTheDocument()
    expect(screen.getByText('Scheduled')).toBeInTheDocument()
  })

  it('shows a score once a fixture has finished', async () => {
    stub({
      fixtures: [
        {
          ...fixture,
          status: 'finished',
          home: { ...fixture.home, score: 2 },
          away: { ...fixture.away, score: 1 },
        },
      ],
      total: 1,
      start: '2026-09-12',
      end: '2026-09-18',
    })

    renderSchedule()

    expect(await screen.findByText('2–1')).toBeInTheDocument()
    expect(screen.getByText('Finished')).toBeInTheDocument()
  })

  it('explains an empty slate instead of showing a blank page', async () => {
    stub({ fixtures: [], total: 0, start: '2026-09-12', end: '2026-09-18' })

    renderSchedule()

    expect(await screen.findByText(/No fixtures in this range/)).toBeInTheDocument()
    expect(screen.getByText(/add a key in Settings/i)).toBeInTheDocument()
  })

  it('surfaces an API failure rather than pretending there are no games', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))

    renderSchedule()

    expect(await screen.findByText('Could not load the schedule')).toBeInTheDocument()
  })
})
