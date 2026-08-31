import { useQuery } from '@tanstack/react-query'
import { fetchHealth, type ComponentStatus } from '../lib/api'

const STATUS_STYLES: Record<ComponentStatus['status'], string> = {
  up: 'bg-edge-positive/15 text-edge-positive',
  down: 'bg-edge-negative/15 text-edge-negative',
  disabled: 'bg-white/10 text-slate-400',
}

function StatusCard({ name, component }: { name: string; component: ComponentStatus }) {
  return (
    <div className="rounded-lg border border-white/10 bg-surface-raised p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-300">{name}</h3>
        <span
          className={`rounded px-2 py-0.5 text-xs font-semibold uppercase ${STATUS_STYLES[component.status]}`}
        >
          {component.status}
        </span>
      </div>
      {component.detail && <p className="mt-2 text-xs text-slate-500">{component.detail}</p>}
    </div>
  )
}

export default function Dashboard() {
  const { data, isPending, isError, error } = useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    refetchInterval: 30_000,
  })

  if (isPending) {
    return (
      <div className="grid gap-4 sm:grid-cols-2" aria-busy="true">
        {[0, 1].map((i) => (
          <div key={i} className="h-20 animate-pulse rounded-lg bg-surface-raised" />
        ))}
      </div>
    )
  }

  if (isError) {
    return (
      <div className="rounded-lg border border-edge-negative/40 bg-edge-negative/10 p-4">
        <h2 className="font-medium text-edge-negative">API unreachable</h2>
        <p className="mt-1 text-sm text-slate-400">{error.message}</p>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <h2 className="text-base font-semibold text-white">System status</h2>
        <span className="text-xs text-slate-500">
          {data.app} v{data.version} — {data.status}
        </span>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <StatusCard name="Database" component={data.database} />
        <StatusCard name="Redis cache" component={data.redis} />
      </div>
      <div className="rounded-lg border border-white/10 bg-surface-raised p-4">
        <h3 className="text-sm font-medium text-slate-300">Data providers</h3>
        {data.providers.length === 0 ? (
          <p className="mt-2 text-xs text-slate-500">
            No providers configured yet — adapters arrive in Phase 3. The app runs fully
            featureless-but-honest until keys are added in Settings.
          </p>
        ) : (
          <ul className="mt-2 space-y-1">
            {data.providers.map((p) => (
              <li key={p.name} className="text-xs text-slate-400">
                {p.name}: {p.status}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
