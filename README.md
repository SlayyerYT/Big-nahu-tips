# Altcoin Long/Short Discord Bot

Posts a top-20 altcoin long/short table to a Discord channel every 10 minutes.

```
COIN   LONG   L/S    chg  SIZE
------------------------------
ETH    69.1  2.23  -0.02  0.97 🟢→
XRP    74.1  2.86  -0.13  0.90 🟢↘
SOL    70.9  2.43  -0.03  0.88 🟢→
------------------------------
18/20 long | avg 65.4%
17/19 disagree by size
```

| Column | Meaning |
| --- | --- |
| `LONG` | Share of accounts net long, in percent (short is the remainder) |
| `L/S` | Long accounts per short account |
| `chg` | Change in the long share since the previous post, in percentage points |
| `SIZE` | Long/short weighted by **position size**, OKX **top traders only** (the `wide` layout adds `1h` and `24h`) |
| 🟢 / 🔴 / ⚪ | Crowd is long (≥55%), short (≤45%), or balanced |
| ↗ / ↘ / → | Longs building, unwinding, or flat since the last post |

## Setup

1. **Install**

   ```bash
   pip install -r requirements.txt
   ```

2. **Create the Discord bot**

   - <https://discord.com/developers/applications> → *New Application*
   - **Bot** tab → *Reset Token* → copy the token
   - **OAuth2 → URL Generator**: scope `bot`, permissions *View Channel*,
     *Send Messages*, *Embed Links*. Open the generated URL and invite the bot.
   - No privileged intents are needed — Message Content is only required for
     *reading* messages, and this bot only posts.

3. **Get the channel ID**

   Discord → Settings → Advanced → **Developer Mode** on, then right-click the
   target channel → *Copy Channel ID*.

4. **Get a Coinalyze API key** (optional, recommended)

   Sign in at <https://coinalyze.net/> → API page → generate a key. The key is a
   string; `https://api.coinalyze.net/v1/doc/` is the documentation, not a key.
   Without a key the bot runs on Binance alone.

