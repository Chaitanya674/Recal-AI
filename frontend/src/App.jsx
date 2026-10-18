import React, { useEffect, useRef, useState } from 'react'
import { streamChat, api } from './lib/stream.js'
import Markdown from './components/Markdown.jsx'
import { renderCard } from './components/cards.jsx'
import SlidesView from './components/SlidesView.jsx'
import WeekView from './components/WeekView.jsx'
import PracticeView from './components/PracticeView.jsx'
import ModelSettings from './components/ModelSettings.jsx'

const CHIPS = [
  { label: 'Ingest a doc', message: 'https://en.wikipedia.org/wiki/Okapi_BM25' },
  { label: 'Explain visually', message: 'Show BM25 vs embeddings as a 1 min visual' },
  { label: 'Plan 4 hours', message: 'Plan 4 hours this week to learn LangGraph' },
  { label: 'Urgent meeting', action: { type: 'urgent', title: 'Urgent client meeting', minutes: 150 } },
  { label: 'Write me notes', message: 'Write me notes on hybrid retrieval' },
  { label: 'Ask a doubt', message: 'How does hybrid retrieval combine BM25 and embeddings?' },
  { label: "I'm tired", action: { type: 'energy', level: 'tired' } },
]

// Google pills: the backend hands back a consent URL and Google redirects to
// /?connected=<key> or /?connect_error=<reason>&provider=<key> when done.
const CONNS = [
  { key: 'calendar', pill: 'Calendar', label: 'Google Calendar',
    connect: () => api('/api/connect/calendar?return_to=' + encodeURIComponent(location.origin)) },
  { key: 'gdocs', pill: 'Google Docs', label: 'Google Docs',
    connect: () => api('/api/connect/notebook', { provider: 'gdocs', return_to: location.origin }) },
]
const LABEL = Object.fromEntries(CONNS.map(c => [c.key, c.label]))

const linkify = (s) => s.split(/(https?:\/\/\S+)/g).map((part, i) =>
  i % 2 ? <a key={i} href={part} target="_blank" rel="noreferrer">{part}</a> : part)

/** Read (and strip) the OAuth result flags Google's redirect left in the URL. */
function oauthNotice() {
  const q = new URLSearchParams(location.search)
  const ok = q.get('connected'), err = q.get('connect_error')
  if (!ok && !err) return null
  history.replaceState(null, '', location.pathname)
  if (ok) return { content: `${LABEL[ok] || ok} connected` +
    (ok === 'calendar' ? ': your events are on the Week tab.' : ': use Export on any card.'), tab: ok === 'calendar' && 'week' }
  const name = LABEL[q.get('provider')] || 'Google'
  return { content: `${name} connect failed: ${OAUTH_HELP[err]?.(q.get('provider')) || err}`, err: true }
}

// Plain-language fixes for the errors people actually hit on Google's screens
const OAUTH_HELP = {
  scope_not_granted: (p) => `on Google's permission screen, tick the box for ${p === 'gdocs'
    ? '"See, edit, create, and delete only the specific Google Drive files you use with this app"'
    : '"See and download any calendar you can access"'} (or "Select all"), then click the pill again.`,
  access_denied: () => 'sign-in was cancelled, or this Google account is not allowed to use the app yet ' +
    '(it must be listed as a test user while the app is in Testing). Click the pill to try again.',
  invalid_grant: () => 'the sign-in code expired. Click the pill to try again.',
  invalid_client: () => 'the OAuth client ID or secret in .env is wrong. Fix it and restart the backend.',
}

const TABS = [['slides', 'Canvas'], ['week', 'Week'], ['cards', 'Cards'], ['practice', 'Practice']]
const TAB_FOR = { reel: 'slides', page: 'slides', plan: 'week', calendar_diff: 'week' }

const getConv = () => (document.cookie.match(/(?:^|; )recal_conv=([^;]+)/) || [])[1] || 'main'

