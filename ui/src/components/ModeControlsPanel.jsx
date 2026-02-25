/**
 * Panel 5 — Mode Controls
 * GET  /api/v1/mode
 * POST /api/v1/mode/{mode_name}
 */
import { useState } from 'react'
import { api } from '../api.js'
import { usePoll } from '../usePoll.js'

const MODES = [
  { id: 'defensive', label: '🛡 Defensive', desc: 'Conservative sizing, risk reduced on consec. losses.' },
  { id: 'live',      label: '⚡ Live',      desc: 'Standard live trading mode.' },
  { id: 'paper',     label: '📄 Paper',     desc: 'Simulated fills, no real orders.' },
  { id: 'demo',      label: '🔬 Demo',      desc: 'Demo engine — synthetic signals.' },
]

export default function ModeControlsPanel() {
  const { data: current, error } = usePoll(() => api.getMode(), 5000)
  const [busy, setBusy]           = useState(false)
  const [msg, setMsg]             = useState(null)

  async function handleMode(mode) {
    setBusy(true)
    setMsg(null)
    try {
      const res = await api.setMode(mode)
      if (res.success) setMsg({ ok: true, text: `Mode set to ${res.mode}` })
      else             setMsg({ ok: false, text: res.error ?? 'Unknown error' })
    } catch (e) {
      setMsg({ ok: false, text: e.message })
    } finally {
      setBusy(false)
    }
  }

  const activeMode = current?.mode ?? null

  return (
    <div className="panel">
      <div className="panel-header">
        <span className="panel-title">Mode Controls</span>
        {activeMode && (
          <span className="pill blue">{activeMode.toUpperCase()}</span>
        )}
      </div>
      <div className="panel-body">
        {error && <div className="error-bar">⚠ {error}</div>}
        {msg && (
          <div
            className={`error-bar`}
            style={msg.ok
              ? { background: 'rgba(34,197,94,.1)', borderColor: 'var(--green)', color: 'var(--green)' }
              : {}}
          >
            {msg.ok ? '✓ ' : '⚠ '}{msg.text}
          </div>
        )}
        <p className="muted" style={{ fontSize: 11, marginBottom: 12 }}>
          Select a mode to reconfigure the live trading engine.
          Changes take effect immediately.
        </p>
        <div className="mode-btn-row">
          {MODES.map(m => (
            <button
              key={m.id}
              className={`mode-btn ${activeMode === m.id ? 'active-mode' : ''}`}
              disabled={busy || activeMode === m.id}
              onClick={() => handleMode(m.id)}
              title={m.desc}
            >
              {m.label}
            </button>
          ))}
        </div>
        {activeMode && (
          <p className="muted" style={{ fontSize: 11, marginTop: 12 }}>
            {MODES.find(m => m.id === activeMode)?.desc}
          </p>
        )}
      </div>
    </div>
  )
}