5. **Configure**

   ```bash
   cp .env.example .env
   ```

   Fill in `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, and optionally
   `COINALYZE_API_KEY`.

6. **Run**

   Double-click **`start-bot.bat`**, or from a terminal:

   ```bash
   python bot.py
   ```

   The bot only runs while that window is open. See *Starting it automatically*
   below to have Windows launch it at login.

## Checking it without Discord

`--dry-run` prints one table to stdout using live market data. It needs no
token and makes no Discord connection:

```bash
python bot.py --dry-run                    # whatever source is configured
python bot.py --dry-run --source binance   # force the backup
python bot.py --dry-run --source coinalyze # verify a new API key (strict)
```

`--source coinalyze` is **strict**: it fails loudly and exits non-zero instead of
falling back. Use it right after pasting a new key. Without it, a broken key or a
failed symbol lookup just falls back to Binance forever, and the bot looks
healthy while never once using the primary source. (In normal operation that
fallback is logged at WARNING, escalating to ERROR after three consecutive
failures, and the footer says `BACKUP SOURCE`.)

## Layout: mobile vs wide

Discord code blocks do not wrap - anything wider than the screen scrolls
sideways, which is unusable on a phone. Two layouts, set with `LAYOUT` in `.env`:

- **`mobile`** (default, 30 chars) - fits a phone screen.
- **`wide`** (51 chars) - adds an explicit `SHORT` column plus `1h` and `24h`.
  Fine on desktop, scrolls sideways on a phone.

`mobile` gives up very little: `SHORT` is always `100 - LONG`, and at a
10-minute cadence `1h` barely differs from the current value. `24h` went when
`SIZE` arrived - with no room for both, a second opinion on the present beats a
third reading of the past, and `chg` still carries the short-term move.

Compare them before choosing:

```bash
python bot.py --dry-run --source binance
```

## Data sources

**Primary — Coinalyze** (needs a free key). Coinalyze symbols are per-market,
so each coin is resolved to its perp on Binance, Bybit and OKX and the long
share is averaged across them, approximating the aggregate figure the Coinalyze
site shows. A weighted mean would need per-venue account counts, which no public
API exposes.

Quota is 40 calls/minute and **each symbol counts as one call**. 20 coins × 3
venues = 60 symbols per cycle = 6 calls/minute at a 10-minute cadence.

**Backup — Binance** (no key). Uses `globalLongShortAccountRatio`, the share of
all Binance futures *accounts* net long. Binance also publishes
`topLongShortAccountRatio` and `topLongShortPositionRatio`, which are different
metrics — swapping them changes what the bot reports.

Because the backup is one venue and the primary is a three-venue mean, the
levels shift by a point or so on failover. The footer says which source produced
the table and flags when the backup is in use, so the shift isn't misread as a
market move.


**Size column - OKX** (no key). `SIZE` is the only column that does not count
accounts. It comes from OKX's top-trader **position** ratio: long notional per
short notional, among OKX's top-trader cohort.

It routinely disagrees with the account columns, and that is the point of having
it - on 2026-09-11 the table read 18/20 coins crowd-long while 17 of 19 were net
short by size. Two caveats belong with any reading of it:

- **Different population, not just different weighting.** It covers a cohort of
  large traders, not everybody, so it is not comparable to `L/S` in level - only
  in direction. The legend under the table says so on every post.
- **A whole-market version would be meaningless.** On a perpetual, total long
  notional always equals total short notional, so the size ratio across all
  traders is identically 1.00. The signal only exists inside a subset.

OKX is the only venue that can serve this in CI: Coinalyze has no position-ratio
endpoint, and Binance and Bybit both refuse GitHub's US-based runners (measured,
not assumed). There is no fallback, so the column is best-effort - if OKX fails
or is missing a coin, that cell prints `-`, the footer stops crediting OKX, and
the rest of the table posts unchanged.

**Coin list — CoinGecko**, refreshed daily and cached to `cache/universe.json`.
BTC, stablecoins and wrapped/staked derivatives are excluded. The list is walked
in market-cap order until 20 coins with a futures market are found; a CoinGecko
failure falls back to the cached list, then to a hardcoded seed.

## Design notes

- **Every time column comes from one fetch.** A single 25-hour history request
  contains the current bar plus the ones 10 minutes, 1 hour and 24 hours back.
  Splitting this into four requests would quadruple quota use for nothing.
- **`chg` is derived from that same series, not from a saved previous post.**
  There is no state file to corrupt, no wrong value on a cold start, and the
  comparison can never straddle a source change.
- **Timestamps are matched, not indexed.** Points are found by proximity to a
  target time and rejected if too far off, so one missing bar at a venue cannot
  silently shift every column by five minutes.
- **A symbol with no data returns an empty history rather than an error**, so
  empty series are treated as missing and the coin is skipped.

## Status

The Binance path and the formatting are verified against live data. **The
Coinalyze path is code-complete but has never been executed** — it needs a key,
so the first run with one is also its first test. Run
`python bot.py --dry-run --source coinalyze` first; if the venue matching or the
market filter needs adjusting, that command says so instead of hiding it behind
a fallback.

## Tuning

`.env` accepts `POST_INTERVAL_MINUTES` (default 10), `TOP_N` (default 20) and
`LAYOUT` (`mobile` or `wide`).
`chg` always compares against one interval back, so changing the interval keeps
the column meaningful.

The coin list is strictly market-cap ranked, so newer top-100 coins (recently
`CC`, `GRAM`) can appear alongside the majors. If you want only recognisable
names, add their CoinGecko ids to `EXCLUDED_IDS` in `universe.py` — no code
change needed.

Thresholds live at the top of `formatter.py`: `LONG_BIAS_PCT`, `SHORT_BIAS_PCT`
and `TREND_EPS_PP`. Venue selection is `PREFERRED_EXCHANGES` in
`sources/coinalyze.py`.

## Running it with no host at all (GitHub Actions + webhook)

The simplest and most reliable way to run this. A scheduled GitHub Action runs
`post_once.py` every 10 minutes and posts through a Discord **webhook**. There is
no server to keep online, no bot token, no gateway connection, and nothing to
renew.

This works only because the bot stores nothing between runs: `chg`, `1h`, `24h`
and `SIZE` all come from the market history fetched in that run, so a cold start
produces exactly the table a long-running process would.

### Setup

1. **Create the Discord webhook.** In Discord: right-click the target channel →
   *Edit Channel* → *Integrations* → *Webhooks* → *New Webhook*. Name it, then
   **Copy Webhook URL**. That URL is a credential - treat it like a password.

2. **Create a GitHub repository** and upload everything except `.env`
   (`.gitignore` already excludes it). Make the repo **public**: Actions minutes
   are unlimited on public repos, while a private repo gets 2,000 minutes/month
   and a 10-minute cadence needs roughly 2,200. No secrets live in the code, so
   public is safe here.

3. **Add the secret.** Repo → *Settings* → *Secrets and variables* → *Actions* →
   *New repository secret*:

   - Name: `DISCORD_WEBHOOK_URL`, value: the webhook URL
   - Optionally `COINALYZE_API_KEY`

4. **Run it once by hand** to check the setup: repo → *Actions* → *Post altcoin
   long/short table* → *Run workflow*.

5. **Set up the external trigger** (see below). GitHub's own scheduler is not
   reliable enough for a 10-minute cadence.

### Why an external trigger

GitHub puts `schedule:` runs on a low-priority queue and states they may be
delayed during high load; on a brand-new repository this meant **zero** runs in
half an hour, while every manual run started within seconds. Their docs also
recommend no more frequent than 15 minutes on free public repos.

`workflow_dispatch` runs are not throttled, so a free external cron service
calls the dispatch API instead:

- **URL** `https://api.github.com/repos/<owner>/<repo>/actions/workflows/post.yml/dispatches`
- **Method** POST
- **Headers**
  - `Authorization: Bearer <token>`
  - `Accept: application/vnd.github+json`
  - `X-GitHub-Api-Version: 2022-11-28`
