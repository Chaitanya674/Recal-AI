import React, { useEffect, useState } from 'react'
import { api } from '../lib/stream.js'

/** The reel IS the rendered MP4 — the exact file you download, played inline.
 *  One renderer (backend/features/render.py), so the app and the download can
 *  never drift apart. The backend prerenders the MP4 the moment a reel is
 *  created, so this is usually ready by the time the card appears. */
export default function ReelPlayer({ generationId }) {
  const [mp4, setMp4] = useState('')
  const [err, setErr] = useState('')
  const [tries, setTries] = useState(0)
  useEffect(() => {
    if (!generationId) return
    let alive = true
    setErr('')
    api(`/api/reels/${generationId}/render`, { format: 'mp4' })
      .then(r => {
        if (!alive) return
        if (r.ok) setMp4(r.url + '?inline=1')
        else setErr(r.detail || r.reason || 'render failed')
      })
      .catch(e => { if (alive) setErr(e.message) })
    return () => { alive = false }
  }, [generationId, tries])
  return (
    <div className="player mp4">
      {mp4
        ? <video src={mp4} controls playsInline preload="metadata" />
        : <div className="stage-empty"><div>
            {err ? <>
              <b>Render failed</b>
              <p>{err}</p>
              <button onClick={() => setTries(t => t + 1)}>Try again</button>
            </> : <>
              <b>Rendering video…</b>
              <p>Drawing the motion-graphic frames and encoding them with the narration track.</p>
            </>}
          </div></div>}
    </div>)
}

/** Download buttons: the backend renders the reel to MP4 or PDF slides (cached after the
 *  first render), then the browser downloads it. */
export function ReelDownloads({ generationId }) {
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const get = (format) => {
    setBusy(format); setErr('')
    api(`/api/reels/${generationId}/render`, { format })
      .then(r => {
        if (!r.ok) throw new Error(r.detail || r.reason || 'render failed')
        const a = document.createElement('a')
        a.href = r.url; a.download = ''
        document.body.appendChild(a); a.click(); a.remove()
      })
      .catch(e => setErr(e.message))
      .finally(() => setBusy(''))
  }
  return (<>
    <button disabled={!!busy} onClick={() => get('mp4')}
            title="MP4 with motion graphics and narration">{busy === 'mp4' ? 'Rendering video…' : '⬇ Video'}</button>
    <button disabled={!!busy} onClick={() => get('pdf')}
            title="PDF, one slide per scene">{busy === 'pdf' ? 'Building slides…' : '⬇ Slides'}</button>
    {err && <span className="err-inline">{err}</span>}
  </>)
}
