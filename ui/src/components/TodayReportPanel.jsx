/**
 * Panel 4 — Today's Report
 * GET /api/v1/report/today
 */
import { api } from '../api.js'
import { usePoll } from '../usePoll.js'

function fmt(v, d = 2) {
  if (v == null) return '—'
  return Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })
}

export default function TodayReportPanel() {
  const { data: r, error, loading } = usePoll(() => api.reportToday(), 5000)

  const total    = r?.total   ?? 0
  const wins     = r?.wins    ?? 0
  const losses   = r?.losses  ?? 0
  const pnl      = r?.total_pnl ?? 0
  const winRate  = total > 0 ? (wins / total * 100) : 0

  return (
    <div className="panel">
      <div className="panel-header">
        <span className="panel-title">Today's Report</span>
        {r && (
          <span className={`pill ${pnl >= 0 ? 'green' : 'red'}`}>
            {pnl >= 0 ? '+' : ''}${fmt(pnl)}
          </span>
        )}
      </div>
      <div className="panel-body">
        {error && <div className="error-bar">⚠ {error}</div>}
        {loading && !r && <div className="loading">Loading…</div>}
        {r && (
          <>
            <div className="stat-row">
              <div className="stat-box">
                <div className="num">{total}</div>
                <div className="lbl">Total</div>
              </div>
              <div className="stat-box">
                <div className="num green">{wins}</div>
                <div className="lbl">Wins</div>
              </div>
              <div className="stat-box">
                <div className="num red">{losses}</div>
                <div className="lbl">Losses</div>
              </div>
              <div className="stat-box">
                <div className={`num ${winRate >= 60 ? 'green' : winRate >= 50 ? 'yellow' : 'red'}`}>
                  {total > 0 ? `${fmt(winRate, 1)}%` : '—'}
                </div>
                <div className="lbl">Win Rate</div>
              </div>
            </div>

            {total > 0 && (
              <>
                <hr className="divider" />
                <div className="kv-grid">
                  <div className={`kv-item wide ${pnl >= 0 ? 'accent-green' : 'accent-red'}`}>
                    <div className="kv-label">Total PnL Today</div>
                    <div className={`kv-value ${pnl >= 0 ? 'green' : 'red'}`} style={{ fontSize: 20 }}>
                      {pnl >= 0 ? '+' : ''}${fmt(pnl)}
                    </div>
                    {/* Win rate bar */}
                    <div className="progress-track" style={{ marginTop: 8 }}>
                      <div
                        className="progress-fill"
                        style={{
                          width: `${winRate}%`,
                          background: winRate >= 60 ? 'var(--green)' : winRate >= 50 ? 'var(--yellow)' : 'var(--red)',
                        }}
                      />
                    </div>
                    <div className="muted" style={{ fontSize: 10, marginTop: 4 }}>
                      Win rate: {fmt(winRate, 1)}% ({wins}W / {losses}L)
                    </div>
                  </div>

                  {r.avg_win != null && (
                    <div className="kv-item">
                      <div className="kv-label">Avg Win</div>
                      <div className="kv-value green">+${fmt(r.avg_win)}</div>
                    </div>
                  )}
                  {r.avg_loss != null && (
                    <div className="kv-item">
                      <div className="kv-label">Avg Loss</div>
                      <div className="kv-value red">-${fmt(Math.abs(r.avg_loss))}</div>
                    </div>
                  )}
                  {r.best_trade != null && (
                    <div className="kv-item">
                      <div className="kv-label">Best Trade</div>
                      <div className="kv-value green">+${fmt(r.best_trade)}</div>
                    </div>
                  )}
                  {r.worst_trade != null && (
                    <div className="kv-item">
                      <div className="kv-label">Worst Trade</div>
                      <div className="kv-value red">-${fmt(Math.abs(r.worst_trade))}</div>
                    </div>
                  )}
                </div>
              </>
            )}

            {total === 0 && (
              <p className="muted" style={{ marginTop: 12, fontSize: 12 }}>
                No trades recorded today.
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )
}
