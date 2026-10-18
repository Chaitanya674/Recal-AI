import React, { useEffect, useRef, useState } from 'react'
import MeetingView from './MeetingView.jsx'
import { api } from '../lib/stream.js'

/** Practice tab: upload a deck/doc/sheet, run the two reviews, and rehearse it
 *  section by section with the mic. The coach replies with feedback + one
 *  client-style cross-question (spoken aloud via the browser's speech synthesis). */

const REASONS = {
  unsupported_type: 'Only .pptx, .docx and .xlsx are supported.',
  file_too_large: 'File is over 15 MB.',
  unreadable_file: "Couldn't read that file — is it password-protected?",
  empty_document: 'That file has no readable content.',
  audio_unreadable: "Couldn't decode the recording — try again.",
}
const err = (r) => REASONS[r.reason] || r.reason || 'Something went wrong.'

function Recorder({ onTake, disabled }) {
  const [rec, setRec] = useState(null)         // active MediaRecorder
  const chunks = useRef([])
  const stop = () => rec && rec.state !== 'inactive' && rec.stop()
  const start = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const r = new MediaRecorder(stream)
      chunks.current = []
      r.ondataavailable = e => chunks.current.push(e.data)
      r.onstop = () => {
        stream.getTracks().forEach(t => t.stop())
        setRec(null)
        onTake(new Blob(chunks.current, { type: r.mimeType }))
      }
      r.start()
      setRec(r)
    } catch {
      onTake(null, 'Microphone blocked — allow mic access for this site, or use "Quiz me" below.')
    }
  }
  return (
    <button className={'btn rec' + (rec ? ' on' : '')} disabled={disabled}
            onClick={rec ? stop : start}>
      {rec ? '■ Stop — I said my piece' : '🎙 Speak this part'}</button>)
}

function Rehearse({ doc, onExit, log }) {
  const [sections, setSections] = useState(null)
  const [i, setI] = useState(0)
  const [coach, setCoach] = useState(null)     // {transcript, feedback, question}
  const [busy, setBusy] = useState(false)
  const [voiceOn, setVoiceOn] = useState(true)
  const voiceRef = useRef(voiceOn)
  voiceRef.current = voiceOn

  useEffect(() => {
    api(`/api/docs/${doc.id}`).then(r => r.ok ? setSections(r.doc.sections) : log(err(r), true))
    return () => { speechSynthesis.cancel(); api(`/api/docs/${doc.id}/coach`, null, 'DELETE') }
  }, [])

  const ask = (blob) => {
    setBusy(true); setCoach(null)
    const fd = new FormData()
    fd.append('section', String(i + 1))
    if (blob) fd.append('audio', blob, 'take.webm')
    fetch(`/api/docs/${doc.id}/coach`, { method: 'POST', body: fd })
      .then(r => r.json())
      .then(r => {
        if (!r.ok) return log(err(r), true)
        setCoach(r)
        if (voiceRef.current && r.question) {
          const u = new SpeechSynthesisUtterance(r.question)
          speechSynthesis.cancel(); speechSynthesis.speak(u)
        }
      })
      .catch(() => log('Coach request failed — is the backend running?', true))
      .finally(() => setBusy(false))
  }

  if (!sections) return <div className="empty-note">Loading {doc.name}…</div>
  const s = sections[i]
  const move = (d) => { setI(n => Math.max(0, Math.min(sections.length - 1, n + d))); setCoach(null); speechSynthesis.cancel() }
  return (
    <div className="rehearse">
      <div className="reh-head">
        <button className="btn" onClick={onExit}>‹ All documents</button>
        <b>{doc.name}</b>
        <span className="spacer" />
        <label className="pill"><input type="checkbox" checked={voiceOn}
               onChange={e => setVoiceOn(e.target.checked)} /> coach voice</label>
        <button className="ghost" disabled={i === 0} onClick={() => move(-1)}>‹</button>
        <span>{s.kind} {i + 1} / {sections.length}</span>
        <button className="ghost" disabled={i === sections.length - 1} onClick={() => move(1)}>›</button>
      </div>

      <div className="reh-slide">
        <h3>{s.title || `(untitled ${s.kind})`}</h3>
        {s.headers?.length > 0 &&
          <div className="reh-cols">columns: {s.headers.map((h, k) =>
            <code key={k}>{h || '(blank)'}</code>)}</div>}
        <ul>{(s.lines || []).slice(0, 14).map((ln, k) => <li key={k}>{ln}</li>)}</ul>
        {s.notes && <div className="reh-notes">notes: {s.notes.slice(0, 300)}</div>}
      </div>

      <div className="reh-actions">
        <Recorder disabled={busy} onTake={(blob, e) => e ? log(e, true) : ask(blob)} />
        <button className="btn" disabled={busy} onClick={() => ask(null)}>Quiz me (no mic)</button>
        {busy && <span className="hint">coach is listening…</span>}
      </div>

      {coach && <div className="coach">
        {coach.transcript && <p className="hint">You said: “{coach.transcript}”</p>}
        {coach.feedback && <p>{coach.feedback}</p>}
        <p className="q">❝ {coach.question} ❞</p>
      </div>}
    </div>)
}

