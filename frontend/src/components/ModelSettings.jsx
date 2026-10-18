import React, { useEffect, useState } from 'react'
import { api } from '../lib/stream.js'

/** Read-only model panel: every call runs on Gemini via Vertex AI with the server's
 *  Google Cloud credentials, so there is no key to enter here — models are set in the
 *  server's .env (MODEL_LITE / MODEL_FAST / MODEL_DEEP). */

const TIERS = [
  ['model_lite', 'routing (lite, temperature 0)'],
  ['model_fast', 'fast'],
  ['model_deep', 'deep (Pro)'],
]

export default function ModelSettings({ onDone, onMock }) {
  const [status, setStatus] = useState(null)   // {mock, source, project, location, model_*}
  const [err, setErr] = useState(null)

  useEffect(() => {
    api('/api/settings/llm')
      .then(r => { setStatus(r); onMock(r.mock) })
      .catch(() => setErr('Backend not reachable.'))
  }, [])

  return (
    <div className="modal-back" onClick={e => e.target === e.currentTarget && onDone()}>
      <div className="modal" role="dialog" aria-label="Models">
        <h3>Models</h3>
        {err ? <p className="err">{err}</p> : !status ? <p className="hint">Checking…</p> : <>
          <p className="hint">
            {status.source === 'vertex'
              ? <>Gemini on Vertex AI · project <b>{status.project}</b> · {status.location}. Model calls are <b>live</b>.</>
              : <>Vertex AI is not configured on the server — Recal is in <b>replay mode</b> (canned
                 answers). Set GOOGLE_CLOUD_PROJECT in the server's .env and restart.</>}
          </p>
          <div className="two-col">
            {TIERS.map(([k, label]) =>
              <label key={k}>{label}
                <input className="field" value={status[k] || ''} readOnly /></label>)}
          </div>
          <div className="modal-foot">
            <span className="spacer" />
            <button type="button" className="btn" onClick={onDone}>Close</button>
          </div>
        </>}
      </div>
    </div>)
}
