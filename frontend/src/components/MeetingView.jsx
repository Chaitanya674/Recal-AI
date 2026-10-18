import React, { useEffect, useRef, useState } from 'react'
import Markdown from './Markdown.jsx'
import { api } from '../lib/stream.js'

/** Live mock client meeting. The browser is the ears and the voice:
 *  SpeechRecognition transcribes the learner continuously; the AI client's
 *  replies are spoken via speechSynthesis — no model audio quota involved.
 *  No SpeechRecognition support (or mic denied) degrades to typed turns. */

const SR = typeof window !== 'undefined' &&
  (window.SpeechRecognition || window.webkitSpeechRecognition)

export default function MeetingView({ doc, topic: topicIn, onExit, log }) {
  const [turns, setTurns] = useState([])        // {who: you|client, text}
  const [live, setLive] = useState(false)       // mic listening
  const [busy, setBusy] = useState(false)
  const [heard, setHeard] = useState('')        // interim transcript
  const [typed, setTyped] = useState('')
  const [summary, setSummary] = useState('')
  const [topic, setTopic] = useState(topicIn || doc?.name || '')
  const [started, setStarted] = useState(false)
  const [openPts, setOpenPts] = useState(null)  // client's unresolved reading-note points
  const rec = useRef(null)
  const endRef = useRef(null)

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [turns, summary])
  useEffect(() => () => { stopMic(); speechSynthesis.cancel() }, [])

  const speak = (text) => {
    speechSynthesis.cancel()
    const u = new SpeechSynthesisUtterance(text)
    u.rate = 1.04
    speechSynthesis.speak(u)
  }

  const begin = () => {
    setBusy(true)
    api('/api/meeting/start', { topic, doc_id: doc?.id || '' })
      .then(r => {
        setStarted(true)
        setTopic(r.topic)
        if (r.open_points) setOpenPts(r.open_points)
        setTurns([{ who: 'client', text: r.say }])
        speak(r.say)
      })
      .catch(() => log('Could not start the meeting — is the backend running?', true))
      .finally(() => setBusy(false))
  }

  const send = (said) => {
    const text = (said || '').trim()
    if (!text || busy) return
    speechSynthesis.cancel()                     // you spoke: the client stops talking
    setTurns(t => [...t, { who: 'you', text }])
    setBusy(true)
    api('/api/meeting/turn', { said: text })
      .then(r => {
        if (typeof r.remaining === 'number' && openPts != null) setOpenPts(r.remaining)
        setTurns(t => [...t, { who: 'client', text: r.say }])
        speak(r.say)
      })
      .catch(() => log('Meeting turn failed.', true))
      .finally(() => setBusy(false))
  }

  const stopMic = () => { try { rec.current?.stop() } catch { /* already stopped */ } setLive(false) }

  const startMic = () => {
    if (!SR) return log('This browser has no speech recognition — type your replies below.', true)
    const r = new SR()
    r.lang = 'en-US'
    r.continuous = true
    r.interimResults = true
    let finalBuf = ''
    let quietTimer = null
    r.onresult = (e) => {
      let interim = ''
      for (let k = e.resultIndex; k < e.results.length; k++) {
        const t = e.results[k][0].transcript
        if (e.results[k].isFinal) finalBuf += t + ' '
        else interim += t
      }
      setHeard(interim || finalBuf)
      clearTimeout(quietTimer)                   // a 1.4s pause ends your turn
      quietTimer = setTimeout(() => {
        if (finalBuf.trim()) { send(finalBuf); finalBuf = ''; setHeard('') }
      }, 1400)
    }
    r.onerror = (e) => { if (e.error === 'not-allowed') log('Microphone blocked — allow mic access, or type below.', true); stopMic() }
    r.onend = () => setLive(false)
    rec.current = r
    r.start()
    setLive(true)
  }

  const end = () => {
    stopMic(); speechSynthesis.cancel()
    setBusy(true)
    api('/api/meeting/end', {})
      .then(r => { setSummary(r.summary); speak('Thanks — that was useful. I sent you my notes.') })
      .catch(() => log('Could not end the meeting.', true))
      .finally(() => setBusy(false))
  }

  return (
    <div className="meeting">
      <div className="reh-head">
        <button className="btn" onClick={() => { stopMic(); speechSynthesis.cancel(); onExit() }}>‹ Back</button>
        <b>Mock client meeting</b>
        {started && openPts != null && !summary &&
          <span className="pill" title="Points from the client's reading notes you haven't cleared yet">
            {openPts === 0 ? 'all points cleared ✓' : `client unclear on ${openPts}`}</span>}
        <span className="spacer" />
        {started && !summary &&
          <button className="btn" disabled={busy} onClick={end}>End meeting & debrief</button>}
      </div>

      {!started ? (
        <div className="meet-setup">
          <p>A client joins the call about your topic. They listen, push back, and ask the
            questions a real stakeholder would — until the gaps show. Speak naturally; a
            pause hands them the floor.</p>
          <input value={topic} placeholder="Meeting topic (e.g. BM25 rollout plan)"
                 onChange={e => setTopic(e.target.value)} />
          <button className="btn primary" disabled={busy || !topic.trim()} onClick={begin}>
            {busy ? 'Joining…' : '📞 Start the meeting'}</button>
          {doc && <p className="hint">Grounded in “{doc.name}” — the client has read it.</p>}
        </div>
      ) : (
        <>
          <div className="meet-log">
            {turns.map((t, i) =>
              <div key={i} className={'mt ' + t.who}>
                <span className="who">{t.who === 'you' ? 'You' : 'Client'}</span>
                <p>{t.text}</p>
              </div>)}
            {heard && <div className="mt you ghosted"><span className="who">You</span><p>{heard}…</p></div>}
            {busy && <div className="hint">client is thinking…</div>}
            {summary && <div className="meet-summary"><Markdown>{summary}</Markdown></div>}
            <div ref={endRef} />
          </div>
          {!summary && <div className="meet-controls">
            <button className={'btn rec' + (live ? ' on' : '')} disabled={busy}
                    onClick={live ? stopMic : startMic}>
              {live ? '■ Mute mic' : '🎙 Speak'}</button>
            <input value={typed} disabled={busy} placeholder="…or type your reply"
                   onChange={e => setTyped(e.target.value)}
                   onKeyDown={e => { if (e.key === 'Enter') { send(typed); setTyped('') } }} />
            <button className="btn" disabled={busy || !typed.trim()}
                    onClick={() => { send(typed); setTyped('') }}>Send</button>
          </div>}
        </>
      )}
    </div>)
}
