"""
X news bot: watches source accounts (Kalshi, Polymarket), rephrases new posts
with Claude, and posts them to your X account.

Designed to run once per invocation (GitHub Actions cron). State is kept in
state.json so the same tweet is never posted twice.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic
import tweepy

# ---------- config ----------
SOURCE_ACCOUNTS = [a.strip().lstrip("@") for a in
                   os.getenv("SOURCE_ACCOUNTS", "Kalshi,Polymarket").split(",") if a.strip()]
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "4"))
MAX_POSTS_PER_DAY = int(os.getenv("MAX_POSTS_PER_DAY", "20"))  # budget guard
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
STATE_FILE = Path(__file__).with_name("state.json")

SYSTEM_PROMPT = """You rewrite prediction-market news posts for an X account.

Rules:
- Rephrase in fresh wording and sentence structure. Never copy phrases verbatim.
- Keep every fact, number, percentage, name and date exactly accurate. Do not add facts.
- Max 260 characters. Plain text. At most one emoji. No hashtags.
- No URLs or links of any kind.
- Do not mention Kalshi, Polymarket, or any source account or @handle.
- If the post is NOT a news update (e.g. promo, giveaway, ad, job post, meme with no
  info, reply-bait, "sign up"/"download" calls to action), output exactly: SKIP
Output only the rewritten post, nothing else."""


# ---------- state ----------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"user_ids": {}, "since_ids": {}, "posted": []}


def save_state(state):
    state["posted"] = state["posted"][-500:]  # keep file small
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ---------- clients ----------
def x_client():
    return tweepy.Client(
        bearer_token=os.environ["X_BEARER_TOKEN"],
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )


URL_RE = re.compile(r"https?://\S+|\bt\.co/\S+|\bwww\.\S+", re.I)


def clean(text: str) -> str:
    text = URL_RE.sub("", text)
    text = re.sub(r"@\w+", "", text)          # never tag anyone
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip().strip('"').strip()


def rephrase(claude, original: str) -> str | None:
    msg = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": original}],
    )
    out = clean("".join(b.text for b in msg.content if b.type == "text"))
    if not out or out.upper().startswith("SKIP"):
        return None
    if len(out) > 280:
        out = out[:277].rsplit(" ", 1)[0] + "…"
    return out


def _words(s):
    return set(re.findall(r"[a-z0-9%$.]+", clean(s).lower()))


def is_duplicate(text, recent, threshold=0.6):
    """Kalshi and Polymarket often post the same story; avoid posting it twice."""
    a = _words(text)
    for prev in recent:
        b = _words(prev)
        if a and b and len(a & b) / len(a | b) >= threshold:
            return True
    return False


# ---------- main ----------
def main():
    state = load_state()
    x = x_client()
    claude = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    # Resolve handles -> user IDs once, then cache (saves API cost).
    for handle in SOURCE_ACCOUNTS:
        if handle not in state["user_ids"]:
            u = x.get_user(username=handle, user_auth=False)
            state["user_ids"][handle] = str(u.data.id)
            print(f"Resolved @{handle} -> {u.data.id}")

    queue = []  # (tweet_id, handle, text)
    for handle in SOURCE_ACCOUNTS:
        uid = state["user_ids"][handle]
        since = state["since_ids"].get(handle)
        resp = x.get_users_tweets(
            uid,
            since_id=since,
            max_results=5 if since is None else 20,
            exclude=["retweets", "replies"],
            tweet_fields=["created_at", "note_tweet"],
            user_auth=False,
        )
        tweets = resp.data or []
        if not tweets:
            print(f"@{handle}: nothing new")
            continue
        newest = max(int(t.id) for t in tweets)
        if since is None:
            # First run: just bookmark the latest post so we don't flood old news.
            state["since_ids"][handle] = str(newest)
            print(f"@{handle}: first run, bookmarked {newest}")
            continue
        state["since_ids"][handle] = str(newest)
        for t in tweets:
            full = (t.data.get("note_tweet") or {}).get("text")  # long posts
            queue.append((int(t.id), handle, full or t.text))

    queue.sort(key=lambda q: q[0])  # oldest first
    if len(queue) > MAX_POSTS_PER_RUN:
        print(f"{len(queue)} new posts; keeping newest {MAX_POSTS_PER_RUN}")
        queue = queue[-MAX_POSTS_PER_RUN:]

    today = time.strftime("%Y-%m-%d", time.gmtime())
    if state.get("day") != today:
        state["day"], state["day_count"] = today, 0

    posted_ids = set(state["posted"])
    for tid, handle, text in queue:
        if str(tid) in posted_ids:
            continue
        if state["day_count"] >= MAX_POSTS_PER_DAY:
            print(f"Daily cap of {MAX_POSTS_PER_DAY} reached; skipping the rest.")
            state["posted"].append(str(tid))
            continue
        try:
            new_text = rephrase(claude, text)
        except Exception as e:
            print(f"Claude error on {tid}: {e}")
            continue
        state["posted"].append(str(tid))  # mark handled either way
        if not new_text:
            print(f"SKIP (not news) @{handle}/{tid}")
            continue
        if is_duplicate(text, state.setdefault("recent", [])):
            print(f"SKIP (same story already posted) @{handle}/{tid}")
            continue
        state["recent"] = (state["recent"] + [text])[-40:]
        print(f"\n@{handle}/{tid}\n  IN : {text!r}\n  OUT: {new_text!r}")
        if DRY_RUN:
            continue
        try:
            x.create_tweet(text=new_text)
            state["day_count"] += 1
            time.sleep(20)  # space out posts
        except tweepy.TooManyRequests:
            print("Rate limited by X; stopping this run.")
            break
        except tweepy.TweepyException as e:
            print(f"Post failed for {tid}: {e}")

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        sys.exit(f"Missing environment variable: {e}")
