# X News Bot

Checks @Kalshi and @Polymarket every 10 minutes, rewrites each new news post with Claude, and posts it to your X account. Runs free on GitHub Actions.

## 1. Get X API keys (~10 min)

1. Go to https://developer.x.com and sign in **with the account the bot will post from**.
2. Sign up for a developer account (pay-per-use is the default) and add credit (~$20 to start).
3. Create a Project + App.
4. In the app's **User authentication settings**: set permissions to **Read and write**, type **Web App/Automated bot**, callback URL `https://example.com`, website any URL. Save.
5. Under **Keys and tokens**, generate and copy:
   - API Key and API Key Secret
   - Bearer Token
   - Access Token and Access Token Secret (generate these *after* setting Read and write, or posting will fail with 403)

## 2. Get a Claude API key

https://console.anthropic.com → API Keys → Create key. Add ~$5 credit.

## 3. Put it on GitHub

1. Create a new **private** repo and upload everything in this folder (including the hidden `.github` folder).
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**. Add:

   | Name | Value |
   |---|---|
   | `X_API_KEY` | API Key |
   | `X_API_SECRET` | API Key Secret |
   | `X_BEARER_TOKEN` | Bearer Token |
   | `X_ACCESS_TOKEN` | Access Token |
   | `X_ACCESS_TOKEN_SECRET` | Access Token Secret |
   | `ANTHROPIC_API_KEY` | Claude key |

3. (Optional) Under the **Variables** tab: `DRY_RUN` = `true` to test without posting, `SOURCE_ACCOUNTS` = `Kalshi,Polymarket,AnotherAccount` to change sources.

## 4. Start it

Repo → **Actions** → enable workflows → **x-news-bot** → **Run workflow**.

- The **first run** only bookmarks the latest posts (so it doesn't dump old news).
- From then on it runs every ~10 minutes. Check the run logs to see each original vs. rewritten post.
- To pause: Actions → x-news-bot → ⋯ → Disable workflow.

## Cost (pay-per-use, Oct 2026)

- Reading: ~$0.005 per new post fetched
- Posting: ~$0.015 per post (the bot strips links — posts with links cost ~$0.20)
- Claude Haiku: well under $0.001 per rewrite
- At ~100 posts/day total: roughly **$50–60/month** on X, ~$1 on Claude. GitHub Actions is free for this.

## Tuning

Edit `SYSTEM_PROMPT` in `bot.py` to change the voice/style. `MAX_POSTS_PER_RUN` (default 4) caps bursts.

## Heads up

GitHub disables scheduled workflows on repos with no activity for 60 days. The bot commits `state.json` whenever there's news, which normally keeps it active.
