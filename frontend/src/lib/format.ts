/** Formatting helpers. Times render in the viewer's own timezone. */

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
  })
}

export function toISODate(date: Date): string {
  // Local calendar date, not UTC — "today" must mean the user's today.
  const offset = date.getTimezoneOffset() * 60_000
  return new Date(date.getTime() - offset).toISOString().slice(0, 10)
}

export function addDays(date: Date, days: number): Date {
  const next = new Date(date)
  next.setDate(next.getDate() + days)
  return next
}

const STATUS_LABELS: Record<string, string> = {
  scheduled: 'Scheduled',
  in_play: 'Live',
  finished: 'Finished',
  postponed: 'Postponed',
  cancelled: 'Cancelled',
  unknown: 'Unknown',
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}