export default function App() {
  const [messages, setMessages] = useState([])   // {role: you|agent|status, content, err?}
  const [cards, setCards] = useState([])
  const [week, setWeek] = useState([])
  const [graph, setGraph] = useState({})
  const [conns, setConns] = useState({})
  const [mock, setMock] = useState(false)
  const [user, setUser] = useState(null)
  const [busy, setBusy] = useState(false)
  const [input, setInput] = useState('')
  const [tab, setTab] = useState('slides')
  const [reelIdx, setReelIdx] = useState(-1)     // -1 = follow latest reel
  const [keysOpen, setKeysOpen] = useState(false)
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme || 'dark')
  const [sessions, setSessions] = useState([])
  const [conv, setConv] = useState(getConv())
  const [hl, setHl] = useState(-1)              // chat message highlighted after a card jump
  const endRef = useRef(null)
  const msgRefs = useRef({})                    // message index -> DOM node (for card jumps)
  const cardMsg = useRef({})                    // card id -> index of the query that made it
  const lastYou = useRef(-1)
  const notice = useRef(oauthNotice())          // shown after bootstrap restores history

  const toggleTheme = () => {
    const next = theme === 'dark' ? 'light' : 'dark'
    document.documentElement.dataset.theme = next
    localStorage.setItem('recal-theme', next)
    setTheme(next)
  }

  const log = (content, err) => setMessages(m => [...m, { role: 'status', content, err }])
  const refreshSessions = () => api('/api/conversations')
    .then(r => setSessions(r.conversations || [])).catch(() => {})

  const pickSession = (id) => {
    document.cookie = `recal_conv=${id}; path=/`
    setConv(id)
    setMessages([]); setCards([]); setReelIdx(-1)
    cardMsg.current = {}
    act({ type: 'bootstrap' })
  }
  const newSession = () => api('/api/conversations', {})
    .then(r => { pickSession(r.id); refreshSessions() })
    .catch(() => log('Could not start a new chat (backend unreachable?)', true))

  const jumpToQuery = (cardId) => {             // canvas card -> the chat query behind it
    const i = cardMsg.current[cardId]
    if (i == null || !msgRefs.current[i]) return
    msgRefs.current[i].scrollIntoView({ behavior: 'smooth', block: 'center' })
    setHl(i); setTimeout(() => setHl(-1), 1800)
  }

  // Transient model overload: retry the turn itself (the backend already retries each
  // model call). Only for plain messages — re-firing an action could duplicate its
  // side effects — and only when the failed turn produced no tokens or cards.
  const RETRYABLE = /503|UNAVAILABLE|high demand|overloaded/i
  const MAX_RETRIES = 2

  const run = (body, label, attempt = 0) => {
    if (busy && attempt === 0) return
    setBusy(true)
    const shown = attempt === 0 && (body.message || label)
    if (shown) setMessages(m => { lastYou.current = m.length; return [...m, { role: 'you', content: shown }] })
    let agentIdx = -1
    let failed = ''
    let gotContent = false
    let finished = false            // onDone fires for the SSE `done` event AND stream end
    streamChat(body, {
      onStatus: d => log(d.text),
      onToken: d => {
        gotContent = true
        setMessages(m => {
          const copy = [...m]
          if (agentIdx < 0 || copy[agentIdx]?.role !== 'agent') {
            copy.push({ role: 'agent', content: '' }); agentIdx = copy.length - 1
          }
          copy[agentIdx] = { ...copy[agentIdx], content: copy[agentIdx].content + d.delta }
          return copy
        })
      },
      onCard: c => {
        gotContent = true
        cardMsg.current[c.id] = lastYou.current
        setCards(cs => [...cs, c])
        setTab(TAB_FOR[c.type] || 'cards')
        if (c.type === 'reel' || c.type === 'page') setReelIdx(-1)
      },
      onReceipt: r => log(
        `${r.model} · ${r.thinking} thinking · ${r.verified ? 'verified ✓' : 'unverified'}` +
        `${r.escalated ? ' · escalated' : ''}`),
      onState: s => {
        if (s.week) setWeek(s.week)
        if (s.graph) setGraph(s.graph)
        if (s.connections) setConns(s.connections)
        if ('mock' in s) setMock(s.mock)
        if (s.history) {   // bootstrap restore (plan §4.6)
          const n = notice.current
          notice.current = null
          const msgs = s.history.messages.filter(m => m.content)
          const map = {}
          let youAt = -1                       // card id -> index of the query before it
          const rendered = msgs.map((m, i) => {
            if (m.role === 'user') youAt = i
            ;(m.card_ids || []).forEach(id => { map[id] = youAt >= 0 ? youAt : i })
            return { role: m.role === 'user' ? 'you' : 'agent', content: m.content }
          })
          cardMsg.current = map
          setMessages([...rendered,
            ...(n ? [{ role: 'status', content: n.content, err: n.err }] : [])])
          setCards(s.history.cards)
          if (n?.tab) setTab(n.tab)
        }
      },
      onError: e => { failed = e.message || 'unknown error' },
      onDone: () => {
        if (finished) return
        finished = true
        if (failed && body.message && !gotContent &&
            RETRYABLE.test(failed) && attempt < MAX_RETRIES) {
          const wait = 2 * (attempt + 1)
          log(`⚠ Model busy — retrying in ${wait}s (attempt ${attempt + 1} of ${MAX_RETRIES})…`, true)
          setTimeout(() => run(body, label, attempt + 1), wait * 1000)
          return                     // stay busy across the retry
        }
        if (failed) log('⚠ ' + failed, true)
        setBusy(false); refreshSessions()
      },
    }).catch(e => { log('⚠ ' + e.message, true); setBusy(false) })
  }

  const act = (action, label) => run({ action }, label)

  const reset = () => {
    if (!confirm('Clear the chat, all cards and the study sessions Recal planned? ' +
                 'Your Google Calendar is not touched.')) return
    setMessages([]); setCards([]); setReelIdx(-1)
    act({ type: 'reset' })
  }

  const connect = (c) => {
    if (conns[c.key]) {     // already connected: offer to disconnect
      if (!confirm(`Disconnect ${c.label}?`)) return
      return api(`/api/connect/${c.key}`, null, 'DELETE').then(r => {
        setConns(cs => ({ ...cs, [c.key]: false }))
        if (r.week) setWeek(r.week)
        log(`${c.label} disconnected.`)
      }).catch(() => log(`${c.label}: disconnect failed`, true))
    }
    return c.connect()
      .then(r => r.ok && r.auth_url ? location.assign(r.auth_url)
                                    : log(`${c.label}: ${r.todo || r.reason}`, true))
      .catch(() => log(`${c.label}: connect failed (is the backend running?)`, true))
  }

  useEffect(() => {
    api('/api/auth', { guest: true }).then(r => {
      setUser(r.user); act({ type: 'bootstrap' }); refreshSessions()
    }).catch(() => log('⚠ Backend not reachable. Start it from the repo root: ' +
                       'uvicorn backend.main:app --reload --port 8000, then reload this page.', true))
  }, [])
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])

  const send = (e) => {
    e.preventDefault()
    const v = input.trim()
    if (!v) return
    setInput('')
    run({ message: v })
  }

  const calCard = [...cards].reverse().find(c => c.type === 'plan' || c.type === 'calendar_diff')
  const note = calCard && { title: calCard.title, text: calCard.payload.summary || calCard.payload.narrative }

  return (
    <>
      <header className="topbar">
        <div className="brand">
          <span className="logo">R</span>
          <span className="name">RECAL</span>
          {mock && <button className="badge" onClick={() => setKeysOpen(true)}
                           title="Canned model replies — Vertex AI is not configured on the server">REPLAY MODE</button>}
        </div>
        <div className="top-actions">
          {CONNS.map(c =>
            <button key={c.key} className={'pill' + (conns[c.key] ? ' on' : '')}
                    title={conns[c.key] ? `${c.label} connected` : `Connect ${c.label}`}
                    onClick={() => connect(c)}>
              <span className="dot" />{c.pill}</button>)}
          <button className="btn" disabled={busy} onClick={reset}>Reset</button>
          <button className="pill" title="Gemini models (Vertex AI)" onClick={() => setKeysOpen(true)}>⚙ Models</button>
          <button className="pill" onClick={toggleTheme}
                  aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}>
            {theme === 'dark' ? '☀' : '☾'}</button>
          <span className="who">{user ? (user.guest ? 'guest session' : user.name) : ''}</span>
        </div>
      </header>

      <main className="layout three">
        <aside className="sessions">
          <button className="newchat" disabled={busy} onClick={newSession}>+ New chat</button>
          <div className="slist">
            {sessions.map(s =>
              <button key={s.id} className={'sess' + (s.id === conv ? ' on' : '')}
                      title={s.title} disabled={busy}
                      onClick={() => s.id !== conv && pickSession(s.id)}>{s.title}</button>)}
          </div>
        </aside>
        <section className="chat">
          <div className="chat-head">
            <h2 className="eyebrow">Conversation</h2>
            <p>one door — every action streams through <code>/api/chat</code></p>
          </div>
          <div className="msgs">
            <div className="msg agent">
              <div className="from">Recal</div>
              <div className="text">
                <p>I'm <b>Recal</b>. I plan study into the gaps your calendar already has, answer from a
                  knowledge graph of what you have ingested, and build 60-second visual explainers.</p>
                <p>Start with a chip below, or paste a documentation URL and I'll read it.</p>
              </div>
            </div>
            {messages.map((m, i) =>
              m.role === 'status'
                ? <div key={i} ref={el => { msgRefs.current[i] = el }}
                       className={'log' + (m.err ? ' err' : '')}>{linkify(m.content)}</div>
                : <div key={i} ref={el => { msgRefs.current[i] = el }}
                       className={'msg ' + m.role + (hl === i ? ' hl' : '')}>
                    <div className="from">{m.role === 'you' ? 'You' : 'Recal'}</div>
                    <div className="text">
                      {m.role === 'agent' ? <Markdown>{m.content}</Markdown> : m.content}
                    </div>
                  </div>)}
            <div ref={endRef} />
          </div>
          <div className="chips">
            {CHIPS.map(c =>
              <button key={c.label} className="chip" disabled={busy}
                      onClick={() => c.action ? act(c.action, c.label) : run({ message: c.message })}>
                {c.label}</button>)}
          </div>
          <form className="composer" onSubmit={send}>
            <input value={input} disabled={busy} onChange={e => setInput(e.target.value)}
                   placeholder="Paste a docs URL, or: plan 4 hours this week - client demo Friday" />
            <button className="send" type="submit" disabled={busy || !input.trim()} aria-label="Send">
              <svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 20.5 21 12 3 3.5l2.6 7.1L14 12l-8.4 1.4z" /></svg>
            </button>
          </form>
        </section>

        <section className="pane">
          <div className="pane-head">
            <div className="tabs">
              {TABS.map(([k, label]) =>
                <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{label}</button>)}
            </div>
            <div className="meter">
              <div><b>{graph.chunks ?? 0}</b><span>chunks</span></div>
            </div>
          </div>
          <div className="pane-body">
            {tab === 'slides' &&
              <SlidesView cards={cards} index={reelIdx} onIndex={setReelIdx}
                          act={act} onJump={jumpToQuery} />}
            {tab === 'week' && <WeekView events={week} note={note} act={act} />}
            {tab === 'cards' && (cards.length
              ? <div className="feed">{[...cards].reverse().map(c => renderCard(c, act))}</div>
              : <div className="empty-note">Reels, pages, plans and calendar changes collect here as you chat.</div>)}
            {tab === 'practice' &&
              <PracticeView log={log}
                            onCard={c => { setCards(cs => [...cs, c]); setTab('cards') }} />}
          </div>
        </section>
      </main>

      {keysOpen && <ModelSettings onMock={setMock} onDone={() => setKeysOpen(false)} />}
    </>
  )
}
