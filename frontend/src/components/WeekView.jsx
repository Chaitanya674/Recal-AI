import React, { useEffect, useState } from 'react'

/** Week time grid from state.week (backend returns Monday + 10 days of events):
 *  hour rows × day columns, events placed by start/end, overlaps side by side. */

const KINDS = ['work', 'study', 'review', 'recovery']
const DAY = 864e5
const HOUR = 44                                 // px per hour row; keep in sync with --hour
const FIRST_HOUR = 7, LAST_HOUR = 23            // default visible range (planner uses 07:00–22:30)
const hm = (d) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
const hours = (d) => d.getHours() + d.getMinutes() / 60

/** Assign side-by-side lanes to overlapping events (list sorted by start). */
function lanes(list) {
  const out = []
  let group = [], groupEnd = 0, laneEnds = []
  const flush = () => {
    group.forEach(x => { x.of = laneEnds.length })
    out.push(...group)
    group = []; groupEnd = 0; laneEnds = []
  }
  for (const e of list) {
    if (group.length && e.s >= groupEnd) flush()
    let lane = laneEnds.findIndex(end => end <= e.s)
    if (lane < 0) { lane = laneEnds.length; laneEnds.push(e.e) } else laneEnds[lane] = e.e
    group.push({ e, lane })
    groupEnd = Math.max(groupEnd, +e.e)
  }
  flush()
  return out
}

function Event({ e, top, height, lane, of, act }) {
  const free = e.busy === 0
  return (
    <div className={`ev ${e.kind} ${e.status}${free ? ' free' : ''}${height < 38 ? ' short' : ''}`}
         style={{ top, height, left: `calc(${(lane / of) * 100}% + 2px)`, width: `calc(${100 / of}% - 4px)` }}
         title={`${hm(e.s)}–${hm(e.e)}  ${e.title}${free ? "\nDoesn't block study time" : e.why ? '\n' + e.why : ''}`}>
      <time>{hm(e.s)}–{hm(e.e)}</time>
      <span className="t">{e.title}</span>
      {e.id.startsWith('gcal:') &&
        <span className="tag g">{e.source || 'Google'}{free ? ' · free' : ''}</span>}
      {e.moved_from && <span className="tag">moved</span>}
      {e.kind !== 'work' && e.status === 'planned' &&
        <button title="Mark done"
                onClick={() => act({ type: 'mark_status', event_id: e.id, status: 'done' })}>✓</button>}
    </div>)
}

export default function WeekView({ events, note, act }) {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => { const t = setInterval(() => setNow(new Date()), 60e3); return () => clearInterval(t) }, [])

  const monday = new Date(now)
  monday.setHours(0, 0, 0, 0)
  monday.setDate(monday.getDate() - (monday.getDay() + 6) % 7)

  const evs = events.map(e => ({ ...e, s: new Date(e.start), e: new Date(e.end) }))
  // always show Mon–Sun; extend into next week if the plan spilled over
  const last = evs.reduce((n, e) => Math.max(n, Math.floor((e.s - monday) / DAY)), 6)
  const days = Array.from({ length: Math.min(last + 1, 10) }, (_, i) =>
    new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + i))
  const today = now.toDateString()

  const timed = evs.filter(e => !e.all_day)
  const allDay = evs.filter(e => e.all_day)
  // widen the visible hours if anything starts earlier / ends later than the default
  const first = Math.min(FIRST_HOUR, ...timed.map(e => Math.floor(hours(e.s))))
  const lastH = Math.max(LAST_HOUR, ...timed.map(e => e.e.toDateString() === e.s.toDateString()
    ? Math.ceil(hours(e.e)) : 24))
  const rows = Array.from({ length: lastH - first }, (_, i) => first + i)
  const y = (d) => (hours(d) - first) * HOUR

  return (
    <div>
      {note && <div className="week-note"><b>{note.title}</b>{note.text}</div>}
      {!note && events.length === 0 &&
        <div className="week-note"><b>Your week is empty</b>
          Connect Google Calendar to see your meetings, or ask Recal to plan study time.</div>}
      <div className="wk" style={{ '--days': days.length, '--hour': `${HOUR}px` }}>
        <div className="wk-row wk-head">
          <div />
          {days.map(d =>
            <div key={d.toDateString()} className={'wk-day' + (d.toDateString() === today ? ' today' : '')}>
              {d.toLocaleDateString([], { weekday: 'short' })}<b>{d.getDate()}</b></div>)}
        </div>

        {allDay.length > 0 &&
          <div className="wk-row wk-allday">
            <div className="wk-axis-label">all day</div>
            {days.map(d =>   // all-day events cover [start, end): show on every day in between
              <div key={d.toDateString()} className="wk-cell">
                {allDay.filter(e => e.s <= d && d < e.e).map(e =>
                  <div key={e.id} className={`ev ${e.kind} free allday`} title={`${e.title} (all day)`}>
                    <span className="t">{e.title}</span>
                    {e.source && <span className="tag g">{e.source}</span>}
                  </div>)}
              </div>)}
          </div>}

        <div className="wk-row wk-body" style={{ height: rows.length * HOUR }}>
          <div className="wk-axis">
            {rows.map(h => <span key={h} style={{ top: (h - first) * HOUR }}>{String(h).padStart(2, '0')}:00</span>)}
          </div>
          {days.map(d => {
            const key = d.toDateString()
            const list = timed.filter(e => e.s.toDateString() === key).sort((a, b) => a.s - b.s)
            return (
              <div key={key} className={'wk-col' + (key === today ? ' today' : '')}>
                {lanes(list).map(({ e, lane, of }) => {
                  const end = e.e.toDateString() === key ? e.e : new Date(d.getTime() + DAY)
                  const top = y(e.s)
                  return <Event key={e.id} e={e} lane={lane} of={of} act={act}
                                top={top} height={Math.max(18, (end - e.s) / 36e5 * HOUR - 2)} />
                })}
                {key === today && hours(now) >= first && hours(now) <= lastH &&
                  <div className="wk-now" style={{ top: y(now) }} />}
              </div>)
          })}
        </div>
      </div>
      <div className="legend">
        {KINDS.map(k => <span key={k} className={k}><i />{k}</span>)}
        <span className="free"><i />dashed = shown, doesn't block study time</span>
      </div>
    </div>)
}
