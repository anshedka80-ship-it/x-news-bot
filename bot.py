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
MAX_AGE_SECONDS = int(float(os.getenv("MAX_AGE_HOURS", "3")) * 3600)  # drop stale news
MAX_QUEUE_PER_SOURCE = 30
READS_PER_SLOT = max(5, min(100, int(os.getenv("READS_PER_SLOT", "7"))))  # X API minimum is 5
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")  # rewriting model
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
STATE_FILE = Path(__file__).with_name("state.json")

SYSTEM_PROMPT = """You are the headline writer for a fast, credible markets-and-news
account on X (like a wire service: Reuters, Bloomberg, AP). You rewrite a source
headline into our own headline.

The source posts short headlines on ANY topic (politics, crypto, AI/tech,
economy, companies, world events, markets). All of these count as news.

STYLE
- Natural, fluent English that reads like a professional news desk wrote it,
  not a lightly edited copy. Factual, active voice, present tense.
- One sentence, or two short ones if that reads better. Usually 90-220
  characters. It is fine to be a little longer than the source if that makes
  it clearer, as long as you add NO new facts.
- Lead with the most newsworthy part. You may reorder the sentence (who/what,
  then the detail), and make implied context explicit only when it is already
  in the source (e.g. "the chain", "the company", "its CEO").
- No filler or hype: no "reportedly", "massive", "huge", "shocking", "could
  potentially", "it has been announced that", "in a major move".
- No opinions, commentary, questions, calls to action or "here's why".
- NO emojis. NO hashtags. No URLs. No @mentions.

ACCURACY (most important)
- Keep every fact, number, percentage, currency, name, ticker and date exactly.
- Never add facts, context, causes or predictions that are not in the source.
- Keep attribution: if the source says "X says"/"per Y"/"according to Z", keep
  that the claim comes from them (you may name Y/Z, e.g. "per Billboard").
- Keep acronyms and jargon exactly as written (e.g. "SI", "ETF", "CPI").
  Never guess what an acronym stands for.
- If the source is its own platform's traders/markets ("our traders",
  "our markets"), say "prediction market traders" / "prediction markets".
  Never write "our". Do not name Kalshi or Polymarket.
- Never refer to a video, clip, image or chart.

REWORDING (important)
- Genuinely rewrite: change the sentence structure AND the wording. Do not keep
  the source's sentence skeleton with a word or two swapped.
- Apart from names, numbers, titles and direct quotes, never reuse more than
  three words in a row from the source.

PREFIX
- Do NOT start with "JUST IN", "BREAKING" or similar; it is added automatically.

EXAMPLES
Source: BREAKING: Customer sues McDonald's, alleging their SI tool coordinates menu prices
Good: McDonald's faces a lawsuit from a customer who claims the chain's SI tool is being used to coordinate menu prices
Bad:  Customer sues McDonald's, alleging its SI tool coordinates menu prices   (same sentence, one word changed)

Source: JUST IN: Ray Dalio warns a US debt crisis could hit within 3 years
Good: A US debt crisis could arrive within the next 3 years, Ray Dalio warns
Bad:  Ray Dalio cautions that the US could potentially face a massive debt crisis within the next three years 📉

Source: JUST IN: AMD CEO says customers are demanding more AI chips than they can make
Good: Demand for AMD's AI chips is now outpacing what the company can produce, according to its CEO

Source: JUST IN: Google is set to close a $1 billion deal to buy nuclear power from Constellation for its data centers
Good: Google is nearing a $1 billion agreement with Constellation to supply its data centers with nuclear power

Source: JUST IN: Our traders now forecast US diesel prices will fall to $6.20 this month
Good: Prediction market traders now expect US diesel prices to drop to $6.20 this month

SKIP
Also output exactly SKIP if the story is too sensitive for a markets brand:
deaths/casualties as the core, attacks, terrorism, hijackings, shootings,
violent or sexual crime, crime suspects, suicide, abuse, child harm, death tolls.
Wars, military moves, sanctions and policy news are fine.

Output exactly SKIP only if the post is clearly not news: an ad/promo for the
platform itself, a "new market" launch, giveaway, job post, pure meme/joke with
no information, reply-bait question, "sign up"/"download"/"trade now" call to
action, or it only makes sense with its video/image. When in doubt, rewrite it.

Output only the rewritten headline, nothing else."""


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


