import type { ReactNode } from 'react'

export function Card({
  title,
  action,
  children,
  className = '',
}: {
  title?: string
  action?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`rounded-lg border border-white/10 bg-surface-raised ${className}`}>
      {(title || action) && (
        <header className="flex items-center justify-between border-b border-white/10 px-4 py-2.5">
          {title && <h2 className="text-sm font-medium text-slate-300">{title}</h2>}
          {action}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="h-10 animate-pulse rounded bg-white/5" />
      ))}
    </div>
  )
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="py-8 text-center">
      <p className="text-sm text-slate-400">{title}</p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  )
}

export function ErrorState({ title, detail }: { title: string; detail?: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-edge-negative/40 bg-edge-negative/10 p-4"
    >
      <p className="text-sm font-medium text-edge-negative">{title}</p>
      {detail && <p className="mt-1 text-xs text-slate-400">{detail}</p>}
    </div>
  )
}

const BADGE_TONES = {
  neutral: 'bg-white/10 text-slate-300',
  positive: 'bg-edge-positive/15 text-edge-positive',
  negative: 'bg-edge-negative/15 text-edge-negative',
  warning: 'bg-amber-500/15 text-amber-400',
} as const

export function Badge({
  children,
  tone = 'neutral',
}: {
  children: ReactNode
  tone?: keyof typeof BADGE_TONES
}) {
  return (
    <span
      className={`inline-block rounded px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${BADGE_TONES[tone]}`}
    >
      {children}
    </span>
  )
}

/** Colour carries no information on its own — the numeric value is always shown. */
export function EdgeValue({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) return <span className="text-slate-500">—</span>
  const tone = value > 0 ? 'text-edge-positive' : value < 0 ? 'text-edge-negative' : 'text-slate-300'
  return (
    <span className={`tabular-nums font-medium ${tone}`}>
      {value > 0 ? '+' : ''}
      {(value * 100).toFixed(1)}%
    </span>
  )
}
