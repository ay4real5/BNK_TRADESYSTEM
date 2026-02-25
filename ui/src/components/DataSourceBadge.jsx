/**
 * DataSourceBadge — polls /api/v1/data-source and shows a prominent indicator.
 * Renders inline in the header alongside the live/dead indicator.
 */
import { api } from '../api.js'
import { usePoll } from '../usePoll.js'

export default function DataSourceBadge() {
  const { data } = usePoll(() => api.health().then(() => fetch('/api/v1/data-source', { cache: 'no-store' }).then(r => r.json())), 10000)

  if (!data) return null

  const isCtrader = data.source === 'ctrader'
  const connected = data.connected

  if (isCtrader) {
    const stats = data.stats || {}
    return (
      <span
        title={`ticks: ${stats.ticks_received ?? '—'}  candles: ${stats.candles_completed ?? '—'}  reconnects: ${stats.reconnects ?? 0}`}
        className={`pill ${connected ? 'green' : 'red'}`}
        style={{ fontSize: 11 }}
      >
        {connected ? '📡 CTRADER LIVE' : '📡 CTRADER (connecting…)'}
      </span>
    )
  }

  return (
    <span className="pill muted" style={{ fontSize: 11 }}>
      🔬 INTERNAL / DEMO
    </span>
  )
}