BREAKING_PREFIX = "BREAKING: "   # used for the biggest stories
JUSTIN_PREFIX = "JUST IN: "      # used for everything else
BREAKING_MIN_SCORE = float(os.getenv("BREAKING_MIN_SCORE", "8"))
PREFIX_RE = re.compile(r"^\W*(just in|breaking( news)?|update|developing)\s*[:\-–—]\s*", re.I)

URL_RE = re.compile(r"https?://\S+|\bt\.co/\S+|\bwww\.\S+", re.I)


EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF\U0000FE0F\U0000200D\U000020E3]+")


def clean(text: str) -> str:
    text = URL_RE.sub("", text)
    text = EMOJI_RE.sub("", text)
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    # remove quotes only if they wrap the whole text (keep real quotations)
    if len(text) > 1 and text[0] in '"“' and text[-1] in '"”' and text.count('"') + text.count('“') + text.count('”') == 2:
        text = text[1:-1].strip()
    return text


def _trigrams(text: str):
    w = re.findall(r"[a-z0-9$%.']+", PREFIX_RE.sub("", clean(text)).lower())
    return {tuple(w[i:i + 3]) for i in range(len(w) - 2)}


def copied_too_much(source: str, rewrite: str, limit: float = 0.4) -> bool:
    """True if the rewrite reuses too many 3-word runs from the source."""
    a, b = _trigrams(source), _trigrams(rewrite)
    return bool(b) and len(a & b) / len(b) > limit


def balance_quotes(text: str) -> str:
    """Close an unmatched quotation mark so a post never ends mid-quote."""
    if text.count('"') % 2 == 1:
        text += '"'
    if text.count('\u201c') > text.count('\u201d'):
        text += '\u201d'
    return text


def _words(s):
    return set(re.findall(r"[a-z0-9%$.]+", PREFIX_RE.sub("", clean(s)).lower()))


def is_duplicate(text, recent, threshold=0.6):
    """Kalshi and Polymarket often post the same story; avoid posting it twice."""
    a = _words(text)
    for prev in recent:
        b = _words(prev)
        if a and b and len(a & b) / len(a | b) >= threshold:
            return True
    return False


def rephrase(claude, original: str, recent_out: list[str], prefix: str = JUSTIN_PREFIX) -> str | None:
    if recent_out:
        history = "\n".join(f"- {p}" for p in recent_out[-20:])
        prompt = (f"Posts already published recently:\n{history}\n\n"
                  "If the new post below reports the SAME story as one of those "
                  "(even if worded differently or with slightly updated numbers), "
                  "output exactly: SKIP. A different story on a similar topic is fine.\n\n"
                  f"New post to rewrite:\n{original}")
    else:
        prompt = f"New post to rewrite:\n{original}"
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(2):
        msg = claude.messages.create(
            model=CLAUDE_MODEL, max_tokens=300, system=SYSTEM_PROMPT, messages=messages)
        out = clean("".join(b.text for b in msg.content if b.type == "text"))
        if attempt == 0 and out and not out.upper().startswith("SKIP") \
                and copied_too_much(original, out):
            print(f"Rewrite too close to source, retrying: {out!r}")
            messages += [{"role": "assistant", "content": out},
                         {"role": "user", "content": "That is too close to the source wording. "
                          "Rewrite it with a different sentence structure and different words "
                          "(keep names, numbers and quotes). Output only the headline."}]
            continue
        break
    if not out or out.upper().startswith("SKIP"):
        return None
    # Always start with the fixed prefix; drop any prefix Claude/source added.
    out = PREFIX_RE.sub("", out).strip()
    out = balance_quotes(out)
    room = 280 - len(prefix)
    if len(out) > room:
        out = out[:room - 1].rsplit(" ", 1)[0] + "…"
    return prefix + out


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
            max_results=5 if since is None else READS_PER_SLOT,  # newest N only (cost)
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


