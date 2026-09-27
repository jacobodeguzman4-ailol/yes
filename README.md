# PAGASA NCR-PRSD Advisory Watcher

Pings a Discord channel the moment DOST-PAGASA's NCR-PRSD office issues a new:

- Rainfall Advisory
- Heavy Rainfall Warning
- Thunderstorm Advisory / Thunderstorm Watch

**Why this watches PAGASA's own site instead of scraping Facebook directly:**
Facebook actively blocks/rate-limits automated scraping and requires either
login credentials or a Meta app-review process to read another page's posts
via the official API — both fragile and slow for a personal project.
PAGASA's own regional forecast page publishes the *exact same bulletin text*,
at the same time, as a stable public webpage:

`https://pagasa.dost.gov.ph/regional-forecast/ncrprsd`

(PAGASA's own legend page confirms these bulletins are "disseminated via SMS,
Social Media, and website" together — so this is the same information,
just from a source that's actually reliable to poll.)

Note: "NCR-PRSD" is PAGASA's forecast division name, but its forecast area
includes nearby provinces (Bataan, Batangas, Bulacan, Cavite, Laguna, Nueva
Ecija, Pampanga, Quezon, Rizal, Tarlac, Zambales) in addition to Metro Manila
— advisories will mention whichever of these areas are actually affected.

## How it works

1. A GitHub Actions workflow runs `watch_pagasa.py` on a schedule (every ~10
   minutes), for free, in GitHub's cloud — no need to keep your PC on.
2. The script fetches the PAGASA page, extracts every advisory currently
   listed, and compares them against `state.json` (which advisories it's
   already alerted on).
3. Any advisory it hasn't seen before gets rendered as a clean image card
   (`render_card.py`, using Pillow) and posted to your Discord channel via
   a webhook, with the advisory recorded in `state.json` so it won't be
   posted again.
4. If the advisory names any of NCR's 17 cities/municipalities specifically
   (Caloocan, Marikina, Quezon City, etc.), the Discord message gets a bold
   callout listing exactly which ones — e.g. "NCR AREAS AFFECTED: Marikina,
   Quezon City". If it only mentions NCR/Metro Manila generically with no
   specific breakdown, it gets a general "METRO MANILA / NCR may be
   affected" callout instead.
5. The workflow commits the updated `state.json` back to the repo after each
   run, so state persists between runs.

### About the image

The image has two parts: the info card (colored header, heading, issued
time, full advisory text) **plus a real geographic map** underneath it —
NCR and the ~12 surrounding provinces PAGASA's NCR-PRSD regularly names,
with actual province boundaries, color-coded pink/purple/orange to match
which areas the advisory is currently **EXPECTING** vs. **AFFECTING**
conditions in (or a single "affected" color for advisories like Thunderstorm
Watch that don't split into those two sections).

**Where the map data comes from:** [geoBoundaries](https://www.geoboundaries.org)
(CC-BY 4.0), an open, actively-maintained administrative boundaries database
built by William & Mary's geoLab — not PAGASA and not Facebook. An earlier
version of this project tried a free Highcharts-derived boundary file, which
turned out to be missing a few provinces PAGASA mentions constantly (Tarlac,
notably); geoBoundaries is a complete, authoritative dataset, so every
province is present. `real_map.py` fetches it once and caches the result to
`data/ph_provinces.geojson`, which the workflow commits back to the repo —
so it's one live fetch ever, not one per run.

**Automatic fallback:** if that fetch ever fails for any reason (a network
hiccup, geoBoundaries changing their API), `render_card.py` catches it and
falls back to a simplified schematic tile-grid map instead of failing to
send the alert at all — you'd still get notified, just with a plainer map
that one time, and the Actions log will say why.

**One honest limitation:** I built and unit-tested the projection, coloring,
and rendering logic thoroughly using a synthetic stand-in dataset, but I
can't make outbound network calls to geoBoundaries.org myself to verify the
*live* fetch end-to-end before handing this to you — GitHub Actions runners
have full internet access and should reach it fine, but the very first real
run is the true test. If the log says "Real map unavailable" on that first
run, tell me the exact error it prints and I'll adjust `real_map.py`
accordingly (most likely culprit would be geoBoundaries using a slightly
different field name than what the code expects — an easy fix once I know
which).

## Setup (~5 minutes)

### 1. Create a Discord webhook
In your Discord server: **Server Settings → Integrations → Webhooks → New
Webhook**. Pick the channel you want alerts in, then copy the webhook URL.

### 2. Create a GitHub repo
Create a new repository (public or private both work fine) and upload all
the files in this project, **keeping the folder structure** — in particular
`.github/workflows/watch.yml` must stay at that exact path.

### 3. Add the webhook URL as a secret
In the repo: **Settings → Secrets and variables → Actions → New repository
secret**.
- Name: `DISCORD_WEBHOOK_URL`
- Value: the webhook URL from step 1

### 4. Run it once manually to initialize
Go to the **Actions** tab → "Watch PAGASA NCR-PRSD Advisories" → **Run
workflow**. This first run just records whatever advisories are currently
active as "already seen" (so you don't get dumped a wall of old advisories)
— it won't post anything to Discord yet.

### 5. Done
From here it runs automatically every 10 minutes and will post to Discord
whenever a genuinely new advisory appears.

## Adjusting it

- **Check frequency**: edit the `cron` line in `.github/workflows/watch.yml`
  (e.g. `*/5 * * * *` for every 5 minutes — GitHub's practical minimum).
- **Which advisory types trigger alerts**: edit the `KEYWORDS` list at the
  top of `watch_pagasa.py`.
- **A different PAGASA region** (Northern Luzon, Southern Luzon, Visayas,
  Mindanao): change `SOURCE_URL` to the matching page, e.g.
  `https://pagasa.dost.gov.ph/regional-forecast/slprsd`.

## Forcing a test message (no waiting for real conditions)

Both scripts support `--test`, which sends one message to Discord immediately
— using whatever's currently live if there's real content available, or a
clearly-labeled synthetic sample otherwise — **without touching state.json**,
so it has zero effect on real duplicate-detection.

**From GitHub (no local setup needed):**
Actions tab → pick the workflow ("Watch PAGASA NCR-PRSD Advisories" or
"Watch QC Class/Work Suspension Announcements") → **Run workflow** → check
the **"Send a test message..."** box → **Run workflow**. Check Discord after
it finishes (usually under a minute).

**Locally**, if you have Python set up:
```bash
pip install -r requirements.txt
DISCORD_WEBHOOK_URL="your-webhook-url-here" python watch_pagasa.py --test
DISCORD_WEBHOOK_URL="your-webhook-url-here" python watch_qc_suspension.py --test
```

Every test message is prefixed with 🧪 so it's unmistakably not a real alert.

## Optimization pass (reliability + speed)

A later pass went through every file specifically looking for inefficiencies
and failure points, not new features:

- **Shared retrying HTTP session (`net_utils.py`).** All three network calls
  in the project (PAGASA's page, QC's feed + og:image, geoBoundaries) used to
  each open their own one-shot `requests.get()` with no retry — a single
  transient blip (common on free CI runners) meant a hard-failed run. They
  now share one `requests.Session` with automatic retry + backoff on
  connection errors, timeouts, and 429/500/502/503/504 responses.
- **Corrupted-cache recovery.** If `data/ph_provinces.geojson` ever ended up
  empty or malformed (an interrupted write, a bad commit), the real map would
  have silently fallen back to the schematic grid *forever*, since a broken
  cache was never distinguished from a missing one. It's now validated on
  read and automatically re-fetched if it's ever unreadable or empty.
- **Font-loading cache in `render_card.py`.** Every piece of text drawn was
  re-reading the same TTF file from disk at the same size. Trivial per-call
  cost, but it happens dozens of times per image — now cached.
- **Workflow concurrency guard.** If a scheduled run and a manual test run
  (or two scheduled runs, if one ever ran long) overlapped, both could reach
  the final `git push` at the same time — one would win, the other would
  fail with a rejected, non-fast-forward push. Both workflows now declare a
  `concurrency` group so overlapping runs queue instead of racing, plus a
  belt-and-suspenders retry (`git pull --rebase` + push again) in the commit
  step in case anything else ever touches the branch mid-run.
- **pip dependency caching.** Both workflows now cache pip downloads between
  runs (`actions/setup-python`'s built-in `cache: pip`), so the "Install
  dependencies" step is near-instant instead of re-downloading the same
  wheels every single run.

## A known GitHub quirk

GitHub automatically disables *scheduled* workflows in a repo that's had no
other activity (commits, etc.) for 60 days. If alerts seem to stop, check the
Actions tab — there's a one-click "Enable workflow" button if this happens.

## Fixed: a bug that caused total silence

An earlier version of `watch_pagasa.py` required the "#NCR_PRSD" hashtag to
appear on the *exact same line* as the advisory heading text, after the page
is flattened to plain text. If PAGASA's page markup puts that hashtag in a
separate element from the heading (very plausible — it's a common CMS
pattern), it lands on its own line once flattened, and the old check would
never find a single advisory — meaning every run silently reported "no new
advisories," forever, with no errors.

The parser no longer requires that same-line match. It now recognizes a
heading purely by advisory-type keywords (excluding negation sentences like
"there is no Heavy Rainfall Warning issued"), and separately folds in a
standalone hashtag line if it finds one nearby. The script also now prints
exactly how many advisories it found on every run, so if something's still
off, the Actions log will show it immediately instead of just silence.

---

## Bonus: QC class/work suspension watcher

`watch_qc_suspension.py` + `.github/workflows/watch-qc.yml` is a second,
independent watcher: it checks the Quezon City government's official site
(quezoncity.gov.ph) for new "Walang Pasok" (class/work suspension)
announcements and posts them to the same Discord webhook — **including the
actual official graphic QC Gov posts**, fetched directly from their site.

This one didn't need a workaround like the PAGASA card image did: unlike
PAGASA's map, QC Gov's site is a plain WordPress site. It publishes a
standard RSS feed (`quezoncity.gov.ph/feed/`) and every announcement post
embeds its real graphic in a normal `og:image` tag — so the script just
downloads that same image and re-posts it, no reconstruction needed.

It runs the same way as the PAGASA watcher (its own schedule, its own state
file `qc_state.json`, same setup — no extra secrets needed, it reuses
`DISCORD_WEBHOOK_URL`). Run its workflow once manually the same way to
initialize it before leaving it on autopilot.

You can sanity-check what it *should* be catching by browsing
[quezoncity.gov.ph/announcements/](https://quezoncity.gov.ph/announcements/)
directly — every "Walang Pasok" post shows up there.

A caveat: this depends on QC Gov's default WordPress RSS feed staying
enabled at `/feed/`. If it's ever disabled (some government sites lock this
down), the script's requests would start failing — let me know if that
happens and I can switch it to scraping the `/announcements/` page instead.
