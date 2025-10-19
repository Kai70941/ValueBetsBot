import os
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import discord
from discord.ext import commands, tasks

# HTTP & DB
import requests
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # psycopg2 optional at import time; we gate all DB usage later
    psycopg2 = None

logging.basicConfig(level=logging.INFO)

# ========= ENV =========
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

BEST_CH = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_CH = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_CH = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
VALUE_CH = int(os.getenv("VALUE_BETS_CHANNEL_ID", "0"))

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DB_URL = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL")

# ========= CONFIG =========
BANKROLL_UNITS = 1000  # “units” bank (not dollars)
CONSERVATIVE_UNIT_PCT = 0.015   # 1.5% of bank as base unit size
# “smart” stake is Kelly-ish light: proportional to edge
SMART_MULTIPLIER_PER_EDGE = 0.15  # 15% of con stake per 1% edge (tunable)
AGG_MULTIPLIER_BASE = 1.1         # agg stake = con * (1 + edge% * factor)

VALUE_EDGE_THRESHOLD = 2.0  # % edge required to call something “Value”
QUICK_WINDOW_HRS = 48
LONG_WINDOW_DAYS = 150

COMMAND_SYNC_ON_READY = True

# Allowed bookmaker filter (lowercase contains)
ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
]

# Sport emojis & friendly names
SPORT_MAP = {
    "soccer": ("⚽", "Soccer"),
    "americanfootball": ("🏈", "American Football"),
    "basketball": ("🏀", "Basketball"),
    "icehockey": ("🏒", "Ice Hockey"),
    "baseball": ("⚾", "Baseball"),
    "golf": ("⛳", "Golf"),
    "mma": ("🥊", "MMA"),
    "boxing": ("🥊", "Boxing"),
    "tennis": ("🎾", "Tennis"),
    "tabletennis": ("🏓", "Table Tennis"),
    "aussierules": ("🏉", "Aussie Rules"),
    "cricket": ("🏏", "Cricket"),
    "rugby": ("🏉", "Rugby"),
    "volleyball": ("🏐", "Volleyball"),
    "esports": ("🎮", "Esports"),
}

# ========= Discord Setup =========
intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

posted_bets = set()  # dedupe across channels


# ========= DB HELPERS (with sslmode=require) =========
def _dsn_with_ssl(url: str) -> str:
    if not url:
        return url
    if "sslmode=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}sslmode=require"

def get_conn():
    if not DB_URL or not psycopg2:
        raise RuntimeError("DB not configured")
    dsn = _dsn_with_ssl(DB_URL)
    return psycopg2.connect(dsn)

