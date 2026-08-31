import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { BuilderResponse } from '../lib/api'
import BetBuilder from '../pages/BetBuilder'

function response(overrides: Partial<BuilderResponse> = {}): BuilderResponse {
  return {
    tiers: [],
    bankroll: 1000,
    currency: 'GBP',
    staking_note: 'Quarter Kelly: drawdowns of 20-30% are normal over a season.',
    slate_start: '2026-09-12T00:00:00Z',
    slate_end: '2026-09-14T00:00:00Z',
    disclaimer: 'Model probabilities are estimates, not certainties.',
    ...overrides,
  }
}

function stub(body: BuilderResponse) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    ),
  )
}

function renderBuilder() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <BetBuilder />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('BetBuilder', () => {
  it('explains why a tier has no bet instead of showing an empty card', async () => {
    stub(
      response({
        tiers: [
          {
            tier: 'low',
            has_bet: false,
            candidates_considered: 14,
            candidates_qualifying: 0,
            reason: '14 candidates found, none above the 2% edge threshold (best was 0.8%)',
          },
        ],
      }),
    )

    renderBuilder()

    expect(await screen.findByText('No qualifying bet.')).toBeInTheDocument()
    expect(screen.getByText(/none above the 2% edge threshold/)).toBeInTheDocument()
    expect(screen.getByText(/14 selections considered/)).toBeInTheDocument()
  })

  it('shows the correlated price alongside the naive one', async () => {
    stub(
      response({
        tiers: [
          {
            tier: 'medium',
            has_bet: true,
            legs: [
              {
                fixture_id: 1,
                fixture: 'Arsenal v Chelsea',
                league: 'Premier League',
                kick_off: '2026-09-12T14:00:00Z',
                market: '1x2',
                selection: 'home',
                line: null,
                price_decimal: 2.1,
                bookmaker: 'Bet365',
                model_probability: 0.55,
                implied_probability: 0.48,
                edge: 0.07,
                confidence: 'high',
                reasoning: 'model 55.0% against a de-vigged market price of 48.0%',
              },
            ],
            combined_odds: 2.1,
            combined_probability: 0.55,
            naive_probability: 0.48,
            correlation_effect: 0.07,
            expected_value: 0.155,
            stake: 15,
            stake_fraction: 0.015,
            kelly_fraction: 0.15,
            projected_return: 31.5,
            loss_frequency: 'expect this to lose roughly 5 times in 10',
            candidates_considered: 20,
            candidates_qualifying: 6,
            copy_text: 'Arsenal v Chelsea: 1x2 home @ 2.10',
          },
        ],
      }),
    )

    renderBuilder()

    expect(await screen.findByText('Arsenal v Chelsea')).toBeInTheDocument()
    // The correlation effect must be visible, not implied.
    expect(screen.getByText(/Assuming the legs were/)).toBeInTheDocument()
    expect(screen.getByText(/lose roughly 5 times in 10/)).toBeInTheDocument()
    expect(screen.getByText('Copy bet')).toBeInTheDocument()
  })

  it('states the honest framing about variance', async () => {
    stub(response({ tiers: [] }))

    renderBuilder()

    expect(await screen.findByText(/drawdowns of 20-30% are normal/)).toBeInTheDocument()
    expect(screen.getByText(/estimates, not certainties/)).toBeInTheDocument()
  })
})
