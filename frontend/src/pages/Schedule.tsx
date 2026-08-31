import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Card, EmptyState, ErrorState, Skeleton } from '../components/ui'
import { fetchFixtures, fetchSports, type Fixture } from '../lib/api'
import { addDays, formatDate, formatTime, statusLabel, toISODate } from '../lib/format'

const RANGES = [
  { label: 'Today', days: 0 },
  { label: 'Next 3 days', days: 2 },
  { label: 'Week', days: 6 },
  { label: 'Fortnight', days: 13 },
] as const

function StatusBadge({ status }: { status: string }) {
  const tone =
    status === 'in_play'
      ? 'positive'
      : status === 'postponed' || status === 'cancelled'
        ? 'warning'
        : 'neutral'
  return <Badge tone={tone}>{statusLabel(status)}</Badge>
}

function FixtureRow({ fixture }: { fixture: Fixture }) {
  const settled = fixture.home.score !== null && fixture.away.score !== null
  return (
    <Link
      to={`/fixtures/${fixture.id}`}
      className="flex items-center gap-3 rounded px-2 py-2 hover:bg-white/5 focus:bg-white/5 focus:outline-none focus-visible:ring-1 focus-visible:ring-white/30"
    >
      <time className="w-14 shrink-0 text-xs tabular-nums text-slate-400">
        {formatTime(fixture.start_time)}
      </time>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm text-slate-200">
          {fixture.home.name} <span className="text-slate-500">v</span> {fixture.away.name}
        </p>
        <p className="truncate text-xs text-slate-500">
          {fixture.league_name}
          {fixture.round ? ` · ${fixture.round}` : ''}
        </p>
      </div>
      {settled && (
        <span className="shrink-0 text-sm font-medium tabular-nums text-slate-200">
          {fixture.home.score}–{fixture.away.score}
        </span>
      )}
      <StatusBadge status={fixture.status} />
    </Link>
  )
}

export default function Schedule() {
  const [rangeDays, setRangeDays] = useState<number>(6)
  const [sport, setSport] = useState<string>('')

  const start = toISODate(new Date())
  const end = toISODate(addDays(new Date(), rangeDays))

  const { data: sports } = useQuery({ queryKey: ['sports'], queryFn: fetchSports })
  const { data, isPending, isError, error } = useQuery({
    queryKey: ['fixtures', start, end, sport],
    queryFn: () => fetchFixtures({ start, end, sport: sport || undefined }),
    // Live scores refresh on their own; the rest is cheap enough to follow along.
    refetchInterval: 60_000,
  })

  const byDay = useMemo(() => {
    const groups = new Map<string, Fixture[]>()
    for (const fixture of data?.fixtures ?? []) {
      const day = fixture.start_time.slice(0, 10)
      groups.set(day, [...(groups.get(day) ?? []), fixture])
    }
    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [data])

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1" role="group" aria-label="Date range">
          {RANGES.map((range) => (
            <button
              key={range.label}
              type="button"
              onClick={() => setRangeDays(range.days)}
              aria-pressed={rangeDays === range.days}
              className={`rounded px-2.5 py-1 text-xs ${
                rangeDays === range.days
                  ? 'bg-white/15 text-white'
                  : 'bg-white/5 text-slate-400 hover:text-slate-200'
              }`}
            >
              {range.label}
            </button>
          ))}
        </div>
        <label className="ml-auto flex items-center gap-2 text-xs text-slate-400">
          Sport
          <select
            value={sport}
            onChange={(e) => setSport(e.target.value)}
            className="rounded border border-white/10 bg-surface-overlay px-2 py-1 text-slate-200"
          >
            <option value="">All sports</option>
            {sports?.map((s) => (
              <option key={s.slug} value={s.slug}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      {isPending && <Skeleton rows={5} />}
      {isError && (
        <ErrorState title="Could not load the schedule" detail={(error as Error).message} />
      )}

      {data && byDay.length === 0 && (
        <Card>
          <EmptyState
            title="No fixtures in this range."
            hint="Fixtures arrive from data providers — add a key in Settings, then run the fixtures job."
          />
        </Card>
      )}

      {byDay.map(([day, fixtures]) => (
        <Card key={day} title={formatDate(`${day}T12:00:00Z`)}>
          <div className="-mx-2 divide-y divide-white/5">
            {fixtures.map((fixture) => (
              <FixtureRow key={fixture.id} fixture={fixture} />
            ))}
          </div>
        </Card>
      ))}
    </div>
  )
}