RANK_MODEL = os.getenv("RANK_MODEL", "claude-sonnet-5-5")
BOTH_SOURCES_BOOST = 2.0        # story reported by both Kalshi AND Polymarket
FRESHNESS_PENALTY_PER_HOUR = float(os.getenv("FRESHNESS_PENALTY_PER_HOUR", "0.5"))  # older stories lose points
FRESH_BONUS = 0.5                  # small tie-breaker for news under 30 minutes old
PLATFORM_ASSETS = os.getenv("PLATFORM_ASSETS", "almost any asset: crypto (BTC, ETH, SOL...), commodities (oil, gold...), individual stocks, indices, currencies and bonds")
PLATFORM_WEIGHT = float(os.getenv("PLATFORM_WEIGHT", "1.0"))  # points per relevance level (0-3)

RANK_PROMPT = """You are the editor of the X account of if.market, "the first
consequence market". Users trade an asset's price INSIDE a future outcome, e.g.
"What is oil worth if the US strikes Iran? What is BTC worth if the Fed cuts?
What is a stock worth if the product ships?" Assets include """ + PLATFORM_ASSETS + """.
Market categories: Geopolitics, Politics, Crypto, Climate, Economics, Companies,
Finance, Tech & Science.

For each headline give two scores.

1) "score" 1-10: how big the news is, using this rubric:
- Reach (0-4): how many people worldwide care. Elections, wars, central banks,
  mega-cap companies, top celebrities, major disasters score high; local or
  niche stories score low.
- Impact (0-3): real consequences for markets, the economy, policy or daily life.
- Surprise/newsworthiness (0-3): genuinely new, unexpected or a major update,
  versus routine/scheduled/incremental info.
Score LOW (1-3): platform self-promotion ("NEW MARKET", "trade now"), the
source's own trader forecasts or odds without a real-world event, routine
data, minor celebrity gossip, memes, vague teasers.

2) "relevance" 0-3: how well the story fits if.market (an event whose outcome
would move one of its assets):
- 3 = a clear "if X happens, asset Y moves" story on ANY tradable asset or
  company: e.g. strikes/sanctions/OPEC (oil), Fed/ECB/rate decisions, inflation
  or jobs data (BTC, gold, indices, dollar), crypto regulation, ETF approvals,
  hacks (crypto), earnings, deals, product launches, lawsuits, CEO moves or
  bans affecting a listed company (that stock). Pending, threatened or upcoming
  decisions are the BEST fit (users can trade the scenario).
- 2 = market-moving but less direct or harder to map to one asset: wars,
  elections, tariffs, recession signals, big policy shifts
- 1 = some indirect market or policy angle
- 0 = no market angle (sports, entertainment, culture, gossip)

3) "sensitive" 0-2: how sensitive the story is for a markets brand to post:
- 2 = TOO SENSITIVE, never post: deaths, casualties or injuries as the core of
  the story; attacks, terrorism, hijackings, shootings, bombings, kidnappings;
  violent or sexual crime, crime suspects/perpetrators and their backgrounds;
  suicide or self-harm; abuse; child harm; graphic disasters or death tolls;
  hate incidents; individual personal tragedies.
- 1 = serious but OK: wars, military moves, sanctions, strikes, elections,
  layoffs, disasters framed by economic/market impact (no casualty focus).
- 0 = not sensitive.

Also give each headline a short "story" key (2-5 lowercase words) naming the
underlying event, e.g. "fed rate cut", "ray dalio debt warning". Headlines about
the SAME event must get the SAME key, even if worded differently.

Reply ONLY with JSON: {"1": {"score": 7, "relevance": 2, "sensitive": 0, "story": "..."}, "2": {...}}"""


