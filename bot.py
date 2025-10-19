import os
import asyncio
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import aiohttp
import psycopg2
import psycopg2.extras as pgx

import discord
from discord.ext import commands, tasks
from discord import app_commands, ui, ButtonStyle


# =========================
# ENV & CONSTANTS
# =========================
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

CHAN_BEST = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
CHAN_QUICK = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
CHAN_LONG = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))

# Value Bets mirror/duplicate channel
CHAN_VALUE_DUP = int(os.getenv("VALUE_BETS_CHANNEL_ID", "0"))

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# bankroll + stake policy
BANKROLL_UNITS = 1000.0
CONSERVATIVE_PCT = 0.015  # 1.5% base unit proportion
VALUE_EDGE_THRESHOLD = 2.0  # % edge to display "Value Bet" marker
BEST_EDGE_MIN = 1.0         # avoid "low value" on Best Bet

# bookmakers allowlist (case-insensitive substring contains)
ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch",
    "neds", "pointsbet", "dabble", "betfair", "tab"
]

# Sports emojis & normalization
SPORT_EMOJIS = {
    "soccer": "⚽",
    "basketball": "🏀",
    "americanfootball": "🏈",
    "baseball": "⚾",
    "icehockey": "🏒",
    "cricket": "🏏",
    "mma": "🥋",
    "boxing": "🥊",
    "tennis": "🎾",
    "tabletennis": "🏓",
    "rugby": "🏉",
    "aussierules": "🦘",
    "esports": "🎮",
    "golf": "⛳",
}

# Regions & markets to fetch
API_BASE = "https://api.the-odds-api.com/v4"
ODDS_ENDPOINT = f"{API_BASE}/sports/upcoming/odds/"

# =========================
# BOT
# =========================
intents = discord.Intents.default()
intents.message_content = True

class ValueBetsBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        # keep a single app command tree (prevents “already has an associated tree”)
        self.tree = app_commands.CommandTree(self)
        await self.tree.sync()

bot = ValueBetsBot()


# =========================
# DB HELPERS
# =========================
def get_db():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL, cursor_factory=pgx.RealDictCursor)

