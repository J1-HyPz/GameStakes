import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { Badge, Card, EmptyState, ErrorState, Skeleton } from '../components/ui'
import { fetchMetrics, fetchTrackedBets, runSettlement } from '../lib/api'

function CalibrationChart({
  points,
}: {
  points: { label: string; predicted: number; actual: number; count: number }[]
}) {
  if (points.length === 0) {
    return (
      <EmptyState
        title="No graded predictions yet."
        hint="Calibration compares stated probabilities against what actually happened — it needs settled fixtures."
      />
    )
  }
  const data = points.map((p) => ({
    predicted: p.predicted * 100,
    actual: p.actual * 100,
    count: p.count,
    label: p.label,
  }))

  return (
    <div>
      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <ScatterChart margin={{ top: 8, right: 8, bottom: 24, left: 8 }}>
            <CartesianGrid stroke="rgba(255,255,255,0.08)" />
            <XAxis
              type="number"
              dataKey="predicted"
              domain={[0, 100]}
              name="Predicted"
              unit="%"
              stroke="#64748b"
              fontSize={11}
              label={{ value: 'Predicted', position: 'insideBottom', offset: -12, fill: '#64748b' }}
            />
            <YAxis
              type="number"
              dataKey="actual"
              domain={[0, 100]}
              name="Actual"
              unit="%"
              stroke="#64748b"
              fontSize={11}
            />
            {/* Perfect calibration is the diagonal: predicted equals actual. */}
            <ReferenceLine
              segment={[
                { x: 0, y: 0 },
                { x: 100, y: 100 },
              ]}
              stroke="#475569"
              strokeDasharray="4 4"
            />
            <Tooltip
              contentStyle={{ background: '#161a21', border: '1px solid rgba(255,255,255,0.1)' }}
              formatter={(value: number, name: string) => [`${value.toFixed(1)}%`, name]}
            />
            <Scatter data={data} fill="#22c55e" />
          </ScatterChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-2 text-xs text-slate-500">
        Points on the dashed line mean the model is honest: its 70% picks win about 70% of the
        time. Points below the line mean it is overconfident, and every edge computed from those
        probabilities is overstated.
      </p>
    </div>
  )
}

function EquityChart({ points }: { points: { index: number; bankroll: number }[] }) {
  if (points.length <= 1) {
    return <EmptyState title="No settled bets yet." />
  }
  return (
    <div className="h-56">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={points} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
          <CartesianGrid stroke="rgba(255,255,255,0.08)" />
          <XAxis dataKey="index" stroke="#64748b" fontSize={11} />
          <YAxis stroke="#64748b" fontSize={11} domain={['auto', 'auto']} />
          <Tooltip
            contentStyle={{ background: '#161a21', border: '1px solid rgba(255,255,255,0.1)' }}
          />
          <Line type="monotone" dataKey="bankroll" stroke="#22c55e" dot={false} strokeWidth={2} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

export default function Tracker() {
  const queryClient = useQueryClient()
  const { data, isPending, isError, error } = useQuery({
    queryKey: ['tracker'],
    queryFn: fetchMetrics,
  })
  const { data: bets } = useQuery({ queryKey: ['bets'], queryFn: () => fetchTrackedBets() })
  const settle = useMutation({
    mutationFn: runSettlement,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tracker'] })
      queryClient.invalidateQueries({ queryKey: ['bets'] })
    },
  })

  if (isPending) return <Skeleton rows={6} />
  if (isError) return <ErrorState title="Could not load metrics" detail={(error as Error).message} />

  return (
    <div className="space-y-4">
      {data.sample_warning && (
        <p className="rounded border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          {data.sample_warning}
        </p>
      )}

      <Card
        title="Performance"
        action={
          <button
            type="button"
            onClick={() => settle.mutate()}
            disabled={settle.isPending}
            className="rounded bg-white/10 px-2 py-0.5 text-xs text-slate-200 hover:bg-white/15 disabled:opacity-50"
          >
            Settle finished
          </button>
        }
      >
        <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <dt className="text-xs text-slate-500">Hit rate</dt>
            <dd className="text-sm text-slate-200">{data.hit_rate.description}</dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Return on investment</dt>
            <dd className="text-sm text-slate-200">{data.roi.description}</dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Brier score</dt>
            <dd className="text-sm tabular-nums text-slate-200">
              {data.brier_score?.toFixed(4) ?? '—'}
              <span className="ml-1 text-xs text-slate-500">
                (0.25 = no better than a coin flip)
              </span>
            </dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Average closing line value</dt>
            <dd className="text-sm tabular-nums text-slate-200">
              {data.average_clv !== null && data.average_clv !== undefined
                ? `${(data.average_clv * 100).toFixed(2)}%`
                : '—'}
            </dd>
          </div>
        </dl>
        <p className="mt-3 text-xs text-slate-500">{data.exposure}</p>
      </Card>

      <Card title="Calibration">
        <CalibrationChart points={data.calibration} />
      </Card>

      <Card title="Bankroll">
        <EquityChart points={data.equity} />
        <dl className="mt-3 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
          <div>
            <dt className="text-slate-500">Max drawdown</dt>
            <dd className="tabular-nums text-slate-200">
              {(data.max_drawdown * 100).toFixed(1)}%
            </dd>
          </div>
          <div>
            <dt className="text-slate-500">Longest losing run</dt>
            <dd className="tabular-nums text-slate-200">{data.longest_losing_streak}</dd>
          </div>
          <div>
            <dt className="text-slate-500">Settled</dt>
            <dd className="tabular-nums text-slate-200">{data.settled_bets}</dd>
          </div>
          <div>
            <dt className="text-slate-500">Open</dt>
            <dd className="tabular-nums text-slate-200">{data.open_bets}</dd>
          </div>
        </dl>
      </Card>

      <Card title="Bets">
        {!bets?.length ? (
          <EmptyState title="No bets tracked yet." hint="Add one from the bet builder." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="text-slate-500">
                <tr>
                  <th className="py-1 pr-3 font-medium">Tier</th>
                  <th className="py-1 pr-3 font-medium">Status</th>
                  <th className="py-1 pr-3 text-right font-medium">Stake</th>
                  <th className="py-1 pr-3 text-right font-medium">Odds</th>
                  <th className="py-1 text-right font-medium">Return</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {bets.map((bet) => (
                  <tr key={bet.id}>
                    <td className="py-1.5 pr-3 text-slate-300">{bet.tier}</td>
                    <td className="py-1.5 pr-3">
                      <Badge
                        tone={
                          bet.status === 'settled'
                            ? Number(bet.payout ?? 0) > Number(bet.stake)
                              ? 'positive'
                              : 'negative'
                            : 'neutral'
                        }
                      >
                        {bet.status}
                      </Badge>
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums text-slate-300">
                      {Number(bet.stake).toFixed(2)}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums text-slate-300">
                      {Number(bet.combined_price_decimal).toFixed(2)}
                    </td>
                    <td className="py-1.5 text-right tabular-nums text-slate-300">
                      {bet.payout !== null && bet.payout !== undefined
                        ? Number(bet.payout).toFixed(2)
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
