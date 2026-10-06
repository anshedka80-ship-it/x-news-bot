# X News Bot

Watches **@Kalshi** and **@Polymarket**, picks the biggest headline, rewrites it with Claude, and posts it to **@ifmarkets**: **14 posts a day**, one about every **1 h 43 min**, around the clock.

## How it works

Every 10 minutes the bot checks whether a posting slot is due. Only then does it read X (to save cost): the **7 newest posts** from each account since the last read go into the queue. Then it:

1. **Filters** the queue:
   - drops posts with videos or GIFs
   - drops anything older than 3 hours
   - drops stories that repeat something already posted
   - drops **too-sensitive** stories (deaths/casualties, attacks, terrorism, hijackings, shootings, violent crime and suspects, suicide, abuse); wars, sanctions and policy news are still allowed
2. **Ranks** the whole queue together with **Claude Sonnet 5.5** acting as editor, scoring 1–10 on a rubric:
   - Reach (0–4): how many people worldwide care
   - Impact (0–3): consequences for markets, economy, policy or daily life
   - Surprise (0–3): genuinely new vs routine

   Self-promo ("NEW MARKET"), the sources' own odds forecasts, routine data and gossip score low. Then:
   - **+0 to +3 for if.market relevance**: how directly the event moves the assets traded on if.market (clear "if X happens, asset Y moves" stories on any tradable asset or listed company, especially pending or upcoming decisions = +3; market-moving but harder to map to one asset, like wars, elections, tariffs = +2; indirect = +1; sports/entertainment = 0)
   - **+2** if Kalshi *and* Polymarket both report the same story (only one copy is posted)
   - **Freshness as a tie-breaker:** +0.5 if under 30 minutes old and −0.5 per hour of age, so relevance decides and age only separates similar stories (stories older than 3 hours are dropped)
3. **Picks** the top story. It alternates between Kalshi and Polymarket, unless the other account has a clearly bigger story (2+ points higher).
4. **Rewrites** the story in wire-service style (Sonnet 5.5): short, direct, no hype, no emojis, no hashtags, in fresh wording, keeping all facts, numbers, names and acronyms exactly as written. It strips links and @mentions, and skips ads, promos and memes.
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
| `bot.py` | The bot: fetch, rank, rewrite, post |
| `.github/workflows/bot.yml` | Runs the bot on GitHub Actions: each run checks every 10 min for ~5h40m, then starts the next run itself (a backup schedule every 2 hours restarts it if the chain breaks) |
| `state.json` | The bot's memory: queue with scores, what's been posted, last post time. Committed after every check. **Don't edit while running.** |
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
| `MAX_AGE_HOURS` | `3` | Ignore news older than this |
| `READS_PER_SLOT` | `7` | Newest posts read per account at each slot (X minimum is 5; more = more choice, more cost) |
| `BREAKING_MIN_SCORE` | `8` | Score needed for a `BREAKING:` label |
| `SOURCE_ACCOUNTS` | `Kalshi,Polymarket` | Accounts to watch |
| `PLATFORM_ASSETS` | almost any asset (crypto, commodities, stocks, indices, FX, bonds) | What if.market trades (used for relevance) |
| `PLATFORM_WEIGHT` | `1.0` | Points per relevance level (0–3) |
| `RANK_MODEL` | `claude-sonnet-5-5` | Model that ranks stories |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | Model that rewrites posts |
| `DRY_RUN` | `false` | `true` = log what it would post, without posting |

> Variables other than `SOURCE_ACCOUNTS` and `DRY_RUN` also need to be added to the `env:` block in `bot.yml` to take effect.

## Checking on it

- **Actions** tab → open the run that is *in progress* → **Run bot every 10 minutes** step → expand a **Check N/34** group. Between slots it just says "Next post slot in N min"; at a slot it shows the posts queued, the ranking (`total (news, rel, age)`), and the original → rewritten post.
- **To pause:** open the in-progress run → **Cancel workflow**, then **x-news-bot → ⋯ → Disable workflow** (otherwise the backup schedule restarts it).
- **To resume:** **Enable workflow**, then **Run workflow**.
- Code changes pushed to `main` are picked up at the next 10-minute check, no restart needed.

## Cost (approx., per month)

| Item | How it's charged | Est. per month |
|---|---|---|
| X API: reading source posts | $0.005 per post read; only the 7 newest per account at each of the 14 slots (≤196 a day, ~140 in practice) | ~$18–25 |
| X API: publishing | $0.015 per post (links stripped; a post with a link costs ~$0.20) × 14 a day | ~$6 |
| Claude Sonnet 5.5: ranking + rewriting | $2 / $10 per million input / output tokens, ~28 calls a day | ~$4–5 |
| GitHub Actions | Free (public repo) | $0 |
| **Total** | | **~$28–36** |

Posting more often adds ~$0.025 per extra post (X + Claude): 24 posts a day ≈ +$8/month, 48 a day ≈ +$29/month. Reading cost doesn't change with posting frequency.

## Notes

- API keys are stored as encrypted GitHub secrets; they never appear in the code or the public logs.
- GitHub's own cron was unreliable for this new repo, which is why the workflow keeps itself running in a loop instead of relying on it.
- If the X or Claude credit runs out, checks keep running but posting fails (shown as `Post failed` / `Claude error` in the log) until credit is added.
