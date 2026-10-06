"""
X news bot: watches source accounts (Kalshi, Polymarket), queues their new
posts, and publishes ONE rephrased post at a time, spread evenly across the
day and alternating between sources.

Runs once per invocation (GitHub Actions cron every 10 min). State lives in
state.json so nothing is posted twice.
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
POSTS_PER_DAY = int(os.getenv("POSTS_PER_DAY", "14"))
GAP_SECONDS = 86400 / POSTS_PER_DAY          # ~1h43m for 14/day
GAP_TOLERANCE = 6 * 60                       # GitHub cron runs late sometimes
MAX_AGE_SECONDS = int(os.getenv("MAX_AGE_HOURS", "4")) * 3600  # drop stale news
MAX_QUEUE_PER_SOURCE = 30
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
STATE_FILE = Path(__file__).with_name("state.json")

SYSTEM_PROMPT = """You rewrite breaking-news posts for an X news account.

The source accounts post short headlines on ANY topic (politics, crypto, AI/tech,
economy, sports, world events, markets, odds). All of these count as news and
should be rewritten. Headlines starting with "JUST IN", "BREAKING", etc. are news.

Rules:
- Rephrase in fresh wording and sentence structure. Never copy phrases verbatim.
- Keep every fact, number, percentage, name and date exactly accurate. Do not add facts.
- Max 260 characters. Plain text. At most one emoji. No hashtags.
- No URLs or links of any kind.
- Do not mention Kalshi, Polymarket, or any source account or @handle.
- Never refer to a video, clip, image, chart or "watch"/"see below".
- Output exactly SKIP ONLY if the post is clearly not news: an ad/promo for the
  platform itself, giveaway, job post, pure meme/joke with no information,
  reply-bait question, "sign up"/"download"/"trade now" call to action, or it
  only makes sense with its video/image. When in doubt, rewrite it.
