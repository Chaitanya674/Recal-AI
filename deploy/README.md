# Deploying Recal for judging

Recal runs as one Compute Engine VM: Caddy (HTTPS) → uvicorn (FastAPI + built React
frontend) → Vertex AI Gemini, Firestore and Cloud Storage, all authenticated as the VM's
service account. There are no API keys anywhere.

| Tier | Model | Used for |
|------|-------|----------|
| lite | `gemini-3.5-flash-lite`, temperature 0 | intent routing, input guard, style tagging |
| fast | `gemini-3.8-flash` | chat, reels, study pages, doc review, ADK concierge |
| deep | `gemini-3.1-pro-preview` | planning, reel visual direction, retries after a failed check |
| tts | `gemini-3.8-flash-tts` (fallback `gemini-3.8-flash-lite-tts`) | reel narration |
| embed | `gemini-embedding-001` (768 dims) | knowledge graph + style memory vectors |

All of these are served from Vertex location `global`. Override any of them in `.env`.

---

## 1. Google Cloud project (once)

- [ ] **Billing:** confirm the $300 credit is on the billing account linked to
      `ai-builder-cup-9a0a1`. Set a budget alert (Billing → Budgets) at $50 / $150 / $250.
- [ ] **Run the setup script** from Cloud Shell:
      `PROJECT_ID=ai-builder-cup-9a0a1 bash deploy/gcp_setup.sh`
      It does the following:
      - enables the Vertex AI, Firestore, Storage, Compute, Calendar, Docs and Drive APIs;
      - creates the `recal-runtime` service account with `aiplatform.user`, `datastore.user`, `logging.logWriter` and bucket `objectAdmin`;
      - creates the Firestore vector indexes, the media bucket, a static IP, a firewall rule for ports 80/443, and the VM.
- [ ] **Vertex AI API.** Right now every model call fails with
      `403 Agent Platform API has not been used in project … or it is disabled`.
      The script enables it. Allow a few minutes for it to propagate.
- [ ] **Quotas:** IAM & Admin → Quotas, filter on `aiplatform` and the `global` region.
      Preview models (3.1 Pro, the TTS models) have low default RPM. Request an increase
      now if the demo will run several reels back to back.
- [ ] **Local dev (optional):** grant `roles/aiplatform.user` to whichever identity runs
      locally (`gcloud auth application-default login`, or the service account in
      `firebase-key.json`). Then set `GOOGLE_CLOUD_LOCATION=global` in your local `.env`.

## 2. Deploy to the VM

```bash
gcloud compute ssh recal-vm --zone us-central1-a
git clone https://github.com/Chaitanya674/Recall.git recal && cd recal
bash deploy/vm_install.sh              # first run creates .env; fill OAuth id/secret, re-run
```

- [ ] The first run writes a production `.env`. It contains the project id, `global`,
      the bucket, a fresh `SESSION_SECRET` and a fresh `RECAL_ENCRYPTION_KEY`. Copy your
      local `RECAL_ENCRYPTION_KEY` over it if you want existing sealed tokens to stay
      readable. Otherwise users simply reconnect Google.
- [ ] Open `https://<ip-with-dashes>.sslip.io/api/health?live=1`. Every `live` check
      should be `ok: true`, and `tts` should name the Gemini model, not "offline system voice".
- [ ] **Redeploy:** `git pull && bash deploy/vm_install.sh`. **Logs:** `journalctl -u recal -f`.
- [ ] Keep `--workers 1`. OAuth state and the knowledge-graph cache live in-process.

## 3. Firestore: test data → MVP data

- [ ] **Purge test data** before judging. Graphs written in mock mode hold 32-dim hash
      vectors that don't match the 768-dim index. Delete `users/*` and `reels/*` in the
      console, or run `firebase firestore:delete --all-collections`.
- [ ] **Lock client access.** The backend uses server credentials, so browsers never need
      Firestore. Publish these rules (Console → Firestore → Rules):
      ```
      rules_version = '2';
      service cloud.firestore {
        match /databases/{database}/documents {
          match /{document=**} { allow read, write: if false; }
        }
      }
      ```
- [ ] Confirm both vector indexes show **Ready** (Firestore → Indexes). Until then KNN
      falls back to in-memory search, which is slower but still correct.
- [ ] Set up daily backups (Firestore → Disaster recovery). It's cheap, and judging day is
      the wrong day to lose data.

## 4. Google OAuth (Calendar + Docs): from "Testing" to judge-ready

