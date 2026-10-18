import React, { useEffect, useId, useState } from 'react'

/** Renders a ```mermaid block (flowchart, sequence, state, timeline, pie…) as SVG.
 *  Mermaid is ~1 MB, so it is imported on the first diagram only. securityLevel
 *  'strict' makes mermaid sanitize labels and disable click handlers, which is what
 *  makes injecting its SVG safe for model-written diagrams. Diagram code the
 *  parser rejects falls back to the source in a code block. */
let ready = null
function loadMermaid() {
  ready ||= import('mermaid').then(({ default: mermaid }) => {
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'dark',
                         fontFamily: 'inherit' })
    return mermaid
  })
  return ready
}

export default function Mermaid({ chart }) {
  const id = 'mmd' + useId().replace(/[^a-zA-Z0-9]/g, '')
  const [svg, setSvg] = useState('')
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let live = true
    setSvg(''); setFailed(false)
    loadMermaid()
      .then(m => m.render(id, chart.trim()))
      .then(({ svg }) => live && setSvg(svg))
      .catch(() => {
        document.getElementById('d' + id)?.remove()   // mermaid leaves its error SVG behind
        if (live) setFailed(true)
      })
    return () => { live = false }
  }, [chart, id])
  if (failed) return (
    <div className="mermaid-fail">
      <pre><code>{chart}</code></pre>
      <small>This diagram couldn't be drawn — showing its source instead.</small>
    </div>)
  if (!svg) return <div className="mermaid-box loading">Drawing diagram…</div>
  return <div className="mermaid-box" dangerouslySetInnerHTML={{ __html: svg }} />
}
