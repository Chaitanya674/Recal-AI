# Recal: Technical Documentation

> Architecture, flows, data model and API reference for Recal, an AI learning agent built on
> Gemini 3.x (Vertex AI), Google ADK and Firestore. For the product overview see the
> [README](../README.md); for deployment see [deploy/README.md](../deploy/README.md).

## Contents

1. [System overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Request lifecycle: one chat door](#3-request-lifecycle-one-chat-door)
4. [The Switchboard: model routing and verification](#4-the-switchboard-model-routing-and-verification)
5. [Knowledge graph and source acquisition](#5-knowledge-graph-and-source-acquisition)
6. [60-second visual reels](#6-60-second-visual-reels)
7. [Managed study sessions](#7-managed-study-sessions)
8. [Renegotiation and session edits](#8-renegotiation-and-session-edits)
9. [AI client: document review, mock meeting, rehearsal](#9-ai-client-document-review-mock-meeting-rehearsal)
10. [Concierge agent (Google ADK)](#10-concierge-agent-google-adk)
11. [Style memory](#11-style-memory)
12. [Google Calendar and Docs integration](#12-google-calendar-and-docs-integration)
13. [Guardrails and security](#13-guardrails-and-security)
14. [Data model](#14-data-model)
15. [Resilience and graceful degradation](#15-resilience-and-graceful-degradation)
16. [API reference](#16-api-reference)
17. [Configuration reference](#17-configuration-reference)
18. [Frontend](#18-frontend)
19. [Deployment architecture](#19-deployment-architecture)
20. [Testing and observability](#20-testing-and-observability)
21. [Limitations and roadmap](#21-limitations-and-roadmap)

---

## 1. System overview

Recal turns a learning intent into scheduled, grounded, verified study material:

| Capability | Entry point | Core modules |
|---|---|---|
| Visual reels (60 s, narrated, MP4/PDF) | chat: *"explain X visually"* | `motion`, `reel_engine`, `render`, `knowledge` |
| Study planning on the real calendar | chat: *"plan 2 hours for X"* | `calendar`, `google` |
| Study books with citations + critic | chat: *"write me notes on X"* | `material`, `knowledge` |
| Re-planning (urgent work, low energy) | quick actions / chat | `calendar` |
| Grounded Q&A | chat: a question | `knowledge`, `llm` |
| Deck/doc review, mock meeting, rehearsal | Practice tab | `documents`, `meeting`, `rehearse` |
| Learned teaching style | 👍 / 👎 on any output | `memory` |

**Design principles**

1. **One door.** Every chat message goes through `POST /api/chat`. A planner model splits it
   into ordered steps, and there is no keyword routing.
2. **Verify, don't trust.** Model output is checked by code (schemas, citations, slot IDs)
   before it is used. A failed check gets one escalation to the Pro tier with the reason.
3. **Always degrade, never die.** Every pipeline has a deterministic fallback.
4. **Grounded by construction.** Reels, notes and answers are built from ingested chunks and
   cite chunk IDs, and outside text is always marked as untrusted data.
5. **Auditable.** Every result carries a receipt (model, thinking level, verified, escalated).

---

## 2. Architecture

### 2.1 Component view

```mermaid
flowchart TB
    subgraph Client["Browser"]
        UI["React 19 + Vite SPA<br/>Chat · Canvas · Week · Cards · Practice"]
    end

    subgraph Server["FastAPI backend (uvicorn, 1 worker)"]
        MW["Middleware<br/>rate limit · request log"]
        subgraph Routers
            CHAT["chat_router<br/>/api/chat SSE"]
            AUTH["auth_router<br/>/api/auth · /api/connect"]
            DOCR["doc_router<br/>/api/docs"]
            MEET["meeting_router<br/>/api/meeting"]
            REEL["reel_router<br/>/api/reels"]
            META["meta_router<br/>/api/health · feedback"]
        end
        subgraph Features
            INT["intents<br/>planner"]
            LLM["llm<br/>Switchboard"]
            KNOW["knowledge<br/>graph + search"]
            MOT["motion<br/>reel pipeline"]
            REN["render + reel_engine<br/>MP4 / PDF"]
            MAT["material<br/>study books"]
            CAL["calendar<br/>slots · plan · replan"]
            DOC["documents · meeting · rehearse"]
            MEM["memory<br/>chat · cards · style"]
            GOO["google<br/>OAuth · Calendar · Docs"]
        end
        AG["agents<br/>ADK concierge"]
        HLP["helper<br/>guardrails · prompts.yml · config"]
        DB["database<br/>fire · schema · graph · vectors"]
    end

    subgraph GCP["Google Cloud"]
        VX[("Vertex AI<br/>Gemini 3.x")]
        FS[("Firestore")]
        GCS[("Cloud Storage")]
        SM[("Secret Manager")]
    end
    GAPI[("Google Calendar /<br/>Docs / Drive APIs")]
    WEB[("Web pages<br/>Wikipedia · search")]

    UI --> MW --> Routers
    CHAT --> INT & AG & Features
    Features --> LLM --> VX
    AG --> VX
    Features --> DB --> FS
    REN --> GCS
    GOO --> GAPI
    KNOW --> WEB
    Routers --> HLP
```

### 2.2 Layering rules

| Layer | Responsibility | Never does |
|---|---|---|
| `router/` | HTTP and SSE framing, user resolution (cookie), card emission | business logic |
| `features/` | pipelines: plan, reel, notes, review, ingest | HTTP concerns |
| `features/llm.py` | the **only** module that calls Gemini (plus `knowledge.embed` via its client) | persistence |
| `database/` | the **only** module that touches Firestore and Cloud Storage | model calls |
| `helper/prompts.yml` | every prompt, versioned in one file (optional Firestore overrides) | — |

---

## 3. Request lifecycle: one chat door

### 3.1 Sequence

```mermaid
sequenceDiagram
    autonumber
    actor L as Learner
    participant UI as React UI
    participant MW as Middleware
    participant CR as chat_router
    participant GR as guardrails.check_input
    participant PL as intents.plan_steps
    participant H as step handler
    participant MEM as memory
    participant FS as Firestore

    L->>UI: types a message
    UI->>MW: POST /api/chat {message}
    MW->>MW: rate limit (30 POST/min per user)
    MW->>CR: forward
    CR->>MEM: append user message
    CR->>CR: sync Google Calendar mirror (60 s TTL)
    par guard and planner run concurrently
        CR->>GR: lite-tier safety verdict
        CR->>PL: lite-tier decomposition (temp 0)
    end
    alt blocked
        GR-->>CR: {allow:false, category}
        CR-->>UI: token (fixed refusal text) + receipt
    else allowed
        PL-->>CR: steps [{action, topic, hours?, prefer?, on_date?}]
        loop each step (max 5)
            CR->>H: handle(step)
            H-->>CR: status / token / card events
            CR-->>UI: SSE stream
            CR->>MEM: save cards
        end
    end
    CR->>MEM: append assistant message + receipt
    MEM->>FS: write-through
    CR-->>UI: state {week, graph, connections, mock}
    CR-->>UI: done
```

### 3.2 Planner actions

`intents.plan_steps` returns up to 5 ordered steps. Each `action` maps to one pipeline:

| action | Pipeline | Card |
|---|---|---|
| `plan` | `calendar.plan`: decompose + place into free slots | `plan` |
| `reschedule` | `calendar.edit_by_request`: verified edits | `calendar_diff` |
| `reel` | `motion.make_reel` (+ auto source acquisition) | `source`*, `reel` |
| `material` | `material.draft_material` → `review_and_revise` | `source`*, `page` |
| `answer` | `knowledge.ask` → grounded stream, or Google-Search-grounded stream | — |
| `ingest` | `knowledge.ingest_url` | `source` |
| `chat` | ADK concierge (`agents/workflow.run_freeform`) | — |

A compound turn such as *"plan 2 hours tomorrow evening and prep notes"* becomes
`plan → material`. The material is sized to the booked minutes (about one page per 15 minutes).

### 3.3 Direct actions (buttons, no planner)

`POST /api/chat {action: {type, ...}}`

| type | Effect |
|---|---|
| `bootstrap` | returns full state + history (page load) |
| `reset` | clears chat, cards and Recal sessions for the user |
| `mark_status` | marks a session done or missed; if pages remain, offers a follow-up session |
| `urgent` | inserts urgent work, then runs `replan` |
| `replan` / `energy` | renegotiates the week (`energy=tired` lightens it) |
| `export` | exports a card to a new Google Doc |

### 3.4 SSE event protocol

Events are ordered `(status | token | card)* → receipt* → state? → done`:

| event | payload |
|---|---|
| `status` | `{text}`: progress line ("Reading https://…") |
| `token` | `{delta}`: streamed prose |
| `card` | `Card {id, type, title, payload, receipt}` |
| `receipt` | `{model, thinking, verified, escalated, ms, reason}` |
| `state` | `{week, graph, connections, mock, history?}` |
| `error` | `{message, recoverable: true}`; the stream still ends with `done` |
| `done` | `{}` |

Every SSE payload passes through `guardrails.redact` before it leaves the server.

---

## 4. The Switchboard: model routing and verification

`llm.route(kind, prompt, verifier, mock_output)` is the single entry point for structured
model work. It **never raises**: callers always get either verified output or their own
deterministic fallback.

### 4.1 Tier selection

| kind | tier / thinking | kind | tier / thinking |
|---|---|---|---|
| `intent` | lite / minimal | `plan`, `replan`, `session_edit` | deep / high |
| `guard` | lite / minimal | `reel_visual_script` | deep / high |
| `style` | lite / minimal | `reel_focus`, `reel_scene_json` | fast / low |
| `chat`, `ask`, `page` | fast / low | `draft_review` | fast / high |
| `doc_review`, `model_knowledge` | fast / low | anything else | fast / low |

The lite tier runs at **temperature 0**, so the same message always routes the same way.
Gemini 3.x uses thinking *levels*. Flash and Pro reject `MINIMAL`, so "none" maps to `LOW`
on those tiers.

### 4.2 Execute → verify → escalate

```mermaid
flowchart TD
    A["route(kind, prompt, verifier)"] --> M{"Mock mode or<br/>no Vertex client?"}
    M -- yes --> MO["Return mock_output<br/>receipt: mock-tier"]
    M -- no --> L{"This kind+domain<br/>escalated ≥ 3 times?"}
    L -- yes --> D["Start on deep / high<br/>(learned routing)"]
    L -- no --> T["Start on table tier"]
    D --> C1["chat_json: JSON mode<br/>+ one repair retry"]
    T --> C1
    C1 --> V1{"Call ok and<br/>verifier passes?"}
    V1 -- yes --> OK(["Output + receipt<br/>verified = true"])
    V1 -- no --> E["Escalate once: deep / high<br/>prompt + 'failed: reason. Fix it.'"]
    E --> V2{"Passes now?"}
    V2 -- yes --> OKE(["Output + receipt<br/>escalated = true"])
    V2 -- no --> FB(["Caller's deterministic fallback<br/>verified = false + reason"])
```

### 4.3 Transport resilience

```mermaid
flowchart LR
    CALL["Gemini call<br/>(90 s server deadline)"] --> R{"Result"}
    R -- "200" --> DONE(["return"])
    R -- "429 / 503" --> W["wait 2 s · 4 s · 6 s<br/>up to 3 retries"] --> CALL
    R -- "504 deadline" --> ONCE{"First attempt?"}
    ONCE -- yes --> CALL
    ONCE -- no --> RAISE(["raise → route() escalates<br/>or caller falls back"])
    R -- "other error" --> RAISE
```

Streams retry only **before** the first chunk, so a retry never duplicates text the user
already saw.

### 4.4 JSON extraction

`extract_json` tries, in order: a whole-text parse, the last fenced ```` ```json ```` block,
and the last balanced `{…}` object. This lets prompts ask for a short explanation followed
by JSON (see §5.3) without breaking parsing.

---

## 5. Knowledge graph and source acquisition

### 5.1 Graph model

The per-user graph (`database/graph_store.GraphStore` + `features/knowledge.Knowledge`) has:

| Node kind | Fields | Embedded |
|---|---|---|
| `Source` | url, title (raw text stored separately in `sources/{id}`) | — |
| `Chunk` | text, ordinal | ✓ 768-d |
| `Concept` | label, domain | ✓ 768-d |
| `Generation` | gen_kind (anchors what an output was grounded in) | — |

| Edge | Meaning |
|---|---|
| `Source -HAS_CHUNK-> Chunk` | the page was cut into this passage |
| `Chunk -MENTIONS-> Concept` | the passage discusses the concept |
| `Concept -<REL>-> Concept` | extracted triple, e.g. `KAFKA -HAS_API-> PRODUCER_API` |
| `Generation -GROUNDED_IN-> Chunk` | an answer, reel or page cited this passage |

The in-memory dicts are a write-through cache over Firestore and are loaded lazily per user.

### 5.2 Ingestion

```mermaid
sequenceDiagram
    autonumber
    participant P as Pipeline
    participant K as knowledge
    participant W as Web page
    participant G as Gemini
    participant FS as Firestore

    P->>K: ingest_url(user, url)
    K->>W: GET (descriptive User-Agent)
    W-->>K: HTML
    K->>K: readable text (Wikipedia: extracts API)
    K->>FS: Source node + sources/{id} raw text (≤ 500 KB)
    K->>K: chunk text
    loop each chunk
        K->>G: embed (gemini-embedding-001, 768-d, normalized)
        K->>FS: Chunk node + vector + HAS_CHUNK edge
    end
    K->>G: route("ask"): extract ≤ 8 concept triples
    K->>FS: Concept nodes (deduplicated by vector, cosine > 0.86) + edges
    K-->>P: {title, chunks, concepts}
```

### 5.3 Finding sources (when the graph has nothing on a topic)

```mermaid
flowchart TD
    S(["find_sources(topic)"]) --> G1["Gemini + Google Search grounding<br/>prompt: describe each page, then JSON"]
    G1 --> G2["Candidates = grounding metadata<br/>(real results) + model-written URLs"]
    G2 --> G3["Live check in parallel:<br/>follow redirects, drop 4xx/5xx"]
    G3 --> G4{"Any reachable?"}
    G4 -- yes --> OUT(["Top 3, ranked:<br/>docs/tutorials > blogs > Wikipedia"])
    G4 -- no --> W1["Keyless web search<br/>(DuckDuckGo HTML)"]
    W1 --> W2{"Results?"}
    W2 -- yes --> OUT
    W2 -- no --> WK["Wikipedia search<br/>(flagged as fallback)"]
    WK --> OUT
    OUT --> ING["Pipeline ingests each URL"]
    ING --> ANY{"Anything ingested?"}
    ANY -- no --> MK["Model knowledge source:<br/>≥ 150-word explainer,<br/>titled 'Model knowledge: …'"]
```

Two details matter here:

- **Why the "describe each page" step.** Vertex only attaches grounding metadata (the real
  result URLs) to text that makes claims. With a JSON-only reply it attaches none.
- **Why the live check.** URLs the model writes into its reply can be invented, and
  checking them stops the chat from showing *"Reading … 404"*.

### 5.4 Retrieval: `relevant_chunks`

- **Real embeddings:** vector KNN over chunks and concepts. A hit is kept if
  cosine ≥ `REEL_MIN_SIMILARITY` (0.6) **or** it contains at least half the topic's
  keywords. Concept hits pull in their mentioning chunks.
- **Hash mode** (mock, or an embedding-quota cooldown): keyword-overlap ranking over all chunks.
- **Embedding failure:** the system switches to hash mode for 10 minutes (a circuit
  breaker), so ingestion never goes dark.

---

## 6. 60-second visual reels

### 6.1 Pipeline

```mermaid
sequenceDiagram
    autonumber
    participant CR as chat_router
    participant M as motion.make_reel
    participant K as knowledge
    participant F as Flash (fast)
    participant P as Pro (deep)
    participant T as Gemini TTS
    participant R as render
    participant GCS as Cloud Storage

    CR->>M: make_reel(user, topic, request)
    M->>K: relevant_chunks(topic, k=8)
    alt nothing relevant
        M-->>CR: None → find_sources, ingest, retry once
    end
    M->>M: extractive_script (deterministic fallback, built first)
    M->>F: 1 · reel_focus: keep facts that serve the ask
    M->>P: 2 · reel_visual_script: storyboard beats (archetypes, running example)
    M->>F: 3 · reel_scene_json: compile beats into scenes
    M->>M: validate_script (escalation on failure, via route)
    loop each scene, sequential
        M->>T: speak(narration)
        T-->>M: WAV → 16-bit mono 24 kHz PCM
    end
    M->>R: save_package + prerender (background MP4)
    R->>GCS: scene WAVs, reel.mp4, reel.pdf
    M-->>CR: {scene_script, durations, audio URLs, narration, generation_id}
```

### 6.2 Validation rules (`motion.validate_script`)

- The script matches the `SceneScript` schema, and each scene's `slots` match its archetype model.
- Narration is at most **25 words** per scene.
- Every `grounded_in` is an **existing chunk ID** in the user's graph.
- Estimated runtime (words / 2.5 + a pad per scene) is at most **60 s**.

If the model fails twice, the **extractive fallback** ships instead, built from source
sentences. The receipt states why.

### 6.3 Scene archetypes

`title`, `build_diagram`, `compare_two`, `timeline`, `code_reveal`, `counter`, `callout`, `flow`,
`bar_chart`, `token_attention`, `matrix_heat`, `vector_space`, `cycle`, `keyword_match`.

The storyboard prompt pushes for variety: at least 4 different archetypes, no two
consecutive scenes alike, concrete labels, and one running example.

### 6.4 Narration fallback chain

```mermaid
flowchart LR
    A["MODEL_TTS<br/>gemini-3.8-flash-tts"] -- "fails" --> B["MODEL_TTS_FALLBACK<br/>flash-lite-tts<br/>(separate quota)"]
    B -- "fails" --> C["Local voice<br/>pyttsx3 / espeak-ng"]
    C -- "fails" --> D["Captions only<br/>(silence, correct timing)"]
```

Scenes are voiced **sequentially**. A 429 waits 15 s once; a scene that still fails gets
silence of the right length, so one rate-limited scene doesn't mute the reel.

### 6.5 Rendering

`render` draws frames with Pillow (2× supersampled for the pictorial scenes) and encodes
H.264/AAC with the ffmpeg binary bundled in `imageio-ffmpeg`, so it needs no GPU and no
system install. `prerender` starts encoding in the background as soon as the reel exists,
so *Download MP4* is usually instant. In testing, MP4 took about 20 s and PDF slides about 2 s.

---

## 7. Managed study sessions

### 7.1 Planning

```mermaid
sequenceDiagram
    autonumber
    participant CR as chat_router
    participant C as calendar
    participant G as Google Calendar mirror
    participant P as Pro (deep/high)

    CR->>C: plan(goal, hours=2, prefer="evening", on_date="2026-10-19")
    C->>G: events for the window (busy blocks)
    C->>C: free_slots: gaps ≥ 45 min inside the window,<br/>daily study cap MAX_STUDY_MIN_PER_DAY
    alt no slot in the preferred window
        C->>C: widen to the whole day + note why
    end
    C->>P: plan_decompose: subtopics with minutes + why
    C->>C: place subtopics into slots in order<br/>never exceed the stated hours, never invent a slot
    C-->>CR: {summary, sessions[]} → plan card + Week refresh
```

Time windows: morning 07:00–12:00, afternoon 12:00–17:00, evening 17:00–22:30.

### 7.2 Study book (multi-call) with critic

```mermaid
flowchart TD
    A["draft_material(topic, minutes)"] --> B{"Relevant chunks?"}
    B -- no --> SRC["find_sources → ingest → retry"]
    B -- yes --> O["Outline call: pages sized to<br/>booked time (~1 page / 15 min)"]
    O --> W["One writer call per page<br/>(concept · code · exercise)"]
    W --> N["Normalize citations + repair math"]
    N --> CIT{"Any [chunk_id] cited?"}
    CIT -- no --> EX["Ship extractive page<br/>(grounded by construction)"]
    CIT -- yes --> CR["Critic: alignment, grounding,<br/>purity, learner's taste"]
    CR --> SEV{"Severity"}
    SEV -- "pass / minor" --> SHIP(["Page card<br/>+ progress tracker 0/N"])
    SEV -- major --> REV["One revision with the<br/>critic's fixes (pass 2 of 2)"]
    REV --> SHIP
```

**Progress contract.** The Canvas reports the furthest page reached
(`POST /api/material/progress`). When a session is marked done with pages left, Recal
offers to book a follow-up session sized to the remaining pages.

---

## 8. Renegotiation and session edits

```mermaid
sequenceDiagram
    autonumber
    actor L as Learner
    participant CR as chat_router
    participant C as calendar
    participant P as Pro (deep/high)

    alt "Urgent client meeting" button
        L->>CR: action urgent {title, minutes}
        CR->>C: add_event(work, tomorrow)
        CR->>C: replan()
    else "I'm tired"
        L->>CR: action energy {level: tired}
        CR->>C: replan(energy="tired")
    else free text: "move tomorrow's session to 7pm"
        L->>CR: message → planner: reschedule
        CR->>C: edit_by_request(request)
    end
    C->>C: list free slots (with slot IDs) + overdue + upcoming sessions
    C->>P: propose moves {event_id, action, to_slot_id | start}
    C->>C: VERIFY: only known event IDs, only returned slot IDs,<br/>moves land inside real free windows
    alt verification fails twice
        C-->>CR: ask one clarifying question, change nothing
    else verified
        C->>C: apply move / shrink / drop (drop = marked missed, never deleted silently)
        C-->>CR: calendar_diff card {narrative, moves}
    end
```

Google Calendar events are read-only mirrors: Recal only ever edits its own sessions.

---

## 9. AI client: document review, mock meeting, rehearsal

### 9.1 Upload and review

```mermaid
sequenceDiagram
    autonumber
    actor L as Learner
    participant D as documents
    participant K as knowledge
    participant G as Gemini (fast)

    L->>D: POST /api/docs (PPTX / DOCX / XLSX ≤ 15 MB)
    D->>D: parse into sections (slides, headings, sheets)
    D->>K: feed text into the knowledge graph
    L->>D: POST /api/docs/{id}/review {mode}
    D->>D: heuristic findings in code<br/>(jargon, walls of text, numbers without units, cryptic headers)
    D->>G: route("doc_review"): findings as hints, model deepens them
    alt model output malformed
        D-->>L: heuristic review (still useful)
    else verified
        D-->>L: doc_review card {headline, issues[]}
    end
```

Modes: `message` (did the deck land its message?) and `client_ready` (can it go to a client?).

### 9.2 Mock client meeting

```mermaid
stateDiagram-v2
    [*] --> Prep: POST /api/meeting/start {doc_id | topic}
    Prep: Client studies the material<br/>understood vs unclear → agenda
    Prep --> Asking
    Asking: Client asks about the next agenda item
    Asking --> Judging: POST /api/meeting/turn {said}
    Judging: Did the explanation resolve it?<br/>one gap + one follow-up
    Judging --> Asking: next question
    Judging --> Debrief: POST /api/meeting/end
    Debrief: verdict · strongest moments ·<br/>fixes · what to drill next
    Debrief --> [*]
```

The browser handles the audio: SpeechRecognition for listening and speechSynthesis for the
client's voice, so a meeting costs zero model audio quota. Every turn goes through the input
guard. Without a model, a heuristic agenda (unexplained acronyms, numbers without
baselines) keeps the meeting running.

### 9.3 Rehearsal coach

`POST /api/docs/{id}/coach` (multipart: `section`, `audio`): the learner presents one slide
aloud. The audio is converted to 16 kHz WAV and sent with the slide text to Gemini, which
returns `{transcript, feedback, question}`: what landed, the gap versus the slide's claim,
and one client cross-question. Without a model, Recal asks a canned question built from the
slide itself.

---

## 10. Concierge agent (Google ADK)

Messages the planner can't map to a pipeline (small talk, open questions, session tweaks) go
to an ADK `LlmAgent` built by `agents/agent_factory.AgentFactory`:

| Tool | Does |
|---|---|
| `search_context(question)` | top-5 relevant chunks from the user's graph, wrapped as untrusted data |
| `read_week()` | this week's sessions and mirrored Google events |
| `edit_study_session(request)` | `calendar.edit_by_request` with the same verification gate as §8 |

Tools are **closures over the user ID**, so the model can't address another user's data. The
instruction (`concierge_instruction` in `prompts.yml`) plus a standing security rule forbids
following instructions found in tool results. If ADK is unavailable, the same prompt is
answered by the chat model directly.

---

## 11. Style memory

```mermaid
sequenceDiagram
    autonumber
    actor L as Learner
    participant M as memory
    participant G as Gemini
    participant FS as Firestore

    L->>M: 👍 on a reel or page (generation_id)
    M->>G: distil the generation's trace into a reusable descriptor<br/>("prefers one running example, code before theory")
    M->>G: embed descriptor + topic
    M->>FS: style_memory (dedupe: cosine ≥ 0.9 reinforces instead)
    Note over M,FS: 👎 stores an "avoid" preference
    L->>M: later: new reel or notes on a related topic
    M->>FS: vector KNN over "prefer" styles (cosine ≥ 0.3)
    M-->>G: injected into the drafter and critic prompts
```

---

## 12. Google Calendar and Docs integration

### 12.1 OAuth (web server flow)

```mermaid
sequenceDiagram
    autonumber
    actor L as Learner
    participant UI as React UI
    participant API as auth_router
    participant GG as google
    participant GA as accounts.google.com
    participant FS as Firestore

    L->>UI: click "Calendar" pill
    UI->>API: GET /api/connect/calendar?return_to=origin
    API->>GG: auth_url(): state token (10 min), safe return_to
    API-->>UI: {auth_url}
    UI->>GA: redirect (scope calendar.readonly, offline access)
    GA-->>API: GET /api/connect/google/callback?code&state
    API->>GG: take_pending(state) + exchange code for tokens
    GG->>FS: connections/google_calendar = seal(tokens) (AES-256-GCM)
    API->>API: force calendar sync
    API-->>UI: redirect origin/?connected=calendar
```

| Provider | Scope | Use |
|---|---|---|
| `calendar` | `calendar.readonly` | mirror the next 14 days of events (busy and free), synced at most every 60 s |
| `gdocs` | `drive.file` | create a new Doc from a card; Recal can only see files it created |

`return_to` is restricted to localhost or `OAUTH_REDIRECT_BASE`, so the callback can't be
used as an open redirect.

---

## 13. Guardrails and security

```mermaid
flowchart TD
    IN(["User text"]) --> L1{"1 · Input guard<br/>≤ 4000 chars +<br/>lite-model verdict"}
    L1 -- "harmful / self_harm /<br/>prompt_injection" --> FIX(["Fixed refusal text<br/>(never model-written)"])
    L1 -- allow --> PIPE["Pipelines"]
    SRC(["Web pages · uploads ·<br/>retrieved chunks"]) --> L2["2 · Untrusted-data wrapping<br/>&lt;untrusted_data&gt; + standing rule"]
    L2 --> PIPE
    PIPE --> L3["3 · Output redaction<br/>configured secrets + key/token shapes"]
    L3 --> OUT(["SSE · API JSON · logs"])
    REQ(["POST /api/chat · docs ·<br/>meeting · reels"]) --> L4{"4 · Rate limiter<br/>30/min per user"}
    L4 -- over --> R429(["429 + Retry-After"])
```

| Concern | Control |
|---|---|
| Credentials | none on the server: Vertex, Firestore, GCS and Secret Manager use the VM's service account (ADC) |
| Secrets at rest | OAuth tokens sealed with Cloud KMS or AES-256-GCM (`RECAL_ENCRYPTION_KEY`); weaker seals are upgraded on read |
| Database access | Firestore rules deny all client access; only the backend reads or writes |
| Prompt injection | outside text is tagged as untrusted, and agent tools are scoped to the calling user |
| Guard outage | fails **open** (logged), so a model outage never locks a learner out |
| Logs | redacted formatter; on the VM no logs are written to disk at all |

---

## 14. Data model

### 14.1 Firestore layout (schema v2, `database/schema.py`)

```mermaid
erDiagram
    USER ||--o{ CONVERSATION : has
    CONVERSATION ||--o{ MESSAGE : contains
    USER ||--o{ CARD : has
    USER ||--o{ EVENT : schedules
    USER ||--o{ PROGRESS : tracks
    USER ||--o{ DOC : uploads
    DOC ||--o{ SECTION : "parsed into"
    USER ||--o{ NODE : "knowledge graph"
    USER ||--o{ EDGE : "knowledge graph"
    USER ||--o{ SOURCE_TEXT : "raw page text"
    USER ||--o{ GENERATION : produced
    USER ||--o{ STYLE_MEMORY : learned
    USER ||--o{ CONNECTION : "OAuth (sealed)"
    REEL }o--|| USER : "owned by"

    NODE {
        string kind "Concept | Chunk | Source | Generation"
        vector embedding "768-d, vector-indexed"
        string label_or_text
    }
    EDGE {
        string from
        string rel "HAS_CHUNK | MENTIONS | GROUNDED_IN | triple"
        string to
        float confidence
    }
    EVENT {
        datetime start
        datetime end
        string kind "study | review | work"
        string status "planned | done | missed"
        string topic
    }
    STYLE_MEMORY {
        string descriptor
        string polarity "prefer | avoid"
        vector embedding
        int reinforced
    }
```

| Path | Contents |
|---|---|
| `users/{uid}/conversations/{conv}/messages/{id}` | chat messages + receipts |
| `users/{uid}/cards/{id}` | card records |
| `users/{uid}/events/{id}` | study, review and work events |
| `users/{uid}/progress/{topic}` | study-book progress `{done, total}` |
| `users/{uid}/docs/{id}/sections/{n}` | parsed uploads |
| `users/{uid}/nodes/{id}`, `edges/{id}` | knowledge graph (vector-indexed `embedding`) |
| `users/{uid}/sources/{node_id}` | raw page text (never bulk-loaded) |
| `users/{uid}/generations/{id}`, `style_memory/{id}` | pedagogy memory |
| `users/{uid}/connections/google_{provider}` | sealed OAuth tokens |
| `reels/{gen_id}` | reel package metadata (`user` = owner) |
| `config/app` | runtime key/value settings |

**Vector indexes** (cosine, flat, 768-d): `nodes (kind ASC, embedding)` and
`style_memory (polarity ASC, embedding)`.

### 14.2 Cloud Storage layout

`users/{uid}/reels/{gen_id}/scene{n}.wav · reel.mp4 · reel.pdf`. Downloads use V4 signed
URLs; when the credentials can't sign, the server proxies the bytes instead.

---

## 15. Resilience and graceful degradation

| Failure | What the learner gets |
|---|---|
| No Google Cloud configured | **mock mode**: every feature works with canned output |
| Model returns invalid JSON | one repair retry, then escalation to Pro, then the deterministic fallback |
| 429 / 503 from Vertex | automatic retry with backoff (2/4/6 s) |
| Call stalls | 90 s deadline → 504 → one retry → fallback |
| Google Search grounding finds nothing | web search → Wikipedia → model-knowledge source |
| Embedding quota exhausted | hash vectors + keyword retrieval for 10 min |
| TTS rate-limited | fallback TTS model → local voice → captions with correct timing |
| Reel script fails validation twice | extractive reel built from source sentences |
| Notes without citations | extractive grounded page |
| Planner model down | the whole message goes to the concierge agent |
| ADK unavailable | concierge prompt answered by the chat model directly |
| Guard model down | fails open (logged) |
| Firestore unreachable | in-memory working set for the session |

---

## 16. API reference

| Method | Path | Body / query | Returns |
|---|---|---|---|
| POST | `/api/chat` | `{message}` or `{action:{type,…}}` | SSE stream (§3.4) |
| GET | `/api/conversations` | — | `{conversations:[{id,title,ts}]}` |
| POST | `/api/conversations` | — | `{id}` new conversation |
| POST | `/api/auth` | `{guest:true}` or `{id_token}` | `{ok,user}` + `recal_user` cookie |
| GET | `/api/connect/calendar` | `?return_to=` | `{auth_url}` |
| POST | `/api/connect/notebook` | `{return_to}` | `{auth_url}` (Google Docs) |
| GET | `/api/connect/google/callback` | `code, state` | redirect back to the app |
| GET | `/api/connect/google` | — | connection status + sync result |
| DELETE | `/api/connect/calendar` · `/api/connect/gdocs` | — | disconnect (revoke + delete tokens) |
| GET | `/api/docs` | — | uploaded documents |
| POST | `/api/docs` | multipart `file` | `{doc:{id,name,kind,sections}}` |
| GET | `/api/docs/{id}` | — | parsed sections |
| POST | `/api/docs/{id}/review` | `{mode:"message"\|"client_ready"}` | `doc_review` card |
| POST | `/api/docs/{id}/coach` | multipart `section`, `audio?` | `{transcript,feedback,question}` |
| DELETE | `/api/docs/{id}/coach` | — | reset rehearsal |
| POST | `/api/meeting/start` | `{doc_id?, topic?}` | client's opening line |
| POST | `/api/meeting/turn` | `{said}` | `{say, gap}` |
| POST | `/api/meeting/end` | — | `{summary}` debrief |
| POST | `/api/reels/{gen_id}/render` | `{format:"mp4"\|"pdf"}` | `{url, seconds, bytes}` |
| GET | `/api/reels/{gen_id}/files/{name}` | `?inline=1` | WAV / MP4 / PDF |
| POST | `/api/feedback` | `{generation_id, signal:"up"\|"down", note?}` | `{style_descriptor}` |
| POST · GET | `/api/material/progress` | `{topic,page,total}` · `?topic=` | progress state |
| GET | `/api/settings/llm` | — | read-only model configuration |
| GET | `/api/health` | `?live=1` | config, problems, per-feature status, live pings |

The user is resolved from the `recal_user` cookie, and the conversation from `recal_conv`.

---

## 17. Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | — | enables Vertex AI + Firestore (unset = mock mode) |
| `GOOGLE_CLOUD_LOCATION` | `global` | Vertex location (Gemini 3.x Pro/TTS are global-only) |
| `GOOGLE_CLOUD_API_KEY` | — | optional Vertex API key (Gemini only; blank = service account) |
| `MOCK` | — | `1` forces canned output |
| `MODEL_LITE` / `MODEL_FAST` / `MODEL_DEEP` | 3.5-flash-lite / 3.8-flash / 3.1-pro-preview | model tiers |
| `MODEL_EMBED` | `gemini-embedding-001` | embeddings (768-d) |
| `MODEL_TTS` / `MODEL_TTS_FALLBACK` | 3.8-flash-tts / 3.8-flash-lite-tts | narration |
| `TTS_VOICE` | `Kore` | prebuilt Gemini voice |
| `TTS_STYLE` | `clear, friendly teaching voice` | delivery style, sent as speech metadata (never spoken) |
| `LOCAL_TTS` | `1` | offline voice fallback |
| `LLM_TIMEOUT_S` | `90` | per-call Gemini deadline |
| `REEL_MIN_SIMILARITY` | `0.6` | cosine floor for "on-topic" chunks |
| `MAX_STUDY_MIN_PER_DAY` | `150` | daily study cap used by the planner |
| `RECAL_MEDIA_BUCKET` | — | Cloud Storage bucket for media |
| `RECAL_ENCRYPTION_KEY` | — | 32-byte base64 key for AES-256-GCM token sealing |
| `RECAL_KMS_KEY` | — | Cloud KMS key (takes precedence over AES) |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | — | OAuth web client |
| `OAUTH_REDIRECT_BASE` | `http://localhost:8000` | public base URL for the OAuth callback |
| `SESSION_SECRET` | `change-me` | session secret |
| `GUARD_ENABLED` / `GUARD_MAX_CHARS` | `1` / `4000` | input guard |
| `RATE_LIMIT_PER_MIN` | `30` | per-user POST limit (`0` = off) |
| `LOG_LEVEL` / `LOG_TO_FILE` / `LOG_DIR` | `INFO` / `1` / `logs/` | logging (`LOG_TO_FILE=0` on the VM) |
| `PROMPTS_FROM_DB` | — | `1` = prompt overrides from Firestore `prompts/{name}` |
| `HTTP_USER_AGENT` | `RecalBot/0.2 (+repo URL)` | outbound fetches (Wikipedia policy) |
| `RENDER_SSAA` | `2` | supersampling for reel frames |

---

## 18. Frontend

React 19 + Vite, served by the backend from `frontend/dist`, from the same origin as `/api`.

| View | Shows |
|---|---|
| Chat (left) | the conversation, live status lines, receipts; quick actions (*Ingest a doc · Explain visually · Plan 4 hours · Urgent meeting · Write me notes · Ask a doubt · I'm tired*) |
| **Canvas** | reels (`ReelPlayer`, MP4/PDF downloads) and study books (`MaterialSlides`, page-by-page with progress) |
| **Week** | `WeekView`: Recal sessions + Google events, mark done or missed |
| **Cards** | every card: plans, diffs, sources, reviews; export to Google Docs |
| **Practice** | `PracticeView`: upload, review, rehearsal coach, `MeetingView` voice meeting |

Rendering helpers: `Markdown` (GFM + KaTeX math) and `Mermaid` (diagrams inside study pages).
`lib/stream.js` parses the SSE protocol.

---

## 19. Deployment architecture

```mermaid
flowchart LR
    J(["Judges / users"]) -->|"HTTPS :443"| CAD
    subgraph VM["Compute Engine VM · Ubuntu 24.04 · e2-small + 2 GB swap"]
        CAD["Caddy<br/>Let's Encrypt TLS<br/>SSE flush, 50 MB uploads"] -->|"127.0.0.1:8000"| UV["uvicorn (nohup)<br/>FastAPI + frontend/dist"]
        CRON["@reboot cron"] -.-> UV
    end
    UV -->|"service account (ADC)"| VX[("Vertex AI · global")]
    UV --> FS[("Firestore (default)")]
    UV --> GCS[("Cloud Storage<br/>media bucket")]
    SM[("Secret Manager<br/>recal-env")] -.->|"pulled by vm_install.sh"| UV
    UV --> GAPI[("Calendar / Docs APIs")]
```

- `deploy/vm_install.sh` is idempotent. Each run:
  - adds swap and installs the packages;
  - pulls `.env` from Secret Manager;
  - builds the frontend, then deletes `node_modules` to save disk;
  - (re)starts the backend with `deploy/start.sh` and configures Caddy;
  - prints the URL.
- **No logs on disk:** app output goes to `/dev/null`, `LOG_TO_FILE=0`, and journald is
  RAM-only (16 MB cap).
- **Redeploy:** `git pull && bash deploy/vm_install.sh`. **Debug:** `bash deploy/start.sh fg`.

The full step-by-step guide is in [deploy/README.md](../deploy/README.md).

---

## 20. Testing and observability

- **`GET /api/health`** returns config, a problems list and a per-feature status (`real`, `mock`,
  `setup` or `broken`) with a note. **`?live=1`** makes one real call each to the lite, fast and
  deep models, embeddings, TTS and web search.
- **Receipts** on every output show the model, thinking level, `verified`, `escalated` and the
  reason, so they're visible in the UI log.
- **End-to-end API suite** (run against real Gemini; it uses its own test user and resets it):

| Check | Result |
|---|---|
| health, frontend, privacy page | ✅ |
| concierge chat | ✅ |
| plan study time | ✅ |
| reel (6 scenes, Gemini TTS, real web sources) | ✅ |
| reel → PDF / MP4 | ✅ ~2 s / ~20 s |
| grounded Q&A | ✅ ~7 s |
| study notes (multi-page + critic) | ✅ |
| doc upload + review | ✅ |
| mock meeting start / turn / end | ✅ |
| reset | ✅ |

**16/16 passed.**

---

## 21. Limitations and roadmap

| Limitation | Next step |
|---|---|
| Guests share one `demo` profile | issue a per-browser `g_<uuid>`; verify Google ID tokens (or Firebase Auth) for sign-in |
| Single process holds OAuth state + graph cache | move OAuth state to Firestore; run on Cloud Run with min instances |
| OAuth app unverified (100-user cap, warning screen) | domain verification + Google OAuth review |
| Reels are 2–3 min end to end on a cold topic | cache sources per topic; stream scenes as they're narrated |
| Calendar is read-only | optional write-back of study sessions to Google Calendar |