def init_db():
    conn = get_db()
    if not conn:
        return
    with conn:
        with conn.cursor() as cur:
            # system feed / paper trading
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bets (
                    id SERIAL PRIMARY KEY,
                    event_id TEXT,
                    bet_key TEXT UNIQUE,
                    match TEXT,
                    bookmaker TEXT,
                    team TEXT,
                    odds NUMERIC,
                    consensus NUMERIC,
                    implied NUMERIC,
                    edge NUMERIC,
                    bet_time TIMESTAMPTZ,
                    category TEXT,
                    sport TEXT,
                    league TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            # per-user clicks from buttons
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_bets (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    bet_key TEXT,
                    event_id TEXT,
                    sport TEXT,
                    league TEXT,
                    stake_type TEXT,     -- 'conservative' | 'smart' | 'aggressive'
                    stake_units NUMERIC,
                    odds NUMERIC,
                    result TEXT,         -- 'win' | 'loss' | 'push' | NULL (unsettled)
                    pnl NUMERIC,         -- realized P/L if result is set
                    settled_at TIMESTAMPTZ
                );
            """)
    conn.close()


# =========================
# UTILS
# =========================
def _is_allowed_bookmaker(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in ALLOWED_BOOKMAKER_KEYS)

def sport_label_and_emoji(sport_key: str, sport_title: str) -> tuple[str, str]:
    """
    sport_key examples from TheOddsAPI:
    'soccer_epl', 'cricket_international_t20', 'americanfootball_ncaaf'
    We'll map root segment before first '_' for emoji.
    """
    root = (sport_key or "").split("_", 1)[0].lower()
    # TheOdds often uses americanfootball; user wanted Soccer named distinctly
    # Keep our emoji map fallback:
    emoji = SPORT_EMOJIS.get(root, "🏆")
    # Title from API is a nice league title (e.g., "Cricket International Twenty20").
    label = sport_title or "Unknown League"
    return label, emoji

def percent(x: float) -> str:
    return f"{x:.2f}%"

def fmt_units(x: float) -> str:
    return f"{x:.2f} units"

def build_bet_key(sport_key: str, event_id: str, market_key: str, outcome_name: str, bookmaker: str) -> str:
    raw = f"{sport_key}|{event_id}|{market_key}|{outcome_name}|{bookmaker}"
    return raw.lower()

def choose_category(commence_dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    delta = commence_dt - now
    if delta <= timedelta(hours=48):
        return "quick"
    if delta <= timedelta(days=150):
        return "long"
    return "ignore"

def value_indicator(edge: float) -> str:
    return "🟢 Value Bet" if edge >= VALUE_EDGE_THRESHOLD else "🛑 Low Value"


# =========================
# ODDS FETCH & FORMAT
# =========================
async def fetch_events(session: aiohttp.ClientSession) -> list[dict]:
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,totals,spreads",
        "oddsFormat": "decimal"
    }
    try:
        async with session.get(ODDS_ENDPOINT, params=params, timeout=30) as resp:
            resp.raise_for_status()
            return await resp.json()
    except Exception as e:
        print("Odds API error:", e)
        return []

def compute_consensus_and_edges(event: dict) -> list[dict]:
    """Return a list of bet candidates with consensus/implied/edge etc."""
    home = event.get("home_team")
    away = event.get("away_team")
    match_name = f"{home} vs {away}"
    commence_time = event.get("commence_time")
    try:
        commence_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
    except Exception:
        return []

    category = choose_category(commence_dt)
    if category == "ignore":
        return []

    sport_key = event.get("sport_key", "")
    sport_title = event.get("sport_title", "")
    league_label, sport_emoji = sport_label_and_emoji(sport_key, sport_title)

    # Build global consensus across allowed bookmakers
    consensus_by_outcome: dict[str, list[float]] = defaultdict(list)
    for book in event.get("bookmakers", []):
        if not _is_allowed_bookmaker(book.get("title", "")):
            continue
        for market in book.get("markets", []):
            key = market.get("key")
            for outcome in market.get("outcomes", []):
                price = outcome.get("price")
                name = outcome.get("name")
                if not (price and name and price > 1):
                    continue
                outcome_key = f"{key}:{name}"
                consensus_by_outcome[outcome_key].append(1 / float(price))

    if not consensus_by_outcome:
        return []

    # average across all outcomes
    def mean(lst: list[float]) -> float:
        return sum(lst) / max(1, len(lst))

    # We’ll create candidates (one per bookmaker/outcome) with edge computed
    candidates = []
    for book in event.get("bookmakers", []):
        book_title = book.get("title", "Unknown")
        if not _is_allowed_bookmaker(book_title):
            continue
        for market in book.get("markets", []):
            mkey = market.get("key")
            for outcome in market.get("outcomes", []):
                price = outcome.get("price")
                name = outcome.get("name")
                if not (price and name and price > 1):
                    continue
                outcome_key = f"{mkey}:{name}"
                implied = 1 / float(price)
                consensus = mean(consensus_by_outcome.get(outcome_key, [implied]))
                edge = (consensus - implied) * 100.0  # %
                if edge <= 0:
                    continue

                bet_key = build_bet_key(sport_key, event.get("id", ""), mkey, name, book_title)
                candidates.append({
                    "event_id": event.get("id", ""),
                    "bet_key": bet_key,
                    "match": match_name,
                    "bookmaker": book_title,
                    "team": name,
                    "odds": float(price),
                    "consensus": consensus * 100.0,
                    "implied": implied * 100.0,
                    "edge": edge,
                    "bet_time": commence_dt,
                    "category": category,
                    "sport": sport_key,
                    "league": league_label,
                    "sport_emoji": sport_emoji,
                })
    return candidates


# =========================
# EMBEDS & BUTTONS
# =========================
def build_embed(title: str, b: dict) -> discord.Embed:
    indicator = value_indicator(b["edge"])
    # headline line with sport + league
    header = f"{b['sport_emoji']} {b['league']}"
    emb = discord.Embed(title=title, color=0x2ECC71 if b["edge"] >= VALUE_EDGE_THRESHOLD else 0xE67E22)
    emb.add_field(name=indicator, value=header, inline=False)

    body = (
        f"**Match:** {b['match']}\n"
        f"**Pick:** {b['team']} @ {b['odds']}\n"
        f"**Bookmaker:** {b['bookmaker']}\n"
        f"**Consensus %:** {percent(b['consensus'])}\n"
        f"**Implied %:** {percent(b['implied'])}\n"
        f"**Edge:** {percent(b['edge'])}\n"
        f"**Time:** {b['bet_time'].strftime('%d/%m/%y %H:%M')}\n\n"
    )
    # Unit stakes
    cons_units = round(BANKROLL_UNITS * CONSERVATIVE_PCT, 2)
    smart_units = round(cons_units * (1 + b['edge']/100.0), 2)
    aggr_units  = round(cons_units * (1 + 2*(b['edge']/100.0)), 2)

    cons_pay = round(cons_units * b['odds'], 2)
    smart_pay= round(smart_units * b['odds'], 2)
    aggr_pay = round(aggr_units * b['odds'], 2)

    cons_exp = round((b['consensus']/100.0) * cons_pay - cons_units, 2)
    smart_exp= round((b['consensus']/100.0) * smart_pay - smart_units, 2)
    aggr_exp = round((b['consensus']/100.0) * aggr_pay - aggr_units, 2)

    body += (
        f"💵 **Conservative Stake:** {fmt_units(cons_units)} → Payout: {cons_pay} | Exp. Profit: {cons_exp}\n"
        f"🧠 **Smart Stake:** {fmt_units(smart_units)} → Payout: {smart_pay} | Exp. Profit: {smart_exp}\n"
        f"🔥 **Aggressive Stake:** {fmt_units(aggr_units)} → Payout: {aggr_pay} | Exp. Profit: {aggr_exp}\n"
    )

    emb.description = body
    return emb

class StakeButtons(ui.View):
    def __init__(self, bet_key: str, event_id: str, sport: str, league: str, odds: float):
        super().__init__(timeout=None)
        payload = {
            "bet_key": bet_key,
            "event_id": event_id,
            "sport": sport,
            "league": league,
            "odds": odds,
        }
        data = json.dumps(payload)

        self.add_item(ui.Button(
            label="Conservative",
            style=ButtonStyle.secondary,
            emoji="💵",
            custom_id=f"stake:conservative:{data}"
        ))
        self.add_item(ui.Button(
            label="Smart",
            style=ButtonStyle.primary,
            emoji="🧠",
            custom_id=f"stake:smart:{data}"
        ))
        self.add_item(ui.Button(
            label="Aggressive",
            style=ButtonStyle.danger,
            emoji="🔥",
            custom_id=f"stake:aggressive:{data}"
        ))


@bot.event
async def on_interaction(inter: discord.Interaction):
    """
    Handle persistent button custom_ids stake:<type>:<json-payload>
    """
    try:
        cid = inter.data.get("custom_id", "")
    except Exception:
        return
    if not cid.startswith("stake:"):
        return
    if inter.user is None:
        return

    _, stake_type, payload = cid.split(":", 2)
    try:
        payload = json.loads(payload)
    except Exception:
        await inter.response.send_message("Invalid data on this bet.", ephemeral=True)
        return

    bet_key = payload.get("bet_key")
    event_id = payload.get("event_id")
    sport = payload.get("sport")
    league = payload.get("league")
    odds = float(payload.get("odds", 0))

    # compute units like in embed
    cons_units = round(BANKROLL_UNITS * CONSERVATIVE_PCT, 2)
    if stake_type == "conservative":
        stake_units = cons_units
    elif stake_type == "smart":
        stake_units = round(cons_units * 1.5, 2)
    else:
        stake_units = round(cons_units * 2.0, 2)

    # save to DB
    try:
        conn = get_db()
        if not conn:
            await inter.response.send_message("❌ Could not save your bet. Is the database configured?", ephemeral=True)
            return
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_bets (user_id, username, bet_key, event_id, sport, league, stake_type, stake_units, odds)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (inter.user.id, str(inter.user), bet_key, event_id, sport, league, stake_type, stake_units, odds))
        conn.close()
        await inter.response.send_message(
            f"✅ Saved your **{stake_type}** bet ({fmt_units(stake_units)}).",
            ephemeral=True
        )
    except Exception as e:
        await inter.response.send_message(f"❌ Could not save your bet. Is the database configured?\n`{e}`", ephemeral=True)


# =========================
# POSTING PIPELINE
# =========================
async def post_bet_to_channels(b: dict):
    """
    Sends embeds + buttons to appropriate channels.
    Duplicates *value bets* to the dedicated duplicate channel as well.
    """
    # Best bet goes to BEST channel; Quick to QUICK; Long to LONG
    title_map = {
        "quick": "⏱ Quick Return Bet",
        "long": "📅 Longer Play Bet",
        "best": "⭐ Best Bet"
    }

    emb = build_embed(title_map.get(b.get("category"), "Bet"), b)
    view = StakeButtons(b["bet_key"], b["event_id"], b["sport"], b["league"], b["odds"])

    chan_id = CHAN_BEST if b["category"] == "best" else (CHAN_QUICK if b["category"] == "quick" else CHAN_LONG)
    channel = bot.get_channel(chan_id)
    if channel:
        await channel.send(embed=emb, view=view)

    # Duplicate Value Bets to testing channel
    if b["edge"] >= VALUE_EDGE_THRESHOLD and CHAN_VALUE_DUP:
        dup = bot.get_channel(CHAN_VALUE_DUP)
        if dup:
            await dup.send(embed=emb, view=view)

def pick_best_bet(candidates: list[dict]) -> dict | None:
    """
    Choose a single best bet that is not low-value.
    Strategy: max edge subject to BEST_EDGE_MIN.
    """
    good = [c for c in candidates if c["edge"] >= max(BEST_EDGE_MIN, VALUE_EDGE_THRESHOLD)]
    if not good:
        return None
    best = max(good, key=lambda x: (x["edge"], x["consensus"]))
    best = dict(best)
    best["category"] = "best"
    return best

def upsert_bet_row(b: dict):
    conn = get_db()
    if not conn:
        return
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bets (event_id, bet_key, match, bookmaker, team, odds, consensus, implied, edge, bet_time, category, sport, league)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (bet_key) DO UPDATE SET
                    consensus = EXCLUDED.consensus,
                    implied = EXCLUDED.implied,
                    edge = EXCLUDED.edge,
                    category = EXCLUDED.category,
                    sport = EXCLUDED.sport,
                    league = EXCLUDED.league
            """, (
                b["event_id"], b["bet_key"], b["match"], b["bookmaker"], b["team"], b["odds"],
                b["consensus"], b["implied"], b["edge"], b["bet_time"], b["category"], b["sport"], b["league"]
            ))
    conn.close()