export default function PracticeView({ onCard, log }) {
  const [docs, setDocs] = useState([])
  const [busyId, setBusyId] = useState('')     // doc id being reviewed, or 'upload'
  const [active, setActive] = useState(null)   // doc being rehearsed
  const [meeting, setMeeting] = useState(null) // {doc?} -> live mock client meeting
  const fileRef = useRef(null)

  const refresh = () => api('/api/docs').then(r => r.ok && setDocs(r.docs)).catch(() => {})
  useEffect(() => { refresh() }, [])

  const upload = (file) => {
    if (!file) return
    setBusyId('upload')
    const fd = new FormData()
    fd.append('file', file)
    fetch('/api/docs', { method: 'POST', body: fd }).then(r => r.json())
      .then(r => r.ok ? (refresh(), log(`Uploaded ${r.doc.name}: ${r.doc.sections} ${r.doc.kind === 'xlsx' ? 'sheets' : 'parts'}.`))
                      : log(err(r), true))
      .catch(() => log('Upload failed — is the backend running?', true))
      .finally(() => { setBusyId(''); if (fileRef.current) fileRef.current.value = '' })
  }

  const review = (doc, mode) => {
    setBusyId(doc.id)
    api(`/api/docs/${doc.id}/review`, { mode })
      .then(r => r.ok ? onCard(r.card) : log(err(r), true))
      .catch(() => log('Review failed — is the backend running?', true))
      .finally(() => setBusyId(''))
  }

  if (meeting) return <MeetingView doc={meeting.doc} onExit={() => setMeeting(null)} log={log} />
  if (active) return <Rehearse doc={active} onExit={() => setActive(null)} log={log} />
  return (
    <div className="practice">
      <div className="week-note"><b>Practice & polish</b>
        Upload a deck, document or spreadsheet. Recal checks whether it lands its message,
        flags what a client would trip on, and rehearses it with you — it listens and asks
        the questions a client would.</div>
      <div className="up-row">
        <button className="btn primary" onClick={() => setMeeting({ doc: null })}>
          📞 Mock client meeting (live, voice)</button>
      </div>
      <div className="up-row">
        <input ref={fileRef} type="file" accept=".pptx,.docx,.xlsx,.xlsm" hidden
               onChange={e => upload(e.target.files[0])} />
        <button className="btn primary" disabled={busyId === 'upload'}
                onClick={() => fileRef.current?.click()}>
          {busyId === 'upload' ? 'Reading…' : '⬆ Upload .pptx / .docx / .xlsx'}</button>
      </div>
      {docs.length === 0
        ? <div className="empty-note">Nothing uploaded yet.</div>
        : <ul className="doc-list">{docs.map(d =>
            <li key={d.id}>
              <span className="kind">{d.kind}</span>
              <b>{d.name}</b>
              <span className="hint">{d.sections} {d.kind === 'xlsx' ? 'sheets' : 'parts'}</span>
              <span className="spacer" />
              <button className="btn" disabled={!!busyId} onClick={() => review(d, 'message')}>
                {busyId === d.id ? '…' : 'Message check'}</button>
              <button className="btn" disabled={!!busyId} onClick={() => review(d, 'client_ready')}>
                Client-ready check</button>
              <button className="btn primary" disabled={!!busyId} onClick={() => setActive(d)}>
                🎙 Rehearse</button>
              <button className="btn" disabled={!!busyId} onClick={() => setMeeting({ doc: d })}>
                📞 Meeting</button>
            </li>)}
          </ul>}
    </div>)
}