def init_db():
    if not DB_URL or not psycopg2:
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bets (
                        id SERIAL PRIMARY KEY,
                        event_id TEXT,
                        sport_key TEXT,
                        match TEXT,
                        bookmaker TEXT,
                        team TEXT,
                        odds NUMERIC,
                        edge NUMERIC,
                        bet_time TIMESTAMP,
                        category TEXT,
                        sport TEXT,
                        league TEXT,
                        created_at TIMESTAMP DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_bets (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        username TEXT,
                        bet_key TEXT NOT NULL,
                        event_id TEXT,
                        sport_key TEXT,
                        match TEXT,
                        team TEXT,
                        bookmaker TEXT,
                        odds NUMERIC,
                        strategy TEXT,         -- conservative / smart / aggressive
                        stake_units NUMERIC,   -- amount in units
                        placed_at TIMESTAMP DEFAULT NOW(),
                        result TEXT,           -- win / lose / push / void
                        return_units NUMERIC,  -- payout (units) minus stake (units)
                        settled_at TIMESTAMP
                    );
                """)
    except Exception as e:
        logging.error(f"DB init error: {e}")

def save_bet_to_db(b: dict, category: str):
    """Write discovered bet into bets table (fire-and-forget)."""
    if not DB_URL or not psycopg2:
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO bets
                       (event_id,sport_key,match,bookmaker,team,odds,edge,bet_time,category,sport,league)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        b.get("event_id"), b.get("sport_key"), b.get("match"),
                        b.get("bookmaker"), b.get("team"), b.get("odds"),
                        b.get("edge"), b.get("time"), category,
                        b.get("sport"), b.get("league"),
                    )
                )
    except Exception as e:
        logging.error(f"DB save error: {e}")

def log_user_bet(user: discord.User, b: dict, strategy: str, units: float) -> bool:
    """Store 'I Placed' actions from users into user_bets."""
    if not DB_URL or not psycopg2:
        return False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_bets
                    (user_id,username,bet_key,event_id,sport_key,match,team,bookmaker,odds,strategy,stake_units)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    user.id, f"{user.name}#{user.discriminator}", bet_identity(b),
                    b.get("event_id"), b.get("sport_key"), b.get("match"),
                    b.get("team"), b.get("bookmaker"), b.get("odds"),
                    strategy, units,
                ))
        return True
    except Exception as e:
        logging.error(f"user_bets insert error: {e}")
        return False


# ========= Odds Logic =========
def bookmaker_allowed(title: str) -> bool:
    return any(key in (title or "").lower() for key in ALLOWED_BOOKMAKER_KEYS)

def fetch_odds():
    """Pull upcoming odds from TheOddsAPI. You’re on the paid plan, so 100k calls/month."""
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal"
    }
    try:
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error(f"Odds API error: {e}")
        return []

def calc_bets(data):
    """Calculate value bets: build consensus across allowed books, compute edge, stakes."""
    now = datetime.now(timezone.utc)
    bets = []

    for ev in data:
        home, away = ev.get("home_team"), ev.get("away_team")
        match = f"{home} vs {away}"
        event_id = ev.get("id") or ev.get("event_id")
        sport_key = (ev.get("sport_key") or "").lower()
        sport_title = ev.get("sport_title") or sport_key
        league = sport_title  # TheOddsAPI doesn't give sub-league reliably; show sport_title

        commence_time = ev.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
        except Exception:
            continue

        delta = commence_dt - now
        if delta.total_seconds() <= 0 or delta > timedelta(days=LONG_WINDOW_DAYS):
            continue

        # Build consensus (avg of implied probabilities) among allowed books
        consensus_by_outcome = defaultdict(list)
        book_count = 0
        for book in ev.get("bookmakers", []):
            if not bookmaker_allowed(book.get("title", "")):
                continue
            book_count += 1
            for m in book.get("markets", []):
                for out in m.get("outcomes", []):
                    price = out.get("price")
                    name = out.get("name")
                    if not price or not name:
                        continue
                    key = f"{m['key']}:{name}"
                    consensus_by_outcome[key].append(1.0 / float(price))

        if not consensus_by_outcome or book_count == 0:
            continue

        # Global consensus fallback (avg of all individual implieds)
        flat_probs = [p for plist in consensus_by_outcome.values() for p in plist]
        global_consensus = sum(flat_probs) / max(1, len(flat_probs))

        for book in ev.get("bookmakers", []):
            if not bookmaker_allowed(book.get("title", "")):
                continue
            for m in book.get("markets", []):
                for out in m.get("outcomes", []):
                    price = out.get("price")
                    name = out.get("name")
                    if not price or not name:
                        continue

                    implied = 1.0 / float(price)
                    k = f"{m['key']}:{name}"
                    if k in consensus_by_outcome:
                        consensus = sum(consensus_by_outcome[k]) / len(consensus_by_outcome[k])
                    else:
                        consensus = global_consensus

                    edge = (consensus - implied) * 100.0  # %
                    # Unit stakes
                    con_units = round(BANKROLL_UNITS * CONSERVATIVE_UNIT_PCT, 2)
                    smart_units = round(con_units * (1.0 + (max(0.0, edge) * SMART_MULTIPLIER_PER_EDGE / 1.0)), 2)
                    agg_units = round(con_units * (AGG_MULTIPLIER_BASE + max(0.0, edge) / 100.0), 2)

                    cons_payout = round(con_units * float(price), 2)
                    smart_payout = round(smart_units * float(price), 2)
                    agg_payout = round(agg_units * float(price), 2)

                    # Expected profit (in units); use consensus probability
                    exp_cons = round(consensus * cons_payout - con_units, 2)
                    exp_smart = round(consensus * smart_payout - smart_units, 2)
                    exp_agg = round(consensus * agg_payout - agg_units, 2)

                    is_quick = delta <= timedelta(hours=QUICK_WINDOW_HRS)
                    is_long = not is_quick

                    bets.append({
                        "event_id": event_id,
                        "sport_key": sport_key,
                        "sport": SPORT_MAP.get(sport_key, ("🏟️", sport_title))[1],
                        "league": league,
                        "emoji": SPORT_MAP.get(sport_key, ("🏟️", sport_title))[0],

                        "match": match,
                        "bookmaker": book.get("title", "Unknown Bookmaker"),
                        "team": name,
                        "odds": float(price),
                        "probability": round(implied * 100.0, 2),
                        "consensus": round(consensus * 100.0, 2),
                        "edge": round(edge, 2),
                        "time": commence_dt,

                        # stakes in UNITS (no $ sign)
                        "con_units": con_units,
                        "smart_units": smart_units,
                        "agg_units": agg_units,
                        "con_payout": cons_payout,
                        "smart_payout": smart_payout,
                        "agg_payout": agg_payout,
                        "exp_cons": exp_cons,
                        "exp_smart": exp_smart,
                        "exp_agg": exp_agg,

                        "quick_return": is_quick,
                        "long_play": is_long,
                    })

    return bets


# ========= Formatting / UI =========
def value_indicator(edge: float) -> str:
    return "🟢 Value Bet" if edge >= VALUE_EDGE_THRESHOLD else "🔴 Low Value"

def bet_identity(b: dict) -> str:
    return f"{b.get('match')}|{b.get('team')}|{b.get('bookmaker')}|{b.get('time')}"

def embed_bet(b: dict, title: str, color: int) -> discord.Embed:
    # Header with sport + league
    league_label = b.get("league") or "Unknown League"
    header = f"{b.get('emoji','🏟️')} {b.get('sport')} ({league_label})"

    desc = (
        f"**{header}**\n\n"
        f"**Match:** {b['match']}\n"
        f"**Pick:** {b['team']} @ {b['odds']}\n"
        f"**Bookmaker:** {b['bookmaker']}\n"
        f"**Consensus %:** {b['consensus']}%\n"
        f"**Implied %:** {b['probability']}%\n"
        f"**Edge:** {b['edge']}%\n"
        f"**Time:** {b['time'].strftime('%d/%m/%y %H:%M')}\n\n"
        # units (no $)
        f"🧾 **Conservative Stake:** {b['con_units']}u → Payout: {b['con_payout']}u | Exp. Profit: {b['exp_cons']}u\n"
        f"🧠 **Smart Stake:** {b['smart_units']}u → Payout: {b['smart_payout']}u | Exp. Profit: {b['exp_smart']}u\n"
        f"🔥 **Aggressive Stake:** {b['agg_units']}u → Payout: {b['agg_payout']}u | Exp. Profit: {b['exp_agg']}u\n"
    )
    e = discord.Embed(title=title, description=desc, color=color)
    return e

class PlaceBetModal(discord.ui.Modal, title="I Placed This Bet"):
    stake_units = discord.ui.TextInput(
        label="Stake (units)",
        placeholder="e.g., 5",
        min_length=1, max_length=12, required=True
    )
    strategy = discord.ui.TextInput(
        label="Strategy (conservative / smart / aggressive)",
        placeholder="conservative",
        min_length=4, max_length=12, required=True
    )

    def __init__(self, bet_payload: dict):
        super().__init__(timeout=180)
        self.bet_payload = bet_payload

    async def on_submit(self, interaction: discord.Interaction):
        try:
            units = float(str(self.stake_units.value).strip())
        except Exception:
            await interaction.response.send_message("Enter a valid number for units.", ephemeral=True)
            return
        strat = str(self.strategy.value).strip().lower()
        if strat not in ("conservative", "smart", "aggressive"):
            await interaction.response.send_message("Strategy must be conservative / smart / aggressive.", ephemeral=True)
            return

        ok = log_user_bet(interaction.user, self.bet_payload, strat, units)
        if ok:
            await interaction.response.send_message(f"✅ Logged {units}u on **{self.bet_payload.get('team')}** ({strat}).", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Could not save your bet. Is the database configured?", ephemeral=True)

class BetView(discord.ui.View):
    def __init__(self, bet_payload: dict):
        super().__init__(timeout=None)
        self.bet_payload = bet_payload

    @discord.ui.button(label="I Placed", style=discord.ButtonStyle.success, emoji="📝", custom_id="placed_btn")
    async def place_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PlaceBetModal(self.bet_payload))


# ========= Posting =========
async def post_bets(bets):
    """Post best, quick, long, and duplicate value bets to VALUE_CH."""
    if not bets:
        return

    # Only choose a “Best Bet” from bets that are Value (edge >= threshold)
    value_bets = [b for b in bets if b["edge"] >= VALUE_EDGE_THRESHOLD]
    best = max(value_bets, key=lambda x: (x["consensus"], x["edge"]), default=None)

    # Quick / Long lists (value or not, but we’ll show label in title)
    quick = [b for b in bets if b["quick_return"]]
    long_ = [b for b in bets if b["long_play"]]

    # BEST
    if best and bet_identity(best) not in posted_bets and BEST_CH:
        posted_bets.add(bet_identity(best))
        ch = bot.get_channel(BEST_CH)
        if ch:
            title = "⭐ Best Bet"
            color = 0xFFD700
            header = value_indicator(best['edge'])
            em = embed_bet(best, title, color)
            em.insert_field_at(0, name="Indicator", value=header, inline=False)
            view = BetView(best)
            await ch.send(embed=em, view=view)
        save_bet_to_db(best, "best")

    # QUICK
    if QUICK_CH:
        ch = bot.get_channel(QUICK_CH)
        for b in quick[:8]:
            if bet_identity(b) in posted_bets:
                continue
            posted_bets.add(bet_identity(b))
            title = "⏱ Quick Return Bet"
            color = (0x2ECC71 if b["edge"] >= VALUE_EDGE_THRESHOLD else 0x95A5A6)
            em = embed_bet(b, title, color)
            em.insert_field_at(0, name="Indicator", value=value_indicator(b['edge']), inline=False)
            view = BetView(b)
            await ch.send(embed=em, view=view)
            save_bet_to_db(b, "quick")

    # LONG
    if LONG_CH:
        ch = bot.get_channel(LONG_CH)
        for b in long_[:8]:
            if bet_identity(b) in posted_bets:
                continue
            posted_bets.add(bet_identity(b))
            title = "📅 Longer Play Bet"
            color = (0x3498DB if b["edge"] >= VALUE_EDGE_THRESHOLD else 0x95A5A6)
            em = embed_bet(b, title, color)
            em.insert_field_at(0, name="Indicator", value=value_indicator(b['edge']), inline=False)
            view = BetView(b)
            await ch.send(embed=em, view=view)
            save_bet_to_db(b, "long")

    # Duplicate **value** bets to VALUE_CH
    if VALUE_CH:
        ch = bot.get_channel(VALUE_CH)
        for b in value_bets[:10]:
            # We want duplicates even if posted elsewhere — still dedupe on identity for VALUE feed
            key = f"value|{bet_identity(b)}"
            if key in posted_bets:
                continue
            posted_bets.add(key)
            title = "🟢 Value Bet (Testing)"
            color = 0x2ECC71
            em = embed_bet(b, title, color)
            view = BetView(b)
            await ch.send(embed=em, view=view)
            save_bet_to_db(b, "value")


# ========= Commands =========
@bot.tree.command(name="ping", description="Report bot latency.")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency*1000)} ms", ephemeral=True)

@bot.tree.command(name="dbcheck", description="Verify database connectivity & tables.")
async def dbcheck_cmd(interaction: discord.Interaction):
    if not DB_URL or not psycopg2:
        await interaction.response.send_message("DB not configured (missing URL or psycopg2).", ephemeral=True)
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
        await interaction.response.send_message("✅ DB connection OK (sslmode=require).", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ DB error: {e}", ephemeral=True)

@bot.tree.command(name="fetchbets", description="Preview a few incoming bets now (ephemeral).")
async def fetchbets_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = fetch_odds()
    bets = calc_bets(data)[:5]
    if not bets:
        await interaction.followup.send("No bets right now.", ephemeral=True)
        return

    lines = []
    for b in bets:
        lines.append(f"**{b['match']}** — {b['team']} @ {b['odds']} ({b['bookmaker']}) | Edge: {b['edge']}%")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="roi", description="Compute all-time ROI from settled user bets.")
async def roi_cmd(interaction: discord.Interaction):
    if not DB_URL or not psycopg2:
        await interaction.response.send_message("DB not configured.", ephemeral=True)
        return
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT COALESCE(SUM(stake_units),0) AS stake,
                           COALESCE(SUM(return_units),0) AS ret
                    FROM user_bets
                    WHERE result IS NOT NULL
                """)
                row = cur.fetchone() or {"stake": 0, "ret": 0}
        stake = float(row["stake"] or 0)
        ret = float(row["ret"] or 0)  # return_units already = payout - stake
        roi = 0.0 if stake <= 0 else (ret / stake) * 100.0
        await interaction.response.send_message(
            f"📈 ROI (all strategies, settled only): **{roi:.2f}%**\n"
            f"Stake: {stake:.2f}u | Net: {ret:.2f}u",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(f"ROI error: {e}", ephemeral=True)


# ========= Lifecycle =========
@bot.event
async def on_ready():
    logging.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    init_db()
    if COMMAND_SYNC_ON_READY:
        try:
            await bot.tree.sync()
            logging.info("✅ Slash commands synced.")
        except Exception as e:
            logging.error(f"Slash sync failed: {e}")
    if not bet_loop.is_running():
        bet_loop.start()

@tasks.loop(seconds=45)
async def bet_loop():
    data = fetch_odds()
    bets = calc_bets(data)
    try:
        await post_bets(bets)
    except Exception as e:
        logging.error(f"Post bets error: {e}")


# ========= Start =========
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_BOT_TOKEN")
    bot.run(TOKEN)

















