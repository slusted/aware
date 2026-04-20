# Deploying Competitor Watch to Railway

Single-process FastAPI + APScheduler + SQLite. A Railway persistent volume mounted at `/data` keeps the DB and live config across redeploys.

---

## What's already in place

- `Procfile` — Railway reads this to start the web process and run release migrations.
- `.python-version` — pins Python 3.12 for the Nixpacks builder.
- `app/db.py` — respects `DATA_DIR` for the SQLite path.
- `app/main.py` — respects `ENV_PATH` for runtime-set API keys, and seeds `config.json` from the repo onto the volume on first boot.
- `app/auth.py` + `/login` / `/setup` — cookie-based sessions with bcrypt passwords. The session cookie auto-enables `Secure` when served over HTTPS, which Railway provides out of the box.

No further code changes are needed.

---

## One-time setup

### 1. Push the project to GitHub

If the folder isn't a git repo yet:

```bash
cd "C:\Users\slust\OneDrive\Documents\AgentSimon\competitor-watch"
git init
git add .
git commit -m "Initial commit"
gh repo create competitor-watch --private --source=. --push
```

Or create an empty private repo on github.com and push manually.

**Before pushing, confirm `.gitignore` excludes `.env` and `data/`.**

### 2. Create the Railway project

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo** → pick `competitor-watch`.
2. Railway auto-detects Python, reads `Procfile`, starts building. The first build will fail because env vars aren't set yet — that's fine.

### 3. Add a persistent volume

1. In the service, go to **Settings** → **Volumes** → **New Volume**.
2. Mount path: `/data`
3. Size: `1 GB` is plenty.

### 4. Set environment variables

In the service's **Variables** tab, add:

**Paths (tell the app to use the volume):**
```
DATA_DIR=/data
CONFIG_PATH=/data/config.json
ENV_PATH=/data/.env
```

**API keys (paste your real values):**
```
ANTHROPIC_API_KEY=sk-ant-...
TAVILY_API_KEY=tvly-...
SERPER_API_KEY=...
ZENROWS_API_KEY=...
SCRAPINGBEE_API_KEY=...
GMAIL_USER=slusted@gmail.com
GMAIL_APP_PASSWORD=...
```

**Scheduler timezone (optional but recommended — defaults to UTC):**
```
TZ=Australia/Melbourne
```

Set only the keys you actually use — missing optional providers fail quiet.

### 5. Trigger a redeploy

**Deployments** → latest deploy → **Redeploy**. The `release` command (`alembic upgrade head && python scripts/import_state.py`) runs first, creating the DB on the volume and seeding it from `config.json`. Then `uvicorn` starts and the scheduler boots.

### 6. Get the public URL

**Settings** → **Networking** → **Generate Domain**. You'll get something like `competitor-watch-production.up.railway.app`.

---

## Verify it's working

1. **Open the URL** — on first deploy you'll land on `/setup`, which is shown only while no admin account exists. Enter your email, name, and a password (≥ 8 chars). On submit you're signed in and redirected to the dashboard; `/setup` is then closed and all future visitors go through `/login`. Additional accounts are created from **Admin → Users** once you're signed in.
2. **Check logs** (Deployments → View Logs). Expect to see:
   - `seeded /data/config.json from repo config.json` (first deploy only)
   - `Scheduler started`
   - Three jobs registered: `daily_scan`, `reply_check`, `daily_momentum`
3. **Trigger a run manually** from the UI to confirm Anthropic + scrapers + email are all wired.
4. **Next-day check**: confirm the daily scan fires at your configured `scan_hour` (UTC by default, local time if you set `TZ`).

---

## Ongoing ops

- **Rotating an API key** — either update the Railway env var (triggers redeploy) or use the in-app settings page (writes to `/data/.env`, no restart).
- **Editing competitors / filters / providers** — use the UI; all writes persist to `/data/config.json`.
- **Backups** — `app.db` lives at `/data/app.db`. Railway has volume snapshots on paid plans; otherwise periodically download via `railway run cat /data/app.db > backup.db`.
- **Upgrading** — `git push`. Railway auto-redeploys. The release command runs migrations idempotently.

---

## Cost

Hobby plan ($5/mo credit) covers this comfortably:
- ~512 MB RAM idle, ~1 vCPU burst during scans
- 1 GB volume = ~$0.25/mo
- Total: well under $5/mo unless scan frequency spikes.

---

## Things NOT to do

- Don't scale replicas above 1. SQLite + APScheduler is single-process only; two replicas would double-fire the scheduler and corrupt the DB.
- Don't delete the volume if you want to keep history. Redeploys and service deletes are fine; the volume persists until explicitly removed.
