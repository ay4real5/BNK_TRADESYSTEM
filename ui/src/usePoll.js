import { useState, useEffect, useCallback, useRef } from 'react'

/**
 * usePoll(fetcher, intervalMs)
 * Polls `fetcher()` every intervalMs milliseconds.
 * Returns { data, error, loading, lastUpdated, countdown }
 */
export function usePoll(fetcher, intervalMs = 5000) {
  const [data, setData]               = useState(null)
  const [error, setError]             = useState(null)
  const [loading, setLoading]         = useState(true)
  const [lastUpdated, setLastUpdated] = useState(null)
  const [countdown, setCountdown]     = useState(intervalMs / 1000)

  const timerRef      = useRef(null)
  const countdownRef  = useRef(null)
  const fetcherRef    = useRef(fetcher)
  fetcherRef.current  = fetcher

  const doFetch = useCallback(async () => {
    try {
      const result = await fetcherRef.current()
      setData(result)
      setError(null)
      setLastUpdated(new Date())
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
      setCountdown(intervalMs / 1000)
    }
  }, [intervalMs])

  useEffect(() => {
    doFetch()
    timerRef.current = setInterval(doFetch, intervalMs)

    // Tick a countdown display each second
    countdownRef.current = setInterval(() => {
      setCountdown(prev => Math.max(0, prev - 1))
    }, 1000)

    return () => {
      clearInterval(timerRef.current)
      clearInterval(countdownRef.current)
    }
  }, [doFetch, intervalMs])

  return { data, error, loading, lastUpdated, countdown }
}
