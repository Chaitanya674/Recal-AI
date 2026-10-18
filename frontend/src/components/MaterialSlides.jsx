import React, { useEffect, useState } from 'react'
import Markdown from './Markdown.jsx'
import { api } from '../lib/stream.js'

/** Study material as a book: one page per section (concept / code steps /
 *  do-it-yourself exercise), navigated left/right. Sections come from the
 *  multi-call drafter; a plain markdown page degrades to a single page.
 *  Every page turn reports progress, so the system knows how much of the
 *  session's material is read — and can offer to schedule the rest. */
export default function MaterialSlides({ payload }) {
  const sections = payload.sections?.length ? payload.sections
    : [{ title: payload.title || 'Notes', kind: 'concept', markdown: payload.markdown }]
  const [i, setI] = useState(0)
  const [done, setDone] = useState(0)
  const idx = Math.min(i, sections.length - 1)
  const cur = sections[idx]
  useEffect(() => {
    const page = idx + 1
    setDone(d => Math.max(d, page))
    api('/api/material/progress',
        { topic: payload.title || 'Notes', page, total: sections.length })
      .catch(() => {})                     // progress is best-effort, never blocking
  }, [idx, payload.title, sections.length])
  const left = sections.length - Math.max(done, idx + 1)
  const cites = payload.citations || []
  const onPage = cites.map((c, n) => ({ ...c, n: n + 1 }))
    .filter(c => (cur.markdown || '').includes(`[${c.chunk_id}]`))
  return (
    <div className="mslides">
      <div className="mslide">
        <Markdown citations={cites}>{cur.markdown}</Markdown>
        {onPage.length > 0 && <ol className="msources">{onPage.map(c =>
          <li key={c.n} value={c.n}>{c.url
            ? <a href={c.url} target="_blank" rel="noreferrer">{c.title || c.url}</a>
            : c.title || 'Captured source'}</li>)}
        </ol>}
      </div>
      <div className="mnav">
        <button className="ghost" disabled={idx === 0}
                onClick={e => { e.stopPropagation(); setI(idx - 1) }}>‹</button>
        <span>{idx + 1}/{sections.length} · {cur.kind}
          {left > 0 ? ` · ${left} page${left > 1 ? 's' : ''} left` : ' · ✓ complete'}</span>
        <button className="ghost" disabled={idx === sections.length - 1}
                onClick={e => { e.stopPropagation(); setI(idx + 1) }}>›</button>
      </div>
    </div>)
}
