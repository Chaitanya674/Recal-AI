import React, { useEffect, useState } from 'react'
import MaterialSlides from './MaterialSlides.jsx'
import ReelPlayer, { ReelDownloads } from './ReelPlayer.jsx'
import { api } from '../lib/stream.js'

const fmt = (iso) => new Date(iso).toLocaleString([], {
  weekday: 'short', hour: '2-digit', minute: '2-digit' })

function Shell({ card, children, foot }) {
  const r = card.receipt || {}
  return (
    <div className="card">
      <div className="chead">
        <span className="kind">{card.type}</span><b>{card.title}</b>
        <span className="r">{r.model} · {r.verified ? 'verified ✓' : 'unverified'}
          {r.escalated ? ' · escalated' : ''}</span>
      </div>
      <div className="cbody">{children}</div>
      {foot && <div className="cfoot">{foot}</div>}
    </div>)
}

/** 👍 / 👎 teach Recal a style; the learned approach is confirmed inline. */
export function Feedback({ generationId }) {
  const [state, setState] = useState(null)   // null | {signal, busy} | {signal, text, err}
  useEffect(() => setState(null), [generationId])
  const send = (signal) => {
    setState({ signal, busy: true })
    api('/api/feedback', { generation_id: generationId, signal })
      .then(r => setState({ signal, text: r.style_descriptor
        ? (signal === 'up' ? 'Learned: ' : 'Will avoid: ') + r.style_descriptor
        : 'Thanks, noted.' }))
      .catch(() => setState({ signal, err: true, text: 'Couldn’t save that. Try again.' }))
  }
  if (state && !state.busy && !state.err)
    return <span className="fb-note" title={state.text}>{state.signal === 'up' ? '👍' : '👎'} {state.text}</span>
  return (<>
    {state?.err && <span className="fb-note err">{state.text}</span>}
    <button disabled={!!state?.busy} onClick={() => send('up')}>
      {state?.busy && state.signal === 'up' ? 'Learning…' : '👍 This clicked'}</button>
    <button disabled={!!state?.busy} onClick={() => send('down')}>
      {state?.busy && state.signal === 'down' ? 'Noting…' : '👎 Not for me'}</button>
  </>)
}

function ReelCard({ card, act }) {
  const p = card.payload
  return (
    <Shell card={card} foot={<>
      <ReelDownloads generationId={p.generation_id} />
      <Feedback generationId={p.generation_id} />
      <button onClick={() => act({ type: 'export', card_id: card.id, dest: 'gdocs' })}>
        Export → Docs/NotebookLM</button>
    </>}>
      <ReelPlayer generationId={p.generation_id} />
    </Shell>)
}

function PageCard({ card, act }) {
  const p = card.payload
  return (
    <Shell card={card} foot={<>
      <Feedback generationId={p.generation_id} />
      <button onClick={() => act({ type: 'export', card_id: card.id, dest: 'gdocs' })}>
        Export → Docs</button>
    </>}>
      <div style={{ fontSize: '.88rem' }}><MaterialSlides payload={p} /></div>
      {p.style_used?.length > 0 &&
        <div className="status">style: {p.style_used.join(' · ')}</div>}
    </Shell>)
}

function PlanCard({ card, act }) {
  const p = card.payload
  return (
    <Shell card={card}>
      <div style={{ fontSize: '.86rem', marginBottom: '.5rem' }}>{p.summary}</div>
      <ul className="plan">{(p.sessions || []).map(s =>
        <li key={s.event_id}>
          <span className="when">{fmt(s.start)} · {s.minutes}m</span>
          <span>{s.title}</span><small>{s.why}</small>
          <button style={{ fontSize: '.7rem' }} onClick={() =>
            act({ type: 'mark_status', event_id: s.event_id, status: 'done' })}>done</button>
        </li>)}
      </ul>
    </Shell>)
}

function SourceCard({ card }) {
  const p = card.payload
  return (
    <Shell card={card}>
      <div className="status" style={{ wordBreak: 'break-all' }}>{p.url}</div>
      <div className="stats" style={{ marginTop: '.5rem' }}>
        <div><b>{p.chunks}</b>chunks</div>
        <div><b>{p.concepts}</b>concepts</div>
        <div><b>{p.linked}</b>linked</div>
      </div>
    </Shell>)
}

function DiffCard({ card }) {
  const p = card.payload
  return (
    <Shell card={card}>
      <div style={{ fontSize: '.88rem', marginBottom: '.4rem' }}>{p.narrative}</div>
      <ul className="plan">{(p.moves || []).map((m, i) =>
        <li key={i}><span className="when">{m.action}</span>
          <span>{m.reason || m.event_id}</span></li>)}
      </ul>
    </Shell>)
}

const SEV = { high: 'Fix before sending', medium: 'Worth fixing', low: 'Polish' }

function DocReviewCard({ card, act }) {
  const p = card.payload
  return (
    <Shell card={card} foot={
      <button onClick={() => act({ type: 'export', card_id: card.id, dest: 'gdocs' })}>
        Export → Docs</button>}>
      <div className="rev-head">{p.headline}</div>
      {p.key_message && <div className="status">key message: {p.key_message}</div>}
      {['high', 'medium', 'low'].map(sev => {
        const issues = (p.issues || []).filter(i => i.severity === sev)
        return issues.length > 0 && (
          <div key={sev} className="rev-group">
            <span className={'sev ' + sev}>{SEV[sev]}</span>
            <ul>{issues.map((i, k) =>
              <li key={k}><b>{i.where}:</b> {i.issue}
                <div className="fix">→ {i.fix}</div></li>)}
            </ul>
          </div>)
      })}
      {(p.issues || []).length === 0 &&
        <div className="status">No issues found — ready to send.</div>}
    </Shell>)
}

export function renderCard(card, act) {
  const C = { reel: ReelCard, page: PageCard, plan: PlanCard,
              source: SourceCard, calendar_diff: DiffCard, doc_review: DocReviewCard }[card.type]
  return C ? <C key={card.id} card={card} act={act} /> : null
}
