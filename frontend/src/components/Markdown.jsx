import React, { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import Mermaid from './Mermaid.jsx'

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const nodeText = (n) => n?.value ?? (n?.children || []).map(nodeText).join('')
const FENCE = /(```[\s\S]*?```)/g
const ID_LIKE = /^(?:[0-9a-f]{12}|c\d{1,3})$/

/** Source ids the model cites become numbered superscripts. Grouped ("[a, b]") and
 *  adjacent ("[a], [b]") citations merge into one ("1,2"); citation-shaped ids that
 *  are not listed sources are dropped. Never inside fenced code. */
function linkCitations(md, citations) {
  if (!citations?.length) return md
  const num = new Map(citations.map((c, i) => [c.chunk_id, i + 1]))
  const ungroup = (part) => part.replace(/( ?)\[([^\[\]\n]{1,240})\](?!\()/g, (m, sp, inner) => {
    const ids = inner.trim().split(/[\s,;]+/).filter(Boolean)
    if (!ids.length || !ids.every(x => ID_LIKE.test(x))) return m
    const known = [...new Set(ids)].filter(x => num.has(x))
    return known.length ? sp + known.map(x => `[${x}]`).join('') : ''
  })
  const one = `\\[(?:${[...num.keys()].map(esc).join('|')})\\]`
  const run = new RegExp(`${one}(?:\\s*,?\\s*${one})*`, 'g')
  const single = new RegExp(one.replace('(?:', '('), 'g')
  return md.split(FENCE).map((part, i) => i % 2 ? part
    : ungroup(part).replace(run, (m) => {
        const ns = [...new Set([...m.matchAll(single)].map(x => num.get(x[1])))]
        return `[${ns.join(',')}](#cite-${ns.join(',')})`
      }))
    .join('')
}

/** Display math on its own lines (one-line "$$...$$" otherwise renders inline), and
 *  matrix row breaks that lost a backslash in JSON ("2 & 0 \ 0 & 2") restored. The
 *  backend repairs new pages the same way; this keeps older saved pages readable. */
const ENV = /\\begin\{(?:[pbvBV]?matrix|smallmatrix|cases|aligned|align\*?|array|gathered|split)\}[\s\S]*?\\end\{[^}]+\}/g
function repairMath(md) {
  return md.split(FENCE).map((part, i) => i % 2 ? part : part
    .replace(/\$\$([\s\S]+?)\$\$/g, (_, body) => '\n\n$$\n' + body.trim() + '\n$$\n\n')
    .replace(ENV, env => env.replace(/(?<!\\)\\(?=\s|\[\d)/g, () => '\\\\')))
    .join('')
}

/** KaTeX (~300 KB) loads only when a page actually contains math. */
let math = null
let mathLoading = null
function loadMath() {
  mathLoading ||= Promise.all([import('remark-math'), import('rehype-katex'),
                               import('katex/dist/katex.min.css')])
    .then(([rm, rk]) => {
      math = { remark: rm.default, rehype: [rk.default, { throwOnError: false, strict: false }] }
    })
  return mathLoading
}

/** Safe markdown rendering for model output (GFM: tables, lists, code, mermaid, math).
 *  react-markdown builds a React tree — no raw-HTML injection possible. */
export default function Markdown({ children, citations }) {
  const text = children || ''
  const hasMath = text.includes('$')
  const [, setMathReady] = useState(Boolean(math))
  useEffect(() => {
    if (hasMath && !math) loadMath().then(() => setMathReady(true)).catch(() => {})
  }, [hasMath])
  const withMath = hasMath && math
  const cited = (href) => href.slice(6).split(',').map(n => citations?.[Number(n) - 1] || {})
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={withMath ? [remarkGfm, math.remark] : [remarkGfm]}
        rehypePlugins={withMath ? [math.rehype] : []}
        components={{
          a: ({ node, href = '', ...props }) => {
            if (!href.startsWith('#cite-'))
              return <a href={href} {...props} target="_blank" rel="noreferrer" />
            const cs = cited(href)
            const title = cs.map(c => c.title || 'source').join(' · ')
            return <sup className="cite" title={title}>{cs.length === 1 && cs[0].url
              ? <a href={cs[0].url} target="_blank" rel="noreferrer">{props.children}</a>
              : props.children}</sup>
          },
          pre: ({ node, ...props }) => {
            const code = node?.children?.[0]
            return (code?.properties?.className || []).includes('language-mermaid')
              ? <Mermaid chart={nodeText(code)} />
              : <pre {...props} />
          },
        }}>
        {linkCitations(hasMath ? repairMath(text) : text, citations)}
      </ReactMarkdown>
    </div>
  )
}