The consent screen is in **Testing**, so only listed test users can sign in. Their
refresh tokens also **expire after 7 days**.

- [ ] **Use a real HTTPS domain.** Google rejects raw-IP redirect URIs. `sslip.io` usually
      works, but Google may refuse it as an authorized domain. A cheap domain with an
      A record pointing at the static IP is the safe choice. Then run
      `DOMAIN=your.domain bash deploy/vm_install.sh`.
- [ ] **Credentials → OAuth client (Web application):**
      - authorized redirect URI: `https://<domain>/api/connect/google/callback`
      - JS origin: `https://<domain>`
- [ ] **Branding:** app name, support email, home page `https://<domain>`, a privacy
      policy URL (a short static page is enough), and the domain under "Authorized domains".
- [ ] **Scopes:** `calendar.readonly` (sensitive), `drive.file` (non-sensitive), `openid`, `email`.
- [ ] **Publishing status.** Choose one:
      - **Recommended:** **Publish app → In production, unverified.** Up to 100 users can
        sign in. They see "Google hasn't verified this app" and must click
        *Advanced → Go to Recal*. Put that sentence in the judge instructions.
      - **Alternative:** stay in Testing and add the judges' Google emails as test users.
        This only works if you know the emails, and the tokens expire after 7 days.
      - **Not feasible:** full verification for `calendar.readonly` needs Search Console
        domain verification plus a demo video, and takes days to weeks.

## 5. Code hardening still open (recommended before judging)

These were left unchanged on purpose. Each changes behaviour, so decide first.

- [ ] **Every guest is the same user `demo`.** `POST /api/auth` always sets
      `recal_user=demo`, so all judges share one chat history, calendar, graph and rate
      limit (30 POST/min total). Fix: issue a random `g_<uuid>` per browser. Keep `demo`
      only if you want judges to land on a pre-seeded demo.
- [ ] **Cookies:** add `secure=True` to `set_cookie` now that the site is HTTPS-only.
- [ ] **Google sign-in is not verified** (`auth_router.py`, `TODO(P5)`). Any string is
      accepted as an `id_token`. Verify it with `google.oauth2.id_token.verify_oauth2_token`,
      or switch to Firebase Auth, before exposing a sign-in button.
- [ ] **Wikipedia returned 403** in testing. Its API requires a descriptive User-Agent
      with contact info, so change `RecalBot/0.1` in `knowledge.py`. Search grounding
      through Gemini still works without it.
- [ ] **ADK agent graph:** `agents/workflow.py` had been overwritten by a copy of
      `agent_factory.py`. It's now a concierge agent with three tools: search your sources,
      read the week, and edit a session. The planner chains in `chat_router` still handle
      plans, reels and material. Restore the original multi-agent graph if you have it
      elsewhere.

## 6. Pre-submission QA

- [ ] In an incognito window on the public URL, run the full demo:
      - a reel (with narration and MP4 download)
      - a study session with material
      - "I'm tired" re-plan
      - doc upload + mock client meeting
      - Calendar connect
      - Docs export
- [ ] Test on a phone (mic permission for rehearsal needs HTTPS, which Caddy provides).
- [ ] `/api/health?live=1` is all green. No `problems` other than ones you accept.
- [ ] Load: two or three tabs generating reels at once. Watch `journalctl` for 429s.
- [ ] **Rotate secrets** that ever sat in a shared or local `.env`: revoke the old
      OpenRouter key and any AI Studio key. Make sure `firebase-key.json` and `.env` are
      never pushed (they're gitignored and not in history today).

## 7. Submission

- [ ] Public URL, repo link (public), a 2–3 min demo video, and a judge guide with:
      - the 3 things to try, with exact prompts
      - the unverified-app click-through for Google connect
      - that it runs on Vertex AI Gemini 3.x + ADK + Firestore vector search + Cloud Storage
- [ ] An architecture diagram: browser → Caddy → FastAPI → Vertex AI (lite/fast/deep/TTS/
      embeddings) + Firestore + GCS.
- [ ] Keep the VM running through judging and freeze deploys. Afterwards stop it
      (`gcloud compute instances stop recal-vm`) and release the static IP so the
      credits last.

**Rough cost:** e2-standard-2 ≈ $1.6/day, static IP ≈ $0.25/day, Firestore and GCS are
cents. Vertex usage depends on demo volume, and Pro and TTS calls dominate. A week of
judging should stay well under $50 of the $300 credit.
