import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Card, EmptyState, ErrorState } from '../components/ui'
import { fetchLeagues, runBacktest, type BacktestResult } from '../lib/api'

function Results({ result }: { result: BacktestResult }) {
  return (
    <div className="space-y-4">
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div>
          <dt className="text-xs text-slate-500">Bets placed</dt>
          <dd className="text-sm tabular-nums text-slate-100">{result.bets_placed}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Fixtures predicted</dt>
          <dd className="text-sm tabular-nums text-slate-100">{result.fixtures_predicted}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Final bankroll</dt>
          <dd className="text-sm tabular-nums text-slate-100">
            {result.final_bankroll.toFixed(2)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Max drawdown</dt>
          <dd className="text-sm tabular-nums text-slate-100">
            {(result.max_drawdown * 100).toFixed(1)}%
          </dd>
        </div>
      </dl>

      <dl className="space-y-2">
        <div>
          <dt className="text-xs text-slate-500">Hit rate</dt>
          <dd className="text-sm text-slate-200">{result.hit_rate.description}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Return on investment</dt>
          <dd className="text-sm text-slate-200">{result.roi.description}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Brier score</dt>
          <dd className="text-sm tabular-nums text-slate-200">
            {result.brier_score?.toFixed(4) ?? '—'}
          </dd>
        </div>
      </dl>

      {result.notes.length > 0 && (
        <div className="rounded border border-white/10 bg-surface-overlay p-3">
          <p className="text-xs font-medium text-slate-400">Notes</p>
          <ul className="mt-1 space-y-1">
            {result.notes.map((note, i) => (
              <li key={i} className="text-xs text-slate-500">
                {note}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

export default function Backtest() {
  const { data: leagues } = useQuery({ queryKey: ['leagues'], queryFn: () => fetchLeagues() })
  const [league, setLeague] = useState('premier-league')
  const [start, setStart] = useState('2026-01-01')
  const [end, setEnd] = useState('2026-04-01')
  const [minEdge, setMinEdge] = useState(0.03)

  const run = useMutation({
    mutationFn: () =>
      runBacktest({ league, start, end, min_edge: minEdge, starting_bankroll: 1000 }),
  })

  return (
    <div className="space-y-4">
      <Card title="Walk-forward backtest">
        <p className="mb-3 text-xs text-slate-500">
          Replays the period week by week, refitting the model on data available at the time.
          Training is restricted to fixtures that kicked off before each cutoff, and prices to
          snapshots captured before kickoff — if anything violates that, the run fails rather
          than reporting a fictional edge.
        </p>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label className="text-xs text-slate-400">
            League
            <select
              value={league}
              onChange={(e) => setLeague(e.target.value)}
              className="mt-1 w-full rounded border border-white/10 bg-surface-overlay px-2 py-1 text-slate-200"
            >
              {leagues?.map((l) => (
                <option key={l.slug} value={l.slug}>
                  {l.name}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-400">
            From
            <input
              type="date"
              value={start}
              onChange={(e) => setStart(e.target.value)}
              className="mt-1 w-full rounded border border-white/10 bg-surface-overlay px-2 py-1 text-slate-200"
            />
          </label>
          <label className="text-xs text-slate-400">
            To
            <input
              type="date"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
              className="mt-1 w-full rounded border border-white/10 bg-surface-overlay px-2 py-1 text-slate-200"
            />
          </label>
          <label className="text-xs text-slate-400">
            Minimum edge
            <input
              type="number"
              step="0.01"
              min="0"
              max="0.5"
              value={minEdge}
              onChange={(e) => setMinEdge(Number(e.target.value))}
              className="mt-1 w-full rounded border border-white/10 bg-surface-overlay px-2 py-1 text-slate-200"
            />
          </label>
        </div>

        <button
          type="button"
          onClick={() => run.mutate()}
          disabled={run.isPending}
          className="mt-3 rounded bg-white/10 px-3 py-1.5 text-xs text-slate-200 hover:bg-white/15 disabled:opacity-50"
        >
          {run.isPending ? 'Running…' : 'Run backtest'}
        </button>
      </Card>

      {run.isError && (
        <ErrorState title="Backtest failed" detail={(run.error as Error).message} />
      )}

      {run.data && (
        <Card title={`${run.data.league} · model ${run.data.model_version}`}>
          <Results result={run.data} />
        </Card>
      )}

      {!run.data && !run.isPending && !run.isError && (
        <Card>
          <EmptyState
            title="No backtest run yet."
            hint="A backtest needs finished fixtures with results and odds snapshots in the chosen period."
          />
        </Card>
      )}
    </div>
  )
}
