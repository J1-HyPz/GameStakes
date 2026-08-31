import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { Badge, Card, EmptyState, ErrorState, Skeleton } from '../components/ui'
import { fetchFixture } from '../lib/api'
import { formatDate, formatTime, statusLabel } from '../lib/format'

export default function FixtureDetail() {
  const { id } = useParams<{ id: string }>()
  const fixtureId = Number(id)
  const { data, isPending, isError, error } = useQuery({
    queryKey: ['fixture', fixtureId],
    queryFn: () => fetchFixture(fixtureId),
    enabled: Number.isFinite(fixtureId),
  })

  if (isPending) return <Skeleton rows={4} />
  if (isError) return <ErrorState title="Could not load fixture" detail={(error as Error).message} />

  const settled = data.home.score !== null && data.away.score !== null

  return (
    <div className="space-y-4">
      <Link to="/schedule" className="text-xs text-slate-400 hover:text-slate-200">
        ← Back to schedule
      </Link>

      <Card>
        <div className="flex flex-wrap items-baseline gap-2">
          <Badge>{statusLabel(data.status)}</Badge>
          <span className="text-xs text-slate-500">
            {data.league_name} · {formatDate(data.start_time)} {formatTime(data.start_time)}
            {data.venue ? ` · ${data.venue}` : ''}
          </span>
        </div>
        <div className="mt-3 flex items-center gap-4">
          <span className="flex-1 text-right text-base text-slate-100">{data.home.name}</span>
          <span className="text-lg font-semibold tabular-nums text-white">
            {settled ? `${data.home.score}–${data.away.score}` : 'v'}
          </span>
          <span className="flex-1 text-base text-slate-100">{data.away.name}</span>
        </div>
      </Card>

      <Card title="Model projection">
        <EmptyState
          title="No projection for this fixture yet."
          hint="Match models and Monte Carlo simulation arrive in a later phase; until then this page shows schedule data only."
        />
      </Card>
    </div>
  )
}
