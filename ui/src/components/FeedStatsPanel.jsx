/**
 * FeedStatsPanel — shows cTrader connection stats when source=ctrader.
 * Automatically hides when using internal/demo data.
 * GET /api/v1/data-source
 */
import { usePoll } from '../usePoll.js'
import { BASE_URL } from '../api.js'

function fetchDataSource() {
  return fetch(`${BASE_URL}/api/v1/data-source`, { cache: 'no-store' }).then(r => r.json())
}

function fmtTs(isoStr) {
  if (!isoStr) return '—'
  return new Date(isoStr).toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

export default function FeedStatsPanel() {
  const { data } = usePoll(fetchDataSource, 5000)

  // Only render when cTrader source is configured
  if (!data || data.source !== 'ctrader') return null

  const connected = data.connected
  const stats     = data.stats || {}
  const buffers   = stats.candle_buffer_counts || {}

  return (
    <div className="panel">
      <div className="panel-header">
        <span className="panel-title">cTrader Live Feed</span>
        <span className={`pill ${connected ? 'green' : 'red'}`}>
          {connected ? '📡 CONNECTED' : '⚠ DISCONNECTED'}
        </span>
      </div>
      <div className="panel-body">
        <div className="kv-grid">
          <div className="kv-item">
            <div className="kv-label">Ticks Received</div>
            <div className="kv-value">{(stats.ticks_received ?? 0).toLocaleString()}</div>
          </div>
          <div className="kv-item">
            <div className="kv-label">Candles Built</div>
            <div className="kv-value">{(stats.candles_completed ?? 0).toLocaleString()}</div>
          </div>
          <div className="kv-item">
            <div className="kv-label">Reconnects</div>
            <div className={`kv-value ${(stats.reconnects ?? 0) > 0 ? 'yellow' : 'green'}`}>
              {stats.reconnects ?? 0}
            </div>
          </div>
          <div className="kv-item">
            <div className="kv-label">Last Tick</div>
            <div className="kv-value">{fmtTs(stats.last_tick_ts)}</div>
          </div>
          <div className="kv-item">
            <div className="kv-label">Last Candle</div>
            <div className="kv-value">{fmtTs(stats.last_candle_ts)}</div>
          </div>
          <div className="kv-item">
            <div className="kv-label">Connected Since</div>
            <div className="kv-value">{fmtTs(stats.connected_since)}</div>
          </div>

          {Object.keys(buffers).length > 0 && (
            <div className="kv-item wide">
              <div className="kv-label">Candle Buffer Sizes</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 6 }}>
                {Object.entries(buffers).map(([key, count]) => (
                  <span
                    key={key}
                    className={`pill ${count >= 50 ? 'green' : count >= 10 ? 'yellow' : 'red'}`}
                    style={{ fontSize: 11 }}
                  >
                    {key}: {count}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>

        <hr className="divider" />
        <p className="muted" style={{ fontSize: 11 }}>
          Read-only mode — signal generation only. Order placement is disabled.
        </p>
      </div>
    </div>
  )
}