def score_queue(claude, state):
    """Re-score the whole queue together so scores are comparable."""
    q = state["queue"]
    if not q:
        return
    listing = "\n".join(f"{n}. [{i['handle']}] {i['text'][:220]}" for n, i in enumerate(q, 1))
    try:
        msg = claude.messages.create(
            model=RANK_MODEL, max_tokens=1500, system=RANK_PROMPT,
            messages=[{"role": "user", "content": listing}])
        raw = "".join(b.text for b in msg.content if b.type == "text")
        res = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception as e:
        print(f"Ranking failed ({e}); using previous/neutral scores.")
        res = {}
    for n, i in enumerate(q, 1):
        r = res.get(str(n)) or {}
        try:
            i["score"] = float(r.get("score", i.get("score", 5)))
        except (TypeError, ValueError):
            i["score"] = float(i.get("score", 5))
        i["story"] = str(r.get("story") or i.get("story") or i["id"]).strip().lower()
        try:
            i["sensitive"] = int(r.get("sensitive", i.get("sensitive", 0)))
        except (TypeError, ValueError):
            i["sensitive"] = int(i.get("sensitive", 0))
        try:
            i["relevance"] = max(0.0, min(3.0, float(r.get("relevance", i.get("relevance", 0)))))
        except (TypeError, ValueError):
            i["relevance"] = float(i.get("relevance", 0))

    # Both sources covering the same story = bigger story.
    handles_by_story = {}
    for i in q:
        handles_by_story.setdefault(i["story"], set()).add(i["handle"])
    for i in q:
        i["both"] = len(handles_by_story[i["story"]]) > 1
        i["importance"] = min(10.0, i["score"] + (BOTH_SOURCES_BOOST if i["both"] else 0))


def final_score(i, now):
    """News size (+both-sources boost) + if.market relevance - age."""
    age_h = max(0.0, (now - i.get("ts", now)) / 3600)
    return (i.get("importance", i.get("score", 5))
            + PLATFORM_WEIGHT * i.get("relevance", 0)
            - FRESHNESS_PENALTY_PER_HOUR * age_h
            + (FRESH_BONUS if age_h < 0.5 else 0))


def pick_order(state, now):
    """Best final score first, across both sources (no alternation)."""
    return sorted(state["queue"], key=lambda i: (final_score(i, now), int(i["id"])), reverse=True)


def post_one(x, claude, state, now):
    score_queue(claude, state)
    for i in [i for i in state["queue"] if i.get("sensitive", 0) >= 2]:
        state["queue"].remove(i)
        state["posted"].append(i["id"])
        print(f"SKIP too sensitive @{i['handle']}/{i['id']}: {i['text'][:80]!r}")
    order = pick_order(state, now)
    forced = state.pop("force_next", None)
    if forced:
        pick = [i for i in order if i["id"] == str(forced)]
        if pick:
            order = pick  # post exactly this story (still checked for repeats)
        else:
            print(f"Requested story {forced} not in queue (posted, too old or too sensitive).")
    print("Ranked: " + " | ".join(
        f"{final_score(i, now):.1f} (news {i.get('score')}, rel {i.get('relevance', 0)}, sens {i.get('sensitive', 0)}{' +both' if i.get('both') else ''}, "
        f"{(now - i.get('ts', now)) / 60:.0f}m old) @{i['handle']}: {i['text'][:45]!r}"
        for i in order[:6]))
    for item in order:
        handle = item["handle"]
        if item not in state["queue"]:
            continue
        state["queue"].remove(item)
        state["posted"].append(item["id"])
        if is_duplicate(item["text"], state["recent"] + state["recent_out"]):
            print(f"SKIP duplicate story @{handle}/{item['id']}")
            continue
        try:
            prefix = (BREAKING_PREFIX if item.get("importance", item.get("score", 0)) >= BREAKING_MIN_SCORE
                      else JUSTIN_PREFIX)
            new_text = rephrase(claude, item["text"], state["recent_out"], prefix)
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
        # Drop the other source's copy of the same story.
        for other in [i for i in state["queue"] if i.get("story") == item.get("story")]:
            state["queue"].remove(other)
            state["posted"].append(other["id"])
            print(f"Dropped same story from @{other['handle']}/{other['id']}")
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

    wait = state["last_post_ts"] + GAP_SECONDS - GAP_TOLERANCE - now
    if state.get("force_next"):
        print(f"Manual request: posting queued story {state['force_next']} now.")
        wait = 0
    if wait > 0 and all(h in state["since_ids"] for h in SOURCE_ACCOUNTS):
        # Cost saving: X charges per post read, so we only read at posting time.
        print(f"Next post slot in {int(wait // 60)} min (no X reads until then).")
        return save_state(state)

    fetch_new(x, state, now)
    prune_queue(state, now)
    print(f"Queue: " + ", ".join(
        f"{h}={sum(1 for i in state['queue'] if i['handle'] == h)}" for h in SOURCE_ACCOUNTS))
    if wait <= 0:
        post_one(x, anthropic.Anthropic(), state, now)

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        sys.exit(f"Missing environment variable: {e}")
