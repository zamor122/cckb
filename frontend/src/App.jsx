import { useState, useEffect, useRef } from 'react'

const STATUS_LABELS = {
  idle:       { label: 'Ready',       color: '#6b7280' },
  queued:     { label: 'Queued',      color: '#6366f1' },
  processing: { label: 'Processing',  color: '#f59e0b' },
  completed:  { label: 'Complete',    color: '#10b981' },
  failed:     { label: 'Failed',      color: '#ef4444' },
}

export default function App() {
  const [repoPath,  setRepoPath]  = useState('')
  const [jobId,     setJobId]     = useState(null)
  const [status,    setStatus]    = useState('idle')
  const [progress,  setProgress]  = useState(0)
  const [message,   setMessage]   = useState('')
  const [error,     setError]     = useState(null)
  const [specCount, setSpecCount] = useState(null)

  const intervalRef = useRef(null)

  // ── Polling loop ───────────────────────────────────────────────────────────
  useEffect(() => {
    if (jobId && status !== 'completed' && status !== 'failed') {
      intervalRef.current = setInterval(async () => {
        try {
          const res  = await fetch(`/api/status/${jobId}`)
          const data = await res.json()
          setStatus(data.status)
          setProgress(data.progress ?? 0)
          setMessage(data.message ?? '')

          if (data.status === 'completed') {
            // Extract spec count from message if present
            const match = data.message?.match(/(\d+)\s+feature spec/)
            if (match) setSpecCount(parseInt(match[1], 10))
          }
        } catch (e) {
          console.error('Polling error:', e)
        }
      }, 1500)
    }
    return () => clearInterval(intervalRef.current)
  }, [jobId, status])

  // ── Submit handler ─────────────────────────────────────────────────────────
  const handleSubmit = async (e) => {
    e.preventDefault()
    setError(null)
    setStatus('queued')
    setProgress(0)
    setMessage('Queuing scan...')
    setSpecCount(null)

    try {
      const res  = await fetch('/api/init', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ repo_path: repoPath }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? 'Failed to queue scan')
      setJobId(data.job_id)
      setStatus(data.status)
    } catch (err) {
      setError(err.message)
      setStatus('idle')
      setMessage('')
    }
  }

  const reset = () => {
    clearInterval(intervalRef.current)
    setJobId(null)
    setStatus('idle')
    setProgress(0)
    setMessage('')
    setError(null)
    setSpecCount(null)
    setRepoPath('')
  }

  const statusInfo = STATUS_LABELS[status] ?? STATUS_LABELS.idle
  const isRunning  = status === 'queued' || status === 'processing'
  const isDone     = status === 'completed' || status === 'failed'

  return (
    <main className="page">
      <div className="card">
        {/* ── Header ── */}
        <div className="header">
          <div className="logo">⚡</div>
          <h1>CCKB</h1>
          <p className="subtitle">Codebase Context & Knowledge Base</p>
        </div>

        {/* ── Input form (idle only) ── */}
        {status === 'idle' && (
          <form onSubmit={handleSubmit} className="form" id="scan-form">
            <label htmlFor="repo-path-input">Repository Path</label>
            <input
              id="repo-path-input"
              type="text"
              value={repoPath}
              onChange={(e) => setRepoPath(e.target.value)}
              placeholder="/absolute/path/to/your/repo"
              required
              autoComplete="off"
            />
            {error && <p className="error-msg">{error}</p>}
            <button type="submit" className="btn-primary" id="start-scan-btn">
              Onboard Repository
            </button>
          </form>
        )}

        {/* ── Progress view (queued / processing) ── */}
        {isRunning && (
          <div className="progress-view" id="progress-view">
            <div className="status-badge" style={{ '--dot-color': statusInfo.color }}>
              <span className="dot" />
              {statusInfo.label}
            </div>
            <div className="progress-bar-track">
              <div
                className="progress-bar-fill"
                style={{ width: `${progress}%` }}
                id="progress-bar"
              />
            </div>
            <p className="progress-pct">{progress}%</p>
            {message && <p className="progress-msg">{message}</p>}
            {jobId && (
              <p className="job-id-label">
                Job <code>{jobId.slice(0, 8)}…</code>
              </p>
            )}
          </div>
        )}

        {/* ── Completion view ── */}
        {status === 'completed' && (
          <div className="result-view success" id="result-view">
            <div className="result-icon">✓</div>
            <h2>Onboarding Complete</h2>
            {specCount !== null && (
              <p>{specCount} feature spec{specCount !== 1 ? 's' : ''} indexed into MinIO</p>
            )}
            {message && <p className="result-msg">{message}</p>}
            <button className="btn-secondary" onClick={reset} id="scan-again-btn">
              Scan Another Repo
            </button>
          </div>
        )}

        {/* ── Failure view ── */}
        {status === 'failed' && (
          <div className="result-view failure" id="failure-view">
            <div className="result-icon">✕</div>
            <h2>Scan Failed</h2>
            {message && <p className="result-msg">{message}</p>}
            <button className="btn-secondary" onClick={reset} id="retry-btn">
              Try Again
            </button>
          </div>
        )}
      </div>
    </main>
  )
}
