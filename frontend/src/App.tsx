import { NavLink, Route, Routes } from 'react-router-dom'
import BetBuilder from './pages/BetBuilder'
import Dashboard from './pages/Dashboard'
import FixtureDetail from './pages/FixtureDetail'
import Schedule from './pages/Schedule'
import Settings from './pages/Settings'
import Tracker from './pages/Tracker'

const NAV = [
  { to: '/', label: 'Dashboard' },
  { to: '/schedule', label: 'Schedule' },
  { to: '/builder', label: 'Bet builder' },
  { to: '/tracker', label: 'Tracker' },
  { to: '/settings', label: 'Settings' },
]

export default function App() {
  return (
    <div className="min-h-screen">
      <header className="border-b border-white/10 bg-surface-raised">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
          <h1 className="text-lg font-semibold tracking-tight text-white">
            GameStakes
            <span className="ml-2 rounded bg-white/10 px-1.5 py-0.5 text-xs font-normal text-slate-400">
              alpha
            </span>
          </h1>
          <nav className="flex gap-1" aria-label="Main">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === '/'}
                className={({ isActive }) =>
                  `rounded px-2.5 py-1 text-sm ${
                    isActive
                      ? 'bg-white/15 text-white'
                      : 'text-slate-400 hover:text-slate-200'
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <p className="ml-auto hidden text-xs text-slate-500 sm:block">
            predictions with honest uncertainty
          </p>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/schedule" element={<Schedule />} />
          <Route path="/fixtures/:id" element={<FixtureDetail />} />
          <Route path="/builder" element={<BetBuilder />} />
          <Route path="/tracker" element={<Tracker />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
      <footer className="mx-auto max-w-6xl px-4 pb-8 pt-2">
        <p className="text-xs text-slate-600">
          Models can be wrong. Losing runs are expected even with a genuine edge. Nothing here
          is financial advice.
        </p>
      </footer>
    </div>
  )
}
