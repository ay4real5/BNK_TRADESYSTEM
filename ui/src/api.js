/**
 * api.js — thin fetch wrapper for all BNK TradeSystem endpoints.
 *
 * BASE_URL is read from:
 *   1. window.VITE_API_URL  (set at runtime for Codespaces / remote deployments)
 *   2. env var VITE_API_URL (set at build time inside .env)
 *   3. '' — same origin (works when Vite proxy is active, i.e. local dev)
 *
 * To point the UI at a Codespaces backend URL, open browser console and run:
 *   localStorage.setItem('bnk_api_url', 'https://<codespace>-8000.app.github.dev')
 *   location.reload()
 */

const BASE_URL =
  localStorage.getItem('bnk_api_url') ||
  (typeof import.meta !== 'undefined' && import.meta.env?.VITE_API_URL) ||
  ''

const PREFIX = `${BASE_URL}/api/v1`

async function get(path) {
  const res = await fetch(`${PREFIX}${path}`, { cache: 'no-store' })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${PREFIX}${path}`)
  return res.json()
}

async function post(path, body = {}) {
  const res = await fetch(`${PREFIX}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
  })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${PREFIX}${path}`)
  return res.json()
}

export const api = {
  health:        () => get('/health'),
  status:        () => get('/status'),
  account:       () => get('/account'),
  signals:       (limit = 20) => get(`/signals/recent?limit=${limit}`),
  reportToday:   () => get('/report/today'),
  getMode:       () => get('/mode'),
  setMode:       (mode) => post(`/mode/${mode}`),
  dataSource:    () => get('/data-source'),
}

export { BASE_URL }
