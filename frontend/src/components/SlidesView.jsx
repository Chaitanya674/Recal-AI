import React from 'react'
import ReelPlayer, { ReelDownloads } from './ReelPlayer.jsx'
import MaterialSlides from './MaterialSlides.jsx'
import { Feedback } from './cards.jsx'

/** The Canvas: every reel and study page lands here. ▲▼ walks back through
 *  previously generated content; clicking the content jumps the chat to the
 *  query that produced it. */
export default function SlidesView({ cards, index, onIndex, act, onJump }) {
  const items = cards.filter(c => c.type === 'reel' || c.type === 'page')
  if (!items.length) return (
    <div className="slides">
      <div className="player"><div className="stage-empty"><div>
        <b>Nothing on the canvas yet</b>
        <p>Ask for a one-minute visual explainer or study material and it lands here —
          videos play, material pages become slides you can step through.</p>
      </div></div></div>
      <div className="caption-row">no content yet</div>
    </div>)

  const idx = index < 0 ? items.length - 1 : Math.min(index, items.length - 1)
  const item = items[idx]
  const r = item.receipt || {}, p = item.payload
  return (
    <div className="slides">
      <div onClick={() => onJump?.(item.id)}
           title="Click to jump to the chat message that created this">
        {item.type === 'reel'
          ? <ReelPlayer key={item.id} generationId={p.generation_id} />
          : <MaterialSlides key={item.id} payload={p} />}
      </div>
      <div className="caption-row">
        {items.length > 1 && <>
          <button className="ghost" disabled={idx === 0} title="Older"
                  onClick={() => onIndex(idx - 1)}>▲</button>
          <span>{idx + 1}/{items.length}</span>
          <button className="ghost" disabled={idx === items.length - 1} title="Newer"
                  onClick={() => onIndex(idx + 1)}>▼</button>
        </>}
        <span title={r.reason || ''}>{item.title} · {r.model} · {r.verified ? 'verified ✓' : 'unverified'}
          {r.reason?.startsWith('extractive') ? ' (fallback)' : ''}
          {item.type === 'reel' ? ` · ${p.audio ? '🔊 narrated' : 'captions'}` : ''}</span>
        <span className="spacer" />
        <span className="actions">
          {item.type === 'reel' && <ReelDownloads generationId={p.generation_id} />}
          <Feedback generationId={p.generation_id} />
          <button onClick={() => act({ type: 'export', card_id: item.id, dest: 'gdocs' })}>Export → Docs</button>
        </span>
      </div>
    </div>)
}
