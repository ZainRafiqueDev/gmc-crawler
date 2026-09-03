# Deployment Guide — Free Tier

Live-checked 2026-09-03 against current provider docs/pricing pages (not memory) — sources
at the bottom of each section. Three pieces to deploy:

| Piece | What it is | Free host used here |
|---|---|---|
| Frontend | Next.js 16 (`frontend/`) | Vercel — Hobby plan |
| Backend | FastAPI + Playwright + APScheduler (`app/api/main.py`) | Render — free Web Service (Docker) |
| Database | Postgres (audit history, store registry, LLM cache, policy RAG chunks) | Neon — free serverless Postgres |

**Read "The one thing free tiers can't do" below before you start** — it affects whether
you can rely on scheduled store monitoring, not just where things run.

---

## The one thing free tiers can't do

This app has two very different workloads:

1. **On-demand audits** — a user clicks "Run Audit," the backend crawls the store and
   returns a report a few minutes later. Fine on a free tier.
2. **Scheduled monitoring** (`app/scheduling.py`'s `APSchedulerBackend`, driving
   `interval`/`on_change` re-audits) — an in-process scheduler that must be *running
   continuously* to fire jobs on time.

Render's free Web Service spins down after 15 minutes with no inbound HTTP traffic, and
cold-starts on the next request (30-60s). While it's asleep, `AsyncIOScheduler` isn't
running, so a store scheduled for e.g. daily re-audits will only actually re-audit the
next time *something* happens to wake the dyno (a manual visit, or a ping — see below).
This is the real constraint of the two: Render fully stops running your code while
asleep, not just the database.

Neon's free Postgres project is a smaller version of the same idea, but recovers on its
own: its compute suspends after **5 minutes with no active connection** (not a multi-day
pause like some competitors), and auto-resumes on the next query with a roughly
sub-second-to-a-few-second cold start — no manual "unpause" step, no separate wake
mechanism needed for the database specifically. In practice, once Render's dyno is awake
(see below) and makes a query, Neon wakes itself.

Neither behavior is a bug in this app — it's the tradeoff of "free." Two ways to live
with it, pick one:

- **Accept it for a demo/personal-use deployment.** Register stores in `interval` mode
  anyway; treat the "next re-audit due" time as best-effort, not a guarantee, and expect
  a burst of overdue re-audits to fire whenever the service happens to wake up. Ping the
  backend's `/health`-equivalent (or any GET route) every ~10 minutes from a free
  uptime monitor (e.g. UptimeRobot, cron-job.org — both have free tiers) to keep Render
  awake most of the time (Neon will just follow along, waking on the next query Render's
  own activity generates). This is the practical way to get "mostly always-on" out of a
  free tier without paying.
- **Only use on-demand audits.** Don't register stores for scheduled monitoring at all;
  every audit is triggered manually via the frontend's "Run Audit" button. No scheduler
  reliability problem exists if nothing is ever scheduled.

If you need real scheduling guarantees, Render's cheapest **paid** Starter instance
($7/mo) removes the sleep behavior entirely — worth knowing as the upgrade path, not
something to build for now.

---

## 1. Database — Neon (free)

1. Create a project at neon.tech (free tier: up to 100 projects, 0.5GB storage per
   project, 100 compute-hours/month per project, no credit card required). One project
   is plenty for this app.
2. This app does **not** need the pgvector extension enabled — `app/llm/policy_rag.py`
   stores embeddings as a plain JSON float array and does cosine similarity in Python
   (see the comment at `app/db.py:144`), specifically so a bare Postgres is enough. Skip
   any "enable pgvector" step you see in generic Neon/Postgres guides.
3. From the Neon dashboard's Connection Details, copy the pooled connection string and
   convert it to the async form this app needs:
   ```
   postgresql+asyncpg://<user>:<password>@<project>-pooler.<region>.neon.tech/<dbname>
   ```
   (Neon gives you `postgresql://...` — just add `+asyncpg` after `postgresql`. Use the
   *pooled* connection string, not the direct one — this app opens a normal connection
   pool via SQLAlchemy, and pooling on Neon's side avoids exhausting its own per-project
   connection limit under concurrent audits.)
4. Tables are created automatically on backend startup (`app/db.py`'s
   `Base.metadata.create_all` — see `init_db`), so no manual migration step.

