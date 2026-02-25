/**
 * EquityChart — mini sparkline from recent signals' PnL
 * Requires recharts
 */
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ReferenceLine,
} from 'recharts'
import { api } from '../api.js'
import { usePoll } from '../usePoll.js'

export default function EquityChart({ startingBalance = 10000 }) {
  const { data: signals } = usePoll(() => api.signals(100), 5000)

  // Build cumulative equity curve from signals that have a trade_id (executed)
  // We approximate from score alone if no pnl is available
  const points = []
  if (signals && signals.length > 0) {
    let equity = startingBalance
    const executed = signals
      .filter(s => ['won', 'lost', 'closed'].includes(s.status))
      .reverse()      // oldest first

    for (const sig of executed) {
      if (sig.status === 'won') {
        equity += equity * 0.009 * 1.8   // approx win
      } else if (sig.status === 'lost') {
        equity -= equity * 0.009          // approx loss
      }
      points.push({
        t: new Date(sig.ts).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }),
        equity: Math.round(equity * 100) / 100,
      })
    }
  }

  if (points.length < 2) return null

  const minV = Math.min(...points.map(p => p.equity))
  const maxV = Math.max(...points.map(p => p.equity))
  const rising = points[points.length - 1].equity >= points[0].equity

  return (
    <div className="panel" style={{ marginTop: 0 }}>
      <div className="panel-header">
        <span className="panel-title">Equity Curve (session)</span>
        <span className={`pill ${rising ? 'green' : 'red'}`}>
          {rising ? '▲' : '▼'} ${points[points.length - 1].equity.toLocaleString()}
        </span>
      </div>
      <div className="panel-body" style={{ paddingTop: 8, paddingBottom: 4 }}>
        <div className="chart-wrap">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor={rising ? '#22c55e' : '#ef4444'} stopOpacity={0.25} />
                  <stop offset="95%" stopColor={rising ? '#22c55e' : '#ef4444'} stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <XAxis dataKey="t" tick={{ fill: '#8a9bb5', fontSize: 10 }} tickLine={false} axisLine={false} interval="preserveStartEnd" />
              <YAxis domain={[minV * 0.999, maxV * 1.001]} tick={{ fill: '#8a9bb5', fontSize: 10 }} tickLine={false} axisLine={false} width={60}
                tickFormatter={v => `$${(v/1000).toFixed(1)}k`} />
              <Tooltip
                contentStyle={{ background: '#1e2535', border: '1px solid #2a3347', borderRadius: 6, fontSize: 11 }}
                labelStyle={{ color: '#8a9bb5' }}
                itemStyle={{ color: rising ? '#22c55e' : '#ef4444' }}
                formatter={v => [`$${v.toLocaleString()}`, 'Equity']}
              />
              <ReferenceLine y={startingBalance} stroke="#2a3347" strokeDasharray="3 3" />
              <Area
                type="monotone"
                dataKey="equity"
                stroke={rising ? '#22c55e' : '#ef4444'}
                strokeWidth={2}
                fill="url(#eqGrad)"
                dot={false}
                activeDot={{ r: 4, fill: rising ? '#22c55e' : '#ef4444' }}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  )
}
