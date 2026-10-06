# X News Bot

Watches **@Kalshi** and **@Polymarket**, picks the biggest headline, rewrites it with Claude, and posts it to **@ifmarkets**: **14 posts a day**, one about every **1 h 43 min**, around the clock.

## How it works

Every 10 minutes the bot checks both accounts and adds new posts to a queue. When a posting slot comes up, it:

1. **Filters** the queue:
   - drops posts with videos or GIFs
   - drops anything older than 4 hours
   - drops stories that repeat something already posted
2. **Ranks** the whole queue together with **Claude Sonnet 5.5** acting as editor, scoring 1–10 on a rubric:
   - Reach (0–4): how many people worldwide care
   - Impact (0–3): consequences for markets, economy, policy or daily life
   - Surprise (0–3): genuinely new vs routine

   Self-promo ("NEW MARKET"), the sources' own odds forecasts, routine data and gossip score low. Then:
   - **+0 to +3 for if.market relevance**: how directly the event moves the assets traded on if.market (clear "if X happens, asset Y moves" stories on any tradable asset or listed company, especially pending or upcoming decisions = +3; market-moving but harder to map to one asset, like wars, elections, tariffs = +2; indirect = +1; sports/entertainment = 0)
   - **+2** if Kalshi *and* Polymarket both report the same story (only one copy is posted)
   - **−0.75 per hour** of age, so fresh news beats stale news
3. **Picks** the top story. It alternates between Kalshi and Polymarket, unless the other account has a clearly bigger story (2+ points higher).
4. **Rewrites** the story in fresh wording, keeping all facts, numbers, names and acronyms exactly as written. It strips links and @mentions, and skips ads, promos and memes.
5. **Labels** the post:
   - `BREAKING:` for major stories (score 8+)
   - `JUST IN:` for everything else
6. **Posts** it to X.

Repeat protection works in three layers:

- The same source post is never used twice.
- A word-overlap check catches stories that are too similar.
- Claude compares each new story against the last 40 posts and skips it if it's the same news in different words.

## Files

| File | What it is |
|---|---|
| `bot.py` | The bot |
| `.github/workflows/bot.yml` | Runs the bot every 10 min on GitHub Actions |
| `state.json` | The bot's memory: queue, what's been posted, last post time. **Don't edit while running.** |
| `requirements.txt` | Python packages |

## Secrets (Settings → Secrets and variables → Actions)

| Name | Where to get it |
|---|---|
| `X_API_KEY` | X Developer Console → app → Keys & Tokens → **Consumer Key** |
| `X_API_SECRET` | Same popup → **Consumer Secret** |
| `X_BEARER_TOKEN` | Keys & Tokens → **Bearer Token** |
| `X_ACCESS_TOKEN` | Keys & Tokens → **Access Token** (must say *Read and write*) |
| `X_ACCESS_TOKEN_SECRET` | Same popup → **Access Token Secret** |
| `ANTHROPIC_API_KEY` | platform.claude.com → API Keys |

## Settings (optional: Settings → Secrets and variables → Actions → Variables)

| Variable | Default | What it does |
|---|---|---|
| `POSTS_PER_DAY` | `14` | Posts per day, spread evenly |
| `MAX_AGE_HOURS` | `4` | Ignore news older than this |
| `BREAKING_MIN_SCORE` | `8` | Score needed for a `BREAKING:` label |
| `SOURCE_ACCOUNTS` | `Kalshi,Polymarket` | Accounts to watch |
| `PLATFORM_ASSETS` | almost any asset (crypto, commodities, stocks, indices, FX, bonds) | What if.market trades (used for relevance) |
| `PLATFORM_WEIGHT` | `1.0` | Points per relevance level (0–3) |
| `RANK_MODEL` | `claude-sonnet-5-5` | Model that ranks stories |
| `CLAUDE_MODEL` | `claude-haiku-4-5-20251001` | Model that rewrites posts |
| `DRY_RUN` | `false` | `true` = log what it would post, without posting |

> Variables other than `SOURCE_ACCOUNTS` and `DRY_RUN` also need to be added to the `env:` block in `bot.yml` to take effect.

## Checking on it

- **Actions** tab → open a run → **Run bot** step. It shows the queue, the ranking, and each original → rewritten post.
- To pause: **Actions → x-news-bot → ⋯ → Disable workflow**.
- To post right now (if a slot is due): **Actions → x-news-bot → Run workflow**.

## Cost (approx.)

| | |
|---|---|
| X API | ~$0.005 per post read + ~$0.015 per post published. Links are stripped (posts with links cost ~$0.20). |
| Claude (Sonnet ranking + Haiku rewriting) | Roughly a few dollars a month |
| GitHub Actions | Free (private repo, within the free minutes) |

At 14 posts/day the X credit lasts roughly a month per $20.

## Notes

- GitHub's scheduled runs can be delayed or skipped when it's busy. If runs stop, an external timer (e.g. cron-job.org) can trigger the workflow instead.
- GitHub disables scheduled workflows after 60 days with no repo activity. The bot commits `state.json` regularly, which keeps it active.
