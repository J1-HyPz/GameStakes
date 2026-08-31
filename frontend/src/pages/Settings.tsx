import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Badge, Card, EmptyState, ErrorState, Skeleton } from '../components/ui'
import {
  fetchJobRuns,
  fetchJobSchedule,
  fetchProviders,
  fetchResolutionQueue,
  ignoreQueueItem,
  resolveQueueItem,
  runJob,
  testProvider,
  type ProviderHealth,
} from '../lib/api'

const STATE_TONE = {
  up: 'positive',
  degraded: 'warning',
  down: 'negative',
  disabled: 'neutral',
} as const

function ProviderCard() {
  const { data, isPending, isError } = useQuery({
    queryKey: ['providers'],
    queryFn: fetchProviders,
  })
  const [results, setResults] = useState<Record<string, ProviderHealth>>({})
  const test = useMutation({
    mutationFn: testProvider,
    onSuccess: (health) => setResults((prev) => ({ ...prev, [health.name]: health })),
  })

  if (isPending) return <Skeleton rows={4} />
  if (isError) return <ErrorState title="Could not load data sources" />

  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        Keys are set as environment variables and read at startup — see{' '}
        <code className="text-slate-400">.env.example</code>. Testing a connection never spends
        metered credits.
      </p>
      {data.map((provider) => {
        const result = results[provider.name]
        return (
          <div
            key={provider.name}
            className="rounded border border-white/10 bg-surface-overlay p-3"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium text-slate-200">{provider.name}</span>
              {provider.configured ? (
                <Badge tone="positive">configured</Badge>
              ) : (
                <Badge>no key</Badge>
              )}
              {provider.best_effort && <Badge tone="warning">best effort</Badge>}
              <button
                type="button"
                onClick={() => test.mutate(provider.name)}
                disabled={test.isPending}
                className="ml-auto rounded bg-white/10 px-2 py-1 text-xs text-slate-200 hover:bg-white/15 disabled:opacity-50"
              >
                Test connection
              </button>
            </div>
            <p className="mt-2 text-xs text-slate-500">
              {provider.capabilities.join(', ')} · {provider.leagues.length} leagues
            </p>
            {result && (
              <p className="mt-2 text-xs">
                <Badge tone={STATE_TONE[result.state]}>{result.state}</Badge>{' '}
                <span className="text-slate-400">{result.detail}</span>
                {result.quota_remaining !== null && result.quota_remaining !== undefined && (
                  <span className="ml-1 text-slate-400">
                    · {result.quota_remaining} credits remaining
                  </span>
                )}
              </p>
            )}
          </div>
        )
      })}
    </div>
  )
}

function JobsCard() {
  const queryClient = useQueryClient()
  const { data: schedule } = useQuery({ queryKey: ['job-schedule'], queryFn: fetchJobSchedule })
  const { data: runs, isPending } = useQuery({
    queryKey: ['job-runs'],
    queryFn: () => fetchJobRuns(15),
    refetchInterval: 30_000,
  })
  const trigger = useMutation({
    mutationFn: runJob,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['job-runs'] }),
  })

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        {schedule?.length ? (
          schedule.map((job) => (
            <button
              key={job.id}
              type="button"
              onClick={() => trigger.mutate(job.id)}
              disabled={trigger.isPending}
              className="rounded bg-white/10 px-2.5 py-1 text-xs text-slate-200 hover:bg-white/15 disabled:opacity-50"
            >
              Run {job.name}
            </button>
          ))
        ) : (
          <p className="text-xs text-slate-500">
            The scheduler is disabled, so jobs only run when triggered.
          </p>
        )}
      </div>

      {isPending && <Skeleton rows={3} />}
      {runs?.length === 0 && <EmptyState title="No jobs have run yet." />}
      {runs && runs.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-slate-500">
              <tr>
                <th className="py-1 pr-3 font-medium">Job</th>
                <th className="py-1 pr-3 font-medium">Status</th>
                <th className="py-1 pr-3 text-right font-medium">Fetched</th>
                <th className="py-1 pr-3 text-right font-medium">Saved</th>
                <th className="py-1 font-medium">When</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/5">
              {runs.map((run) => (
                <tr key={run.id}>
                  <td className="py-1.5 pr-3 text-slate-300">{run.job_name}</td>
                  <td className="py-1.5 pr-3">
                    <Badge
                      tone={
                        run.status === 'success'
                          ? 'positive'
                          : run.status === 'failure'
                            ? 'negative'
                            : 'neutral'
                      }
                    >
                      {run.status}
                    </Badge>
                    {run.error && <span className="ml-1 text-edge-negative">{run.error}</span>}
                  </td>
                  <td className="py-1.5 pr-3 text-right tabular-nums text-slate-400">
                    {run.rows_fetched}
                  </td>
                  <td className="py-1.5 pr-3 text-right tabular-nums text-slate-400">
                    {run.rows_upserted}
                  </td>
                  <td className="py-1.5 text-slate-500">
                    {new Date(run.started_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function ResolutionCard() {
  const queryClient = useQueryClient()
  const { data, isPending } = useQuery({
    queryKey: ['resolution-queue'],
    queryFn: fetchResolutionQueue,
  })
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['resolution-queue'] })
  const resolve = useMutation({
    mutationFn: ({ id, entityId }: { id: number; entityId: number }) =>
      resolveQueueItem(id, entityId),
    onSuccess: invalidate,
  })
  const ignore = useMutation({ mutationFn: ignoreQueueItem, onSuccess: invalidate })

  if (isPending) return <Skeleton rows={2} />
  if (!data?.length) {
    return (
      <EmptyState
        title="Nothing waiting for review."
        hint="Names that cannot be matched confidently land here instead of being guessed."
      />
    )
  }

  return (
    <div className="space-y-3">
      {data.map((item) => (
        <div key={item.id} className="rounded border border-white/10 bg-surface-overlay p-3">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="text-sm text-slate-200">{item.raw_name}</span>
            <span className="text-xs text-slate-500">
              {item.entity_type} · from {item.provider}
            </span>
            <button
              type="button"
              onClick={() => ignore.mutate(item.id)}
              className="ml-auto rounded bg-white/10 px-2 py-0.5 text-xs text-slate-300 hover:bg-white/15"
            >
              Ignore
            </button>
          </div>
          {item.candidates.length > 0 ? (
            <ul className="mt-2 space-y-1">
              {(item.candidates as { entity_id: number; name: string; score: number }[]).map(
                (candidate) => (
                  <li key={candidate.entity_id} className="flex items-center gap-2 text-xs">
                    <span className="text-slate-300">{candidate.name}</span>
                    <span className="tabular-nums text-slate-500">
                      {candidate.score.toFixed(0)}% match
                    </span>
                    <button
                      type="button"
                      onClick={() =>
                        resolve.mutate({ id: item.id, entityId: candidate.entity_id })
                      }
                      className="rounded bg-white/10 px-2 py-0.5 text-slate-200 hover:bg-white/15"
                    >
                      This one
                    </button>
                  </li>
                ),
              )}
            </ul>
          ) : (
            <p className="mt-2 text-xs text-slate-500">
              No similar names found — nothing in the database resembles this one.
            </p>
          )}
        </div>
      ))}
    </div>
  )
}

export default function Settings() {
  return (
    <div className="space-y-4">
      <Card title="Data sources">
        <ProviderCard />
      </Card>
      <Card title="Ingestion jobs">
        <JobsCard />
      </Card>
      <Card title="Name review queue">
        <ResolutionCard />
      </Card>
    </div>
  )
}
