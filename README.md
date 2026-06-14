# SuperScout sentinel — 24/7 HackenProof watcher (laptop-independent)

Runs on **GitHub's servers** on a schedule, polls the public HackenProof program
& audit listings, and sends a **Telegram** alert when a new bounty / audit
contest appears. Works even when your laptop is off.

It scrapes only **public** pages (no HackenProof login). The only secret is your
Telegram bot token, which lives in a GitHub **Secret** — never in the code.

---

## Setup — ~5 minutes, all in the browser (no terminal, no git)

### 1. Create a repository
- Go to <https://github.com/new>
- **Repository name:** `superscout` (any name)
- **Private** ✅ (recommended — keeps the 30-min schedule inside the free
  2000 min/month). *Public also works and gives unlimited minutes if you later
  want a faster (15-min) schedule.*
- Do **NOT** add a README/.gitignore (we bring our own). Click **Create**.

### 2. Upload these files
- On the new empty repo page, click **“uploading an existing file”**.
- Open this `github-deploy` folder in Finder and **drag ALL of its contents**
  into the browser — including the hidden `.github` folder
  (`programs_scout.py`, `programs_scout_watch.py`, `scout_watch_state.json`,
  `.gitignore`, `README.md`, and `.github/workflows/scout.yml`).
- Scroll down, click **Commit changes**.

  > If the `.github` folder won’t drag: open the repo’s **Actions** tab →
  > **“set up a workflow yourself”** → delete the template → paste the contents
  > of `.github/workflows/scout.yml` → commit.

### 3. Add the Telegram token as a Secret
- In the repo: **Settings → Secrets and variables → Actions → New repository secret**
- **Name:** `TELEGRAM_TOKEN`
- **Value:** copy the `token` string from your file
  `~/ton/fuzz/.telegram_config.json` (the long `NNNNNN:AA…` string) and paste it.
- Click **Add secret**.
  *(Your chat_id `478594467` is already baked into the workflow — not a secret.)*

### 4. Enable & test
- Open the **Actions** tab. If prompted, click **“I understand… enable workflows”**.
- Click **scout-sentinel** → **Run workflow** (manual trigger) to test it now.
- A green check = it ran. From then on it runs every 30 min automatically.

### 5. (Optional) Confirm Telegram
- The first run finds nothing new (baseline already has 131 programs) → no message.
- To prove the pipe works, temporarily edit `scout_watch_state.json` in the repo
  (delete a few entries) and run the workflow — you’ll get a Telegram alert for
  the “new” ones. Then revert.

---

## Notes
- **Timing:** GitHub cron is best-effort (may drift 5–20 min). For ~2-week audit
  windows this is irrelevant. To go faster, make the repo public and change the
  cron in `scout.yml` to `*/15 * * * *`.
- **No duplicate alerts:** the baseline (`scout_watch_state.json`) is committed
  back after each run, so a program is alerted once.
- **Your laptop’s local watcher** (launchd jobs) can stay on for faster 15-min
  detection when the laptop is awake; this GitHub copy is the always-on backup.
  If you’d rather avoid the occasional double alert, disable the local
  `com.ton.scoutsentinel` job and rely on GitHub only.
- **Cost:** $0. Private repo = 2000 free Action-minutes/month; a 30-min sentinel
  uses ~1400. Public repo = unlimited.