Output only the rewritten post, nothing else."""


# ---------- state ----------
def load_state():
    s = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    s.setdefault("user_ids", {})
    s.setdefault("since_ids", {})
    s.setdefault("posted", [])
    s.setdefault("recent", [])       # source texts we've used
    s.setdefault("recent_out", [])   # what we actually posted
    s.setdefault("queue", [])
    s.setdefault("last_post_ts", 0)
    s.setdefault("last_source", "")
    return s


def save_state(state):
    state["posted"] = state["posted"][-500:]
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ---------- helpers ----------
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
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip().strip('"').strip()


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


def rephrase(claude, original: str, recent_out: list[str]) -> str | None:
    if recent_out:
        history = "\n".join(f"- {p}" for p in recent_out[-40:])
        prompt = (f"Posts already published recently:\n{history}\n\n"
                  "If the new post below reports the SAME story as one of those "
                  "(even if worded differently or with slightly updated numbers), "
                  "output exactly: SKIP. A different story on a similar topic is fine.\n\n"
                  f"New post to rewrite:\n{original}")
    else:
        prompt = f"New post to rewrite:\n{original}"
    msg = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    out = clean("".join(b.text for b in msg.content if b.type == "text"))
    if not out or out.upper().startswith("SKIP"):
        return None
    if len(out) > 280:
        out = out[:277].rsplit(" ", 1)[0] + "…"
    return out


# ---------- steps ----------
def fetch_new(x, state, now):
    """Pull new posts from each source into the queue (videos excluded)."""
    for handle in SOURCE_ACCOUNTS:
        if handle not in state["user_ids"]:
            u = x.get_user(username=handle, user_auth=False)
            state["user_ids"][handle] = str(u.data.id)

    for handle in SOURCE_ACCOUNTS:
        since = state["since_ids"].get(handle)
        resp = x.get_users_tweets(
            state["user_ids"][handle],
            since_id=since,
            max_results=5 if since is None else 20,
            exclude=["retweets", "replies"],
            tweet_fields=["created_at", "note_tweet", "attachments"],
            expansions=["attachments.media_keys"],
            media_fields=["type"],
            user_auth=False,
        )
        tweets = resp.data or []
        if not tweets:
            print(f"@{handle}: nothing new")
            continue
        state["since_ids"][handle] = str(max(int(t.id) for t in tweets))
        if since is None:
            print(f"@{handle}: first run, bookmarked latest")
            continue

        video_keys = {m.media_key for m in (resp.includes or {}).get("media", [])
                      if m.type in ("video", "animated_gif")}
        added = 0
        seen = set(state["posted"]) | {i["id"] for i in state["queue"]}
        for t in tweets:
            if str(t.id) in seen:
                continue
            keys = set((t.data.get("attachments") or {}).get("media_keys", []))
            if keys & video_keys:
                print(f"@{handle}/{t.id}: has video, skipped")
                continue
            text = (t.data.get("note_tweet") or {}).get("text") or t.text
            ts = t.created_at.timestamp() if t.created_at else now
            state["queue"].append({"id": str(t.id), "handle": handle,
                                   "text": text, "ts": ts})
            added += 1
        print(f"@{handle}: queued {added} new")


def prune_queue(state, now):
    done = set(state["posted"])
    q = [i for i in state["queue"]
         if i["id"] not in done and now - i["ts"] <= MAX_AGE_SECONDS]
    # keep queue bounded per source (newest kept)
    trimmed = []
    for h in SOURCE_ACCOUNTS:
        items = sorted([i for i in q if i["handle"] == h], key=lambda i: int(i["id"]))
        trimmed += items[-MAX_QUEUE_PER_SOURCE:]
    state["queue"] = trimmed


def source_order(state):
    """Alternate: start with the source that did NOT post last."""
    srcs = list(SOURCE_ACCOUNTS)
    if state["last_source"] in srcs:
        srcs.remove(state["last_source"])
        srcs.append(state["last_source"])
    return srcs


def post_one(x, claude, state, now):
    for handle in source_order(state):
        # newest first within that source
        items = sorted([i for i in state["queue"] if i["handle"] == handle],
                       key=lambda i: int(i["id"]), reverse=True)
        for item in items:
            state["queue"].remove(item)
            state["posted"].append(item["id"])
            if is_duplicate(item["text"], state["recent"] + state["recent_out"]):
                print(f"SKIP duplicate story @{handle}/{item['id']}")
                continue
            try:
                new_text = rephrase(claude, item["text"], state["recent_out"])
            except Exception as e:
                print(f"Claude error on {item['id']}: {e}")
                continue
            if not new_text:
                print(f"SKIP (not news or already covered) @{handle}/{item['id']}")
                continue
            if is_duplicate(new_text, state["recent_out"], threshold=0.5):
                print(f"SKIP rewrite too similar to a recent post @{handle}/{item['id']}")
                continue
            print(f"\n@{handle}/{item['id']}\n  IN : {item['text']!r}\n  OUT: {new_text!r}")
            if not DRY_RUN:
                try:
                    x.create_tweet(text=new_text)
                except tweepy.TweepyException as e:
                    print(f"Post failed: {e}")
                    return
            state["recent"] = (state["recent"] + [item["text"]])[-100:]
            state["recent_out"] = (state["recent_out"] + [new_text])[-100:]
            state["last_post_ts"] = now
            state["last_source"] = handle
            return
    print("Queue empty — nothing to post this slot.")


# ---------- main ----------
def main():
    now = time.time()
    state = load_state()
    x = x_client()

    fetch_new(x, state, now)
    prune_queue(state, now)
    print(f"Queue: " + ", ".join(
        f"{h}={sum(1 for i in state['queue'] if i['handle'] == h)}" for h in SOURCE_ACCOUNTS))

    wait = state["last_post_ts"] + GAP_SECONDS - GAP_TOLERANCE - now
    if wait > 0:
        print(f"Next post slot in {int(wait // 60)} min.")
    else:
        post_one(x, anthropic.Anthropic(), state, now)

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        sys.exit(f"Missing environment variable: {e}")
