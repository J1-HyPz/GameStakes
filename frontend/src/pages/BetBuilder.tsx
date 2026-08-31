import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Card, EdgeValue, ErrorState, Skeleton } from '../components/ui'
import { fetchBuilder, trackBet, type TierOut } from '../lib/api'
import { formatTime } from '../lib/format'

const TIER_LABEL: Record<string, string> = {
  low: 'Low risk',
  medium: 'Medium risk',
  high: 'High risk',
  manual: 'Manual',
}

function NoBet({ tier }: { tier: TierOut }) {
  return (
    <div className="rounded border border-white/10 bg-surface-overlay p-4">
      <p className="text-sm text-slate-300">No qualifying bet.</p>
      <p className="mt-1 text-xs text-slate-500">{tier.reason}</p>
      {(tier.candidates_considered ?? 0) > 0 && (
        <p className="mt-2 text-xs text-slate-600">
          {tier.candidates_considered} selections considered,{' '}
          {tier.candidates_qualifying} cleared this tier&rsquo;s filters.
        </p>
      )}
    </div>
  )
}

function TierCard({ tier, currency }: { tier: TierOut; currency: string }) {
  const [showWhy, setShowWhy] = useState(false)
  const [copied, setCopied] = useState(false)
  const queryClient = useQueryClient()

  const track = useMutation({
    mutationFn: () =>
      trackBet({
        tier: tier.tier,
        stake: tier.stake ?? 0,
        combined_price_decimal: tier.combined_odds ?? 1,
        combined_probability: tier.combined_probability ?? 0,
        naive_probability: tier.naive_probability,
        expected_value: tier.expected_value,
        kelly_fraction: tier.kelly_fraction,
        legs: (tier.legs ?? []).map((leg) => ({
          fixture_id: leg.fixture_id,
          market: leg.market,
          selection: leg.selection,
          line: leg.line,
          price_decimal: leg.price_decimal,
          bookmaker: leg.bookmaker,
          model_probability: leg.model_probability,
          implied_probability: leg.implied_probability,
          edge: leg.edge,
        })),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tracker'] }),
  })

  async function copy() {
    if (!tier.copy_text) return
    await navigator.clipboard.writeText(tier.copy_text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <Card title={TIER_LABEL[tier.tier] ?? tier.tier}>
      {!tier.has_bet ? (
        <NoBet tier={tier} />
      ) : (
        <div className="space-y-4">
          <ul className="divide-y divide-white/5">
            {(tier.legs ?? []).map((leg, i) => (
              <li key={i} className="py-2">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className="text-sm text-slate-200">{leg.fixture}</span>
                  <span className="text-xs text-slate-500">
                    {leg.league} · {formatTime(leg.kick_off)}
                  </span>
                </div>
                <div className="mt-1 flex flex-wrap items-baseline gap-x-3 text-xs">
                  <span className="text-slate-300">
                    {leg.market} {leg.selection}
                    {leg.line !== null ? ` ${leg.line}` : ''}
                  </span>
                  <span className="tabular-nums text-slate-200">
                    {leg.price_decimal.toFixed(2)}
                  </span>
                  {leg.bookmaker && <span className="text-slate-500">{leg.bookmaker}</span>}
                  <span className="text-slate-500">
                    model {(leg.model_probability * 100).toFixed(1)}%
                  </span>
                  <EdgeValue value={leg.edge} />
                </div>
              </li>
            ))}
          </ul>

          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-4">
            <div>
              <dt className="text-slate-500">Combined odds</dt>
              <dd className="tabular-nums text-slate-100">{tier.combined_odds?.toFixed(2)}</dd>
            </div>
            <div>
              <dt className="text-slate-500">Model probability</dt>
              <dd className="tabular-nums text-slate-100">
                {((tier.combined_probability ?? 0) * 100).toFixed(1)}%
              </dd>
            </div>
            <div>
              <dt className="text-slate-500">Expected value</dt>
              <dd className="tabular-nums">
                <EdgeValue value={tier.expected_value} />
              </dd>
            </div>
            <div>
              <dt className="text-slate-500">Stake</dt>
              <dd className="tabular-nums text-slate-100">
                {currency} {tier.stake?.toFixed(2)}{' '}
                <span className="text-slate-500">
                  ({((tier.stake_fraction ?? 0) * 100).toFixed(2)}% of bankroll)
                </span>
              </dd>
            </div>
          </dl>

          {tier.naive_probability !== null && tier.naive_probability !== undefined && (
            <p className="text-xs text-slate-500">
              Priced from the joint simulation at{' '}
              {((tier.combined_probability ?? 0) * 100).toFixed(1)}%. Assuming the legs were
              independent would have said {(tier.naive_probability * 100).toFixed(1)}% — a
              difference of {((tier.correlation_effect ?? 0) * 100).toFixed(1)} points.
            </p>
          )}

          <p className="text-xs text-slate-400">
            Returns {currency} {tier.projected_return?.toFixed(2)} if it lands;{' '}
            {tier.loss_frequency}.
            {tier.capped_by === 'tier_cap' && ' Stake capped by this tier’s limit.'}
            {tier.capped_by === 'exposure_cap' && ' Stake capped by remaining daily exposure.'}
          </p>

          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={copy}
              className="rounded bg-white/10 px-2.5 py-1 text-xs text-slate-200 hover:bg-white/15"
            >
              {copied ? 'Copied' : 'Copy bet'}
            </button>
            <button
              type="button"
              onClick={() => track.mutate()}
              disabled={track.isPending || track.isSuccess}
              className="rounded bg-white/10 px-2.5 py-1 text-xs text-slate-200 hover:bg-white/15 disabled:opacity-50"
            >
              {track.isSuccess ? 'Added to tracker' : 'Add to tracker'}
            </button>
            <button
              type="button"
              onClick={() => setShowWhy((v) => !v)}
              aria-expanded={showWhy}
              className="rounded px-2.5 py-1 text-xs text-slate-400 hover:text-slate-200"
            >
              {showWhy ? 'Hide reasoning' : 'Why these legs?'}
            </button>
          </div>

          {showWhy && (
            <ul className="space-y-1 border-t border-white/5 pt-3">
              {(tier.legs ?? []).map((leg, i) => (
                <li key={i} className="text-xs text-slate-400">
                  <span className="text-slate-300">{leg.fixture}</span> — {leg.reasoning}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Card>
  )
}

export default function BetBuilder() {
  const [hours, setHours] = useState(48)
  const { data, isPending, isError, error } = useQuery({
    queryKey: ['builder', hours],
    queryFn: () => fetchBuilder({ hours_ahead: hours }),
  })

  if (isPending) return <Skeleton rows={6} />
  if (isError) return <ErrorState title="Could not build bets" detail={(error as Error).message} />

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-xs text-slate-400">
          Slate
          <select
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
            className="rounded border border-white/10 bg-surface-overlay px-2 py-1 text-slate-200"
          >
            <option value={24}>Next 24 hours</option>
            <option value={48}>Next 48 hours</option>
            <option value={72}>Next 3 days</option>
            <option value={168}>Next week</option>
          </select>
        </label>
        <span className="text-xs text-slate-500">
          Bankroll: {data.currency} {data.bankroll.toFixed(2)}
        </span>
      </div>

      <p className="rounded border border-white/10 bg-surface-raised px-3 py-2 text-xs text-slate-400">
        {data.staking_note} {data.disclaimer}
      </p>

      {data.tiers.map((tier) => (
        <TierCard key={tier.tier} tier={tier} currency={data.currency} />
      ))}
    </div>
  )
}
