/**
 * Panel 3 — Recent Signals
 * GET /api/v1/signals/recent?limit=20
 */
import { api } from '../api.js'
import { usePoll } from '../usePoll.js'

const STATUS_COLOR = {
  pending:  'yellow',
  active:   'blue',
  filled:   'blue',
  won:      'green',
  closed:   'green',
  lost:     'red',
  rejected: 'red',
  expired:  'muted',
  cancelled:'muted',
}

const SIDE_COLOR = { buy: 'green', sell: 'red' }

function ts(isoStr) {
  if (!isoStr) return '—'
  const d = new Date(isoStr)
  return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function fmt(v, d = 5) {
  if (v == null) return '—'
  return Number(v).toFixed(d)
}

export default function SignalsPanel() {
  const { data: signals, error, loading, countdown } = usePoll(() => api.signals(20), 5000)

  return (
    <div className="panel" style={{ flex: 1 }}>
      <div className="panel-header">
        <span className="panel-title">Recent Signals</span>
        <span className="countdown">↻ {countdown}s</span>
      </div>
      <div className="panel-body" style={{ padding: '0 0 8px' }}>
        {error && <div className="error-bar" style={{ margin: '8px 16px' }}>⚠ {error}</div>}
        {loading && !signals && <div className="loading" style={{ padding: '12px 16px' }}>Loading…</div>}
        {signals && signals.length === 0 && (
          <div className="loading" style={{ padding: '12px 16px' }}>No signals yet.</div>
        )}
        {signals && signals.length > 0 && (
          <div style={{ overflowX: 'auto' }}>
            <table className="sig-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>Entry</th>
                  <th>SL</th>
                  <th>TP</th>
                  <th>Score</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {signals.map(sig => (
                  <tr key={sig.id}>
                    <td className="muted">{ts(sig.ts)}</td>
                    <td style={{ fontWeight: 700 }}>{sig.symbol}</td>
                    <td className={SIDE_COLOR[sig.side] ?? ''}>{(sig.side ?? '').toUpperCase()}</td>
                    <td>{fmt(sig.entry)}</td>
                    <td className="red">{fmt(sig.sl)}</td>
                    <td className="green">{fmt(sig.tp)}</td>
                    <td className={sig.score >= 75 ? 'green' : sig.score >= 50 ? 'yellow' : 'red'}>
                      {sig.score != null ? sig.score : '—'}
                    </td>
                    <td>
                      <span className={`pill ${STATUS_COLOR[sig.status] ?? 'muted'}`}>
                        {sig.status ?? '—'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
