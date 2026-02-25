/**
 * Panel 2 — System Status
 * GET /api/v1/status
 */
import { api } from '../api.js'
import { usePoll } from '../usePoll.js'

function fmt(v, d = 2) {
  if (v == null) return '—'
  return Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })
}

export default function StatusPanel() {
  const { data: s, error, loading } = usePoll(() => api.status(), 5000)

  return (
    <div className="panel">
      <div className="panel-header">
        <span className="panel-title">System Status</span>
        {s && (
          <span className={`pill ${s.kill_switch ? 'red' : s.is_locked ? 'yellow' : 'green'}`}>
            {s.kill_switch ? '☠ KILL-SWITCH' : s.is_locked ? '🔒 LOCKED' : '✓ ACTIVE'}
          </span>
        )}
      </div>
      <div className="panel-body">
        {error && <div className="error-bar">⚠ {error}</div>}
        {loading && !s && <div className="loading">Loading…</div>}
        {s && (
          <>
            <div className="stat-row">
              <div className="stat-box">
                <div className="num">{s.trades_today ?? 0}</div>
                <div className="lbl">Trades</div>
              </div>
              <div className="stat-box">
                <div className="num green">{s.wins_today ?? 0}</div>
                <div className="lbl">Wins</div>
              </div>
              <div className="stat-box">
                <div className="num red">{s.losses_today ?? 0}</div>
                <div className="lbl">Losses</div>
              </div>
              <div className="stat-box">
                <div className={`num ${(s.pnl_today ?? 0) >= 0 ? 'green' : 'red'}`}>
                  {(s.pnl_today ?? 0) >= 0 ? '+' : ''}${fmt(s.pnl_today)}
                </div>
                <div className="lbl">PnL Today</div>
              </div>
            </div>

            <hr className="divider" />

            <div className="kv-grid">
              <div className={`kv-item ${s.kill_switch ? 'accent-red' : ''}`}>
                <div className="kv-label">Kill Switch</div>
                <div className={`kv-value ${s.kill_switch ? 'red' : 'green'}`}>
                  {s.kill_switch ? '⛔ TRIGGERED' : 'OK'}
                </div>
              </div>
              <div className={`kv-item ${s.is_locked ? 'accent-yellow' : ''}`}>
                <div className="kv-label">Lock State</div>
                <div className={`kv-value ${s.is_locked ? 'yellow' : 'green'}`}>
                  {s.is_locked ? `🔒 ${s.lock_reason ?? 'locked'}` : 'Unlocked'}
                </div>
              </div>
              <div className="kv-item">
                <div className="kv-label">Mode</div>
                <div className="kv-value blue">{(s.mode ?? '—').toUpperCase()}</div>
              </div>
              <div className="kv-item">
                <div className="kv-label">Risk Mode</div>
                <div className={`kv-value ${s.risk_mode === 'expansion' ? 'purple' : 'blue'}`}>
                  {(s.risk_mode ?? '—').toUpperCase()}
                </div>
              </div>
              <div className="kv-item">
                <div className="kv-label">Daily Loss Limit</div>
                <div className="kv-value yellow">${fmt(s.daily_loss_limit)}</div>
              </div>
              <div className="kv-item">
                <div className="kv-label">Max Trades / Day</div>
                <div className="kv-value">{s.max_trades_per_day ?? '—'}</div>
              </div>
              <div className="kv-item">
                <div className="kv-label">Account Equity</div>
                <div className="kv-value">${fmt(s.equity)}</div>
              </div>
              <div className="kv-item">
                <div className="kv-label">Drawdown %</div>
                <div className={`kv-value ${(s.drawdown_pct ?? 0) >= 7 ? 'red' : (s.drawdown_pct ?? 0) >= 4 ? 'yellow' : 'green'}`}>
                  {fmt(s.drawdown_pct)}%
                </div>
              </div>
              {s.expansion_trades_remaining != null && (
                <div className="kv-item wide accent-purple">
                  <div className="kv-label">Expansion Trades Remaining</div>
                  <div className="kv-value purple">{s.expansion_trades_remaining}</div>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
