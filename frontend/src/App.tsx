import { Route, Routes } from 'react-router-dom'
import Dashboard from './pages/Dashboard'

export default function App() {
  return (
    <div className="min-h-screen">
      <header className="border-b border-white/10 bg-surface-raised">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
          <h1 className="text-lg font-semibold tracking-tight text-white">
            GameStakes
            <span className="ml-2 rounded bg-white/10 px-1.5 py-0.5 text-xs font-normal text-slate-400">
              alpha
            </span>
          </h1>
          <p className="text-xs text-slate-500">predictions with honest uncertainty</p>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">
        <Routes>
          <Route path="/" element={<Dashboard />} />
        </Routes>
      </main>
    </div>
  )
}
