/**
 * Panel 1 — Account State
 * GET /api/v1/account
 */
import { api } from '../api.js'
import { usePoll } from '../usePoll.js'

function fmt(v, decimals = 2) {
  if (v == null || v === undefined) return '—'
  return Number(v).toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
}

function ddColor(pct) {
  if (pct >= 7) return 'red'
  if (pct >= 4) return 'yellow'
  return 'green'
}

export default function AccountPanel() {
  const { data: a, error, loading } = usePoll(() => api.account(), 5000)

  const isExpansion = a?.expansion_active

  return (
    <div className="panel">
      <div className="panel-header">
        <span className="panel-title">Account</span>
        {a && (
          <span className={`pill ${isExpansion ? 'purple' : 'blue'}`}>
            {isExpansion ? '⚡ EXPANSION' : '🛡 DEFENSIVE'}
          </span>
        )}
      </div>
      <div className="panel-body">
        {error && <div className="error-bar">⚠ {error}</div>}
        {loading && !a && <div className="loading">Loading…</div>}
        {a && (
          <div className="kv-grid">
            <div className={`kv-item ${a.equity >= a.balance ? 'accent-green' : 'accent-red'}`}>
              <div className="kv-label">Equity</div>
              <div className={`kv-value ${a.equity >= a.balance ? 'green' : 'red'}`}>
                ${fmt(a.equity)}
              </div>
            </div>
            <div className="kv-item">
              <div className="kv-label">Balance</div>
              <div className="kv-value">${fmt(a.balance)}</div>
            </div>

            <div className={`kv-item ${a.total_pnl >= 0 ? 'accent-green' : 'accent-red'}`}>
              <div className="kv-label">Total PnL</div>
              <div className={`kv-value ${a.total_pnl >= 0 ? 'green' : 'red'}`}>
                {a.total_pnl >= 0 ? '+' : ''}${fmt(a.total_pnl)}
              </div>
            </div>
            <div className="kv-item">
              <div className="kv-label">Peak Equity</div>
              <div className="kv-value">${fmt(a.peak_equity)}</div>
            </div>

            <div className={`kv-item wide accent-${ddColor(a.drawdown_pct)}`}>
              <div className="kv-label">Drawdown from Peak</div>
              <div className={`kv-value ${ddColor(a.drawdown_pct)}`}>
                {fmt(a.drawdown_pct)}%
                <span className="muted" style={{ fontSize: 11, marginLeft: 8 }}>
                  limit {fmt(a.max_total_drawdown_pct)}%
                </span>
              </div>
              <div className="progress-track">
                <div
                  className="progress-fill"
                  style={{
                    width: `${Math.min(100, (a.drawdown_pct / a.max_total_drawdown_pct) * 100)}%`,
                    background: a.drawdown_pct >= 7
                      ? 'var(--red)'
                      : a.drawdown_pct >= 4 ? 'var(--yellow)' : 'var(--green)',
                  }}
                />
              </div>
            </div>

            <div className="kv-item">
              <div className="kv-label">Consec. Losses</div>
              <div className={`kv-value ${a.consecutive_losses >= a.consecutive_loss_threshold ? 'red' : a.consecutive_losses > 0 ? 'yellow' : 'green'}`}>
                {a.consecutive_losses}
                <span className="muted" style={{ fontSize: 11 }}>
                  {' '}/ {a.consecutive_loss_threshold}
                </span>
              </div>
            </div>
            <div className="kv-item">
              <div className="kv-label">Risk / trade</div>
              <div className="kv-value">{fmt(a.effective_risk_pct, 2)}%</div>
            </div>

            <div className="kv-item">
              <div className="kv-label">Next Win</div>
              <div className="kv-value green">+${fmt(a.next_trade_win)}</div>
            </div>
            <div className="kv-item">
              <div className="kv-label">Next Loss</div>
              <div className="kv-value red">-${fmt(a.next_trade_loss)}</div>
            </div>

            {isExpansion && (
              <div className="kv-item wide accent-purple">
                <div className="kv-label">Expansion Window</div>
                <div className="kv-value purple">
                  {a.expansion_trades_in_window} of {a.expansion_trades_in_window + (a.expansion_trades_remaining ?? 0)} trades
                  <span className="muted" style={{ fontSize: 11, marginLeft: 8 }}>
                    {a.expansion_trades_remaining} remaining
                  </span>
                </div>
                <div className="progress-track">
                  <div
                    className="progress-fill"
                    style={{
                      width: `${Math.min(100, (a.expansion_trades_in_window / (a.expansion_trades_in_window + (a.expansion_trades_remaining ?? 1))) * 100)}%`,
                      background: 'var(--purple)',
                    }}
                  />
                </div>
              </div>
            )}

            <div className="kv-item">
              <div className="kv-label">Daily Limit</div>
              <div className="kv-value yellow">${fmt(a.daily_loss_limit)}</div>
            </div>
            <div className="kv-item">
              <div className="kv-label">Intraday DD</div>
              <div className="kv-value yellow">${fmt(a.intraday_dd_limit)}</div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