async def process_and_post():
    if not bot.session:
        bot.session = aiohttp.ClientSession()

    events = await fetch_events(bot.session)
    all_candidates: list[dict] = []
    for ev in events:
        all_candidates += compute_consensus_and_edges(ev)

    # Pick best and post
    best = pick_best_bet(all_candidates)
    if best:
        upsert_bet_row(best)
        await post_bet_to_channels(best)

    # Post top (up to a few) quick + long
    quicks = [c for c in all_candidates if c["category"] == "quick"]
    longs  = [c for c in all_candidates if c["category"] == "long"]

    # sort by edge desc
    quicks.sort(key=lambda x: x["edge"], reverse=True)
    longs.sort(key=lambda x: x["edge"], reverse=True)

    for b in quicks[:5]:
        upsert_bet_row(b)
        await post_bet_to_channels(b)

    for b in longs[:5]:
        upsert_bet_row(b)
        await post_bet_to_channels(b)


# =========================
# LOOPS & COMMANDS
# =========================
@tasks.loop(seconds=60)
async def bet_loop():
    try:
        await process_and_post()
    except Exception as e:
        print("bet_loop error:", e)

@bot.event
async def on_ready():
    if not bot.session:
        bot.session = aiohttp.ClientSession()
    init_db()
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    if not bet_loop.is_running():
        bet_loop.start()


# -------- /ping
@bot.tree.command(name="ping", description="Check if the bot is alive.")
async def ping_cmd(inter: discord.Interaction):
    await inter.response.send_message(f"Pong! `{round(bot.latency*1000)} ms`", ephemeral=True)

# -------- /