- **Body** `{"ref": "main"}`
- **Schedule** every 10 minutes

The token is a fine-grained personal access token scoped to this repository
alone, with *Actions: Read and write*. A successful call returns HTTP 204 with
an empty body.

`schedule:` is intentionally absent from the workflow: if a throttled run fired
later it would post a duplicate table.

### Binance is geo-blocked on GitHub runners

**A Coinalyze API key is required for the Actions path.** GitHub's runners are
US-based and Binance answers its futures API with HTTP 451 ("Unavailable For
Legal Reasons") from there. Bybit and OKX restrict US traffic too, so no
exchange-direct fallback fixes this.

Coinalyze is a data aggregator rather than an exchange and is not subject to
those restrictions, so it works from the runner. Add the key as a second
repository secret named `COINALYZE_API_KEY` - the workflow already passes it
through.

Running locally from an unrestricted region, Binance works fine and no key is
needed.

### Notes

- **Scheduled runs can be delayed** by a few minutes when GitHub's queue is busy.
  The table is timestamped from the market data rather than the trigger, so a
  late run is still correct.
- **GitHub disables schedules after 60 days of repo inactivity** and emails you.
  Any commit re-enables it.
- **The coin list is cached between runs** by the workflow, so CoinGecko is hit
  about once a day instead of 144 times.
- Test the whole pipeline without sending anything:

  ```bash
  python post_once.py --dry-run
  ```

## Weekly macro calendar (Sunday 08:00)

A second, separate post: the high-impact US events scheduled for the coming week,
so a 70%-long book is not held into a CPI print by accident.

```
Sun 13 Sep - Sun 20 Sep

__Tue 15 Sep__
`14:30` **CPI m/m**  - F 0.3% . P 0.2%
`14:30` **Core CPI m/m**  - F 0.3% . P 0.3%

__Wed 16 Sep__
`20:00` **FOMC Statement**
```

Run it by hand, or check it without posting:

```bash
python post_calendar.py --dry-run
```

**Source.** ForexFactory's free JSON feed (no key). Filtered to `impact: High`
for `USD`, plus US bank holidays - a closed session changes how the week trades.
Widening it to EUR or GBP is one line: `WATCHED_COUNTRIES` in
`sources/econ_calendar.py`.

**Two traps worth knowing**, both handled in code:

- **The feed rate-limits hard, and answers 429 with an HTML page.** A handful of
  requests in a few minutes is enough. Decoding that body as JSON gives a parse
  error that looks like a bug in the parser, so the status is checked first and a
  429 is retried with backoff. Once a week is far inside the limit, but GitHub
  runners share outbound IPs, so another job can spend the budget first.
- **There is no "next week" feed** - `ff_calendar_nextweek.json` 404s, only
  `thisweek` exists, and it is generated per request with no `Last-Modified`, so
  there is no way to prove from outside exactly when it rolls over. The code
  therefore never trusts the label: it filters by timestamp against the window it
  was asked for. If every event in the feed is in the past, the run **fails and
  posts nothing** rather than posting an empty week that would read as "quiet
  week ahead" - the one wrong answer that would actually cost money.

### Scheduling it

Same external-cron pattern as the table, with its own workflow and its own
cron-job.org entry:

- **URL** `https://api.github.com/repos/<owner>/<repo>/actions/workflows/calendar.yml/dispatches`
- **Method** POST, body `{"ref":"main"}`, same headers as the table's trigger
- **Schedule** Sunday 08:00, with the job's timezone set to **Europe/Vienna**

Set the timezone on the cron-job.org entry rather than writing a UTC cron
expression: GitHub's own `schedule:` is UTC-only, so a fixed expression would
drift an hour twice a year and post at 07:00 or 09:00 local across DST.

## Running it as an always-on bot process (alternative)

### Hosting on a panel (Pella and similar)

Running on a host means it posts 24/7 without your PC being on. `main.py` is the
entry point most panels expect; `python bot.py` is equivalent if you can set a
start command.

Upload **`altcoin-ls-bot-upload.zip`** — it contains the code, `requirements.txt`
and `.env.example`, and deliberately **excludes `.env`** so your token is never
inside a file you might share.

Then, in the panel:

1. **Install dependencies** — most panels do this from `requirements.txt`
   automatically. If there is a "startup"/"install" command field, use
   `pip install -r requirements.txt`.
2. **Set the start command** to `python main.py` (only if the panel asks; many
   default to `main.py` already).
3. **Set the secrets.** Either is fine:
   - an *Environment Variables* / *Secrets* tab in the panel — add
     `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, and optionally `COINALYZE_API_KEY`; or
   - create a file named `.env` in the panel's file manager, using
     `.env.example` as the template.

   The bot reads panel variables when they exist and falls back to `.env`, so
   whichever the host offers will work.
4. **Start it**, then read the logs. Success looks like:

   ```
   logged in as YourBot#1234; posting every 10 minutes
   posted 20 rows from Binance
   ```

Notes for any free tier:

- **Python 3.10+ is required.** `main.py` exits with a clear message on anything
  older, rather than failing obscurely.
- **The filesystem may be read-only or wiped on restart.** That is fine: the two
  caches are best-effort and the bot refetches when they are missing.
- **A restart loses nothing.** Nothing is stored between cycles - every column,
  including the change since the last post, is derived from the market history
  fetched in that cycle.
- **Free tiers often idle or restart bots.** This one has no web server to keep
  awake; if the host kills it, it simply resumes posting when restarted.

## Starting it automatically at login (local alternative)

The bot runs only while its window is open. To have Windows start it when you
log in:

1. Right-click **`start-bot.bat`** → **Show more options** → **Create shortcut**.
2. Press <kbd>Win</kbd>+<kbd>R</kbd>, type `shell:startup`, press Enter. The
   Startup folder opens.
3. Drag the shortcut into that folder.

It now launches at every login. A console window stays open while it runs —
minimise it; closing it stops the bot. To undo, delete the shortcut from that
folder.

Note that this starts at **login**, not at boot, and the PC must stay awake. If
you want it running when you are not logged in at all, that is a job for Task
Scheduler ("Run whether user is logged on or not") or a cheap VPS.

## Note

144 posts a day is a lot of channel traffic. Give the bot its own channel.
