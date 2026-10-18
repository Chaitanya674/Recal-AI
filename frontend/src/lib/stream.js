/** POST to /api/chat and parse the SSE stream (plan §3.2).
 * handlers: {onStatus,onToken,onCard,onState,onReceipt,onError,onDone} */
export async function streamChat(body, handlers) {
  const res = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`/api/chat returned HTTP ${res.status}` +
                               (res.status >= 500 ? ' (is the backend running on :8000?)' : ''))
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  const dispatch = (event, data) => {
    const h = {
      status: handlers.onStatus, token: handlers.onToken, card: handlers.onCard,
      state: handlers.onState, receipt: handlers.onReceipt,
      error: handlers.onError, done: handlers.onDone,
    }[event]
    if (h) h(data)
  }
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx); buf = buf.slice(idx + 2)
      let event = 'message', data = {}
      for (const line of frame.split('\n')) {
        if (line.startsWith('event: ')) event = line.slice(7).trim()
        if (line.startsWith('data: ')) { try { data = JSON.parse(line.slice(6)) } catch {} }
      }
      dispatch(event, data)
    }
  }
  handlers.onDone?.({})
}
export const api = (path, body, method) =>
  fetch(path, body ? { method: method || 'POST', headers: { 'Content-Type': 'application/json' },
                       body: JSON.stringify(body) } : { method: method || 'GET' }).then(r => r.json())