Source: [Neon free tier limits, 2026](https://neon.com/faqs/managed-postgres-databases-free-tier).

## 2. Backend — Render (free Web Service, Docker)

The repo now has a `Dockerfile` (root) that starts from Playwright's own maintained
image (`mcr.microsoft.com/playwright/python`) so Chromium's system dependencies are
already present — Render's native Python buildpack does **not** include what Playwright
needs, so Docker is the only realistic free path here.

1. Push this repo to GitHub (Render deploys from a repo, not a local upload).
2. In Render: New → Web Service → connect the repo → Environment: **Docker** (it will
   auto-detect the root `Dockerfile`) → Instance Type: **Free**.
3. Set environment variables (Render dashboard → Environment), from `.env.example`:
   ```
   LLM_PROVIDER=openai
   OPENAI_API_KEY=<your key>
   OPENAI_MODEL=gpt-4o-mini
   OPENAI_EMBEDDING_MODEL=text-embedding-3-small
   DATABASE_URL=postgresql+asyncpg://<user>:<password>@<project>-pooler.<region>.neon.tech/<dbname>
   API_CORS_ORIGIN=https://<your-vercel-app>.vercel.app
   CRAWL_MAX_PAGES=150
   ```
   `LLM_PROVIDER=openai` + `gpt-4o-mini` is the deliberate choice here, not the repo's
   own default (`claude` / `claude-sonnet-4-5`): gpt-4o-mini is roughly an order of
   magnitude cheaper per call, and you need `OPENAI_API_KEY` set regardless (the policy
   RAG index always uses OpenAI embeddings — see `.env.example`'s note), so standardizing
   on OpenAI for a cost-sensitive free-tier deployment avoids paying for two providers.
   Neither Anthropic nor OpenAI has a free API tier — budget a few dollars of prepaid
   balance; this project's own measured cost is well under $0.05 per audit at this
   model.
4. Deploy. First build installs Chromium inside the image (a few extra minutes vs. a
   plain Python build) — subsequent deploys are faster via layer caching.
5. Your backend URL will be `https://<service-name>.onrender.com`. Playwright's browser
   launch happens in the FastAPI `lifespan` (`app/api/main.py`) — no extra Render config
   needed for that part.

Sources: [Render free tier behavior, 2026](https://render.com/articles/platforms-with-a-real-free-tier-for-developers-in-2026), [Render Docker support](https://kuberns.com/blogs/render-backend-deployment/).

## 3. Frontend — Vercel (free Hobby plan)

1. Push the repo (frontend lives in `frontend/`) to GitHub, if not already.
2. In Vercel: New Project → import the repo → set **Root Directory** to `frontend`.
3. Environment variable:
   ```
   NEXT_PUBLIC_API_BASE_URL=https://<service-name>.onrender.com
   ```
   (`frontend/lib/api.ts` reads this; without it, the app falls back to
   `http://localhost:8010`, which won't work once deployed.)
4. Deploy. Vercel Hobby is free for 100GB bandwidth/1M function invocations/month, but
   its terms restrict it to **personal, non-commercial use** — fine for a demo or your
   own monitoring, not for reselling this as a paid product without upgrading to Pro
   ($20/mo).

Source: [Vercel Hobby plan limits, 2026](https://deploywise.dev/blog/vercel-free-tier-limits-2026).

## 4. Wire CORS both ways

Two env vars have to point at each other's *actual deployed URLs*, not localhost:

- Backend's `API_CORS_ORIGIN` → your Vercel URL.
- Frontend's `NEXT_PUBLIC_API_BASE_URL` → your Render URL.

Redeploy whichever side you change second — Next.js bakes `NEXT_PUBLIC_*` vars in at
build time, so changing it in Vercel's dashboard alone doesn't take effect until the
next build/redeploy.

## 5. What this stack costs, in practice

| Piece | Monthly cost | Real constraint |
|---|---|---|
| Vercel Hobby | $0 | personal/non-commercial use only |
| Render free Web Service | $0 | sleeps after 15 min idle; cold start 30-60s |
| Neon free Postgres | $0 | compute suspends after 5 min idle (auto-resumes on query); 0.5GB storage, 100 compute-hours/month cap |
| OpenAI API (gpt-4o-mini + embeddings) | pay-as-you-go, no free tier | ~$0.01-0.05 per audit at current pricing (see below) |

Rejected as not free: **Railway** — no permanent free tier as of 2026 (30-day $5 trial,
then $1/month credit, not enough for an always-on Playwright container); worth knowing
if you outgrow Render's free tier and want a paid alternative later
([source](https://kuberns.com/blogs/railway-free-tier/)).

gpt-4o-mini pricing referenced above: $0.150/1M input tokens, $0.600/1M output tokens,
$0.075/1M cached input tokens (OpenAI, current as researched this session).
