import { useState, useEffect } from 'react'
import { api } from './api.js'
import { usePoll } from './usePoll.js'
import AccountPanel    from './components/AccountPanel.jsx'
import StatusPanel     from './components/StatusPanel.jsx'
import SignalsPanel    from './components/SignalsPanel.jsx'
import TodayReportPanel from './components/TodayReportPanel.jsx'
import ModeControlsPanel from './components/ModeControlsPanel.jsx'
import EquityChart      from './components/EquityChart.jsx'
import DataSourceBadge  from './components/DataSourceBadge.jsx'
import FeedStatsPanel   from './components/FeedStatsPanel.jsx'

function useServerReachable() {
  const [alive, setAlive] = useState(null)
  useEffect(() => {
    let cancelled = false
    async function check() {
      try {
        await api.health()
        if (!cancelled) setAlive(true)
      } catch {
        if (!cancelled) setAlive(false)
      }
    }
    check()
    const iv = setInterval(check, 10_000)
    return () => { cancelled = true; clearInterval(iv) }
  }, [])
  return alive
}

export default function App() {
  const alive = useServerReachable()
  const [now, setNow] = useState(new Date())
  useEffect(() => {
    const iv = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(iv)
  }, [])

  return (
    <div className="dashboard">
      {/* ── Header ─────────────────────────────────────────── */}
      <div className="header">
        <h1>📊 BNK TradeSystem — Operator Dashboard</h1>
        <div className="header-right">
          <DataSourceBadge />
          <span>
            <span className={`pulse ${alive === false ? 'dead' : ''}`} />
            {alive === null ? 'Connecting…' : alive ? 'Live' : 'Backend offline'}
          </span>
          <span>{now.toLocaleTimeString('en-GB')}</span>
          <span className="muted">{now.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })}</span>
        </div>
      </div>

      {/* ── Main grid ──────────────────────────────────────── */}
      <div className="grid-main">
        {/* Left column */}
        <div className="grid-left">
          <AccountPanel />
          <TodayReportPanel />
          <ModeControlsPanel />
          <FeedStatsPanel />
        </div>

        {/* Right column */}
        <div className="grid-right">
          <StatusPanel />
          <EquityChart startingBalance={10000} />
          <SignalsPanel />
        </div>
      </div>
    </div>
  )
}
