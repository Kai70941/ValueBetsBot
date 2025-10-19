# bot.py
import os
import re
import math
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import requests
import discord
from discord.ext import commands, tasks

# --- Optional DB (paper trading & user logs) ---
DB_URL = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL")
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:
    psycopg2 = None
    RealDictCursor = None

# =======================
# Config from env
# =======================
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
BEST_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_RETURNS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_PLAYS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
VALUE_BETS_CHANNEL_ID = int(os.getenv("VALUE_BETS_CHANNEL_ID", "0"))
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

# Bankroll (units, not dollars)
BANKROLL_UNITS = 1000
CONSERVATIVE_PCT = 0.015  # ~1.5% per bet ≈ 15u default
SMART_BASE_UNITS = 5

# Thresholds
EDGE_VALUE_THRESHOLD = 2.0  # % pts edge to call "Value"

# =======================
# Discord setup
# =======================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Track posted bets per channel (avoid duplicate sends)
posted_by_channel = {
    "quick": set(),
    "long": set(),
    "best": set(),
    "value": set(),
}

# =======================
# Bookmaker allow-list (exact 9)
# =======================
ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab",
]
def _allowed_bookmaker(title: str) -> bool:
    if not title:
        return False
    t = re.sub(r"[^a-z0-9]+", "", title.lower())  # normalize e.g. 'Betfair Exchange' -> 'betfairexchange'
    return any(k in t for k in ALLOWED_BOOKMAKER_KEYS)

# =======================
# Helpers
# =======================
def sport_from_title(sport_title: str) -> str:
    if not sport_title:
        return "Unknown"
    s = sport_title.lower()
    if "soccer" in s or "football (soccer)" in s:
        return "Soccer"
    if "american football" in s or "nfl" in s or "ncaa football" in s:
        return "American Football"
    if "basketball" in s:
        return "Basketball"
    if "tennis" in s:
        return "Tennis"
    if "baseball" in s or "mlb" in s:
        return "Baseball"
    if "ice hockey" in s or "nhl" in s:
        return "Ice Hockey"
    if "mma" in s or "ufc" in s:
        return "MMA"
    if "cricket" in s:
        return "Cricket"
    if "rugby" in s:
        return "Rugby"
    if "aussie rules" in s or "afl" in s:
        return "Aussie Rules"
    return sport_title

SPORT_EMOJI = {
    "Soccer": "⚽",
    "American Football": "🏈",
    "Basketball": "🏀",
    "Tennis": "🎾",
    "Baseball": "⚾",
    "Ice Hockey": "🏒",
    "MMA": "🥊",
    "Cricket": "🏏",
    "Rugby": "🏉",
    "Aussie Rules": "🏉",
}
def sport_emoji(name: str) -> str:
    return SPORT_EMOJI.get(name, "🎲")

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def bet_key(b: dict) -> str:
    # keep the same identity you were using
    return f"{b.get('match')}|{b.get('team')}|{b.get('bookmaker')}|{b.get('time')}"

# =======================
# DB init (bets & user_bets)
# =======================
def init_db():
    if not DB_URL or not psycopg2:
        return
    try:
        with psycopg2.connect(DB_URL) as conn:
            with conn.cursor() as cur:
                # existing paper-trade table (kept)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS bets (
                    id SERIAL PRIMARY KEY,
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
                # NEW: user_bets (who placed which bet & stake; result can be settled later)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS user_bets (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    bet_key TEXT NOT NULL,
                    match TEXT,
                    team TEXT,
                    bookmaker TEXT,
                    odds NUMERIC,
                    strategy TEXT,           -- conservative | smart | aggressive
                    stake_units NUMERIC,
                    placed_at TIMESTAMP DEFAULT NOW(),
                    result TEXT,             -- win | loss | push | NULL (unsettled)
                    return_units NUMERIC,    -- stake back or 0 or odds*stake
                    settled_at TIMESTAMP
                );
                """)
        # no commit needed with context manager
    except Exception as e:
        logging.error(f"DB init error: {e}")

def save_bet_to_db(b: dict, category: str):
    # (kept original paper-trade logging for system tracking)
    if not DB_URL or not psycopg2:
        return
    try:
        with psycopg2.connect(DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bets (match, bookmaker, team, odds, edge, bet_time, category, sport, league)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        b.get("match"),
                        b.get("bookmaker"),
                        b.get("team"),
                        b.get("odds"),
                        b.get("edge"),
                        b.get("time"),
                        category,
                        b.get("sport"),
                        b.get("league"),
                    )
                )
    except Exception as e:
        logging.error(f"DB save error: {e}")

def log_user_bet(user: discord.User, b: dict, strategy: str, units: float):
    """Insert a user-submitted bet placement."""
    if not DB_URL or not psycopg2:
        return False
    try:
        with psycopg2.connect(DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_bets (user_id, username, bet_key, match, team, bookmaker, odds, strategy, stake_units)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    user.id,
                    f"{user.name}#{user.discriminator}",
                    bet_key(b),
                    b.get("match"),
                    b.get("team"),
                    b.get("bookmaker"),
                    b.get("odds"),
                    strategy,
                    units,
                ))
        return True
    except Exception as e:
        logging.error(f"user_bets insert error: {e}")
        return False

# =======================
# Odds fetch & bet building
# =======================
def fetch_odds():
    if not ODDS_API_KEY:
        logging.warning("ODDS_API_KEY missing.")
        return []
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error(f"Odds API error: {e}")
        return []

def calculate_bets(data):
    now = datetime.now(timezone.utc)
    bets = []
    for event in data:
        home, away = event.get("home_team"), event.get("away_team")
        if not home or not away:
            continue
        match_name = f"{home} vs {away}"

        commence_time = event.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        except Exception:
            continue

        delta = commence_dt - now
        if delta.total_seconds() <= 0 or delta > timedelta(days=150):
            continue

        # consensus across only allowed books
        consensus_by_outcome = defaultdict(list)
        for book in event.get("bookmakers", []):
            btitle = (book.get("title") or "").strip()
            if not _allowed_bookmaker(btitle):
                continue
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    if outcome.get("price") and outcome.get("name"):
                        key = f"{market['key']}:{outcome['name']}"
                        try:
                            price = float(outcome["price"])
                            if price <= 1.0:
                                continue
                            consensus_by_outcome[key].append(1.0/price)
                        except Exception:
                            continue

        if not consensus_by_outcome:
            continue

        global_consensus = (
            sum(p for lst in consensus_by_outcome.values() for p in lst) /
            max(1, sum(len(lst) for lst in consensus_by_outcome.values()))
        )

        # sport/league line
        sport_title = event.get("sport_title") or ""
        sport = sport_from_title(sport_title)
        league = sport_title or "Unknown League"

        # build bets (only allowed bookmakers)
        for book in event.get("bookmakers", []):
            btitle = (book.get("title") or "Unknown Bookmaker").strip()
            if not _allowed_bookmaker(btitle):
                continue
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    price = safe_float(outcome.get("price"), 0.0)
                    name  = outcome.get("name")
                    if not name or price <= 1.0:
                        continue

                    implied_p = 1.0 / price
                    outcome_key = f"{market['key']}:{name}"
                    consensus_p = (
                        sum(consensus_by_outcome[outcome_key]) / len(consensus_by_outcome[outcome_key])
                        if outcome_key in consensus_by_outcome else global_consensus
                    )
                    edge = (consensus_p - implied_p) * 100.0
                    if edge <= 0:
                        continue

                    # stakes (units)
                    cons_units = round(BANKROLL_UNITS * CONSERVATIVE_PCT, 2)         # ~15u
                    smart_units = round(max(1.0, min(100.0, SMART_BASE_UNITS * (1.0 + edge / 5.0))), 2)
                    aggr_units  = round(cons_units * (1.0 + edge / 10.0), 2)

                    cons_payout  = round(cons_units  * price, 2)
                    smart_payout = round(smart_units * price, 2)
                    aggr_payout  = round(aggr_units  * price, 2)

                    cons_exp = round(consensus_p * cons_payout  - cons_units, 2)
                    smart_exp= round(consensus_p * smart_payout - smart_units, 2)
                    aggr_exp = round(consensus_p * aggr_payout  - aggr_units, 2)

                    bets.append({
                        "match": match_name,
                        "bookmaker": btitle,
                        "team": name,
                        "odds": price,
                        "time": commence_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "probability": round(implied_p * 100.0, 2),
                        "consensus": round(consensus_p * 100.0, 2),
                        "edge": round(edge, 2),

                        "cons_units": cons_units,
                        "smart_units": smart_units,
                        "aggr_units": aggr_units,

                        "cons_payout": cons_payout,
                        "smart_payout": smart_payout,
                        "aggr_payout": aggr_payout,

                        "cons_exp_profit": cons_exp,
                        "smart_exp_profit": smart_exp,
                        "aggr_exp_profit": aggr_exp,

                        "quick_return": delta <= timedelta(hours=48),
                        "long_play": timedelta(hours=48) < delta <= timedelta(days=150),

                        "is_value": edge >= EDGE_VALUE_THRESHOLD,

                        "sport": sport,
                        "league": league,
                    })
    return bets

# =======================
# UI (embeds + buttons + modal)
# =======================
def format_bet(b: dict, title: str, color: int) -> discord.Embed:
    indicator = "🟢 Value Bet" if b.get("is_value") else "🔴 Low Value"
    s_emoji = sport_emoji(b.get("sport", ""))
    sport_line = f"{s_emoji} {b.get('sport','Unknown')} ({b.get('league','Unknown League')})"

    desc = (
        f"{indicator}\n\n"
        f"**{sport_line}**\n\n"
        f"**Match:** {b['match']}\n"
        f"**Pick:** {b['team']} @ {b['odds']}\n"
        f"**Bookmaker:** {b['bookmaker']}\n"
        f"**Consensus %:** {b['consensus']}%\n"
        f"**Implied %:** {b['probability']}%\n"
        f"**Edge:** {b['edge']}%\n"
        f"**Time:** {b['time']}\n\n"
        f"💵 **Conservative Stake:** {b['cons_units']:.2f}u → Payout: {b['cons_payout']:.2f}u | Exp. Profit: {b['cons_exp_profit']:.2f}u\n"
        f"🧠 **Smart Stake:** {b['smart_units']:.2f}u → Payout: {b['smart_payout']:.2f}u | Exp. Profit: {b['smart_exp_profit']:.2f}u\n"
        f"🔥 **Aggressive Stake:** {b['aggr_units']:.2f}u → Payout: {b['aggr_payout']:.2f}u | Exp. Profit: {b['aggr_exp_profit']:.2f}u\n"
    )
    return discord.Embed(title=title, description=desc, color=color)

class StakeModal(discord.ui.Modal, title="Log Bet – Units"):
    units = discord.ui.TextInput(label="Units placed", placeholder="e.g. 10", default="10", required=True)

    def __init__(self, user: discord.User, bet: dict, strategy: str):
        super().__init__(timeout=120)
        self.user = user
        self.bet = bet
        self.strategy = strategy

        # Prefill default units by strategy
        default_units = {
            "conservative": f"{bet.get('cons_units', 10)}",
            "smart": f"{bet.get('smart_units', 10)}",
            "aggressive": f"{bet.get('aggr_units', 10)}",
        }.get(strategy, "10")
        self.units.default = default_units

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = float(str(self.units.value).strip())
            if amt <= 0:
                raise ValueError
        except Exception:
            await interaction.response.send_message("Please enter a valid positive number for units.", ephemeral=True)
            return

        ok = log_user_bet(interaction.user, self.bet, self.strategy, amt)
        if ok:
            await interaction.response.send_message(
                f"✅ Logged **{amt:.2f}u** on **{self.bet['team']} @ {self.bet['odds']}** ({self.strategy}).",
                ephemeral=True
            )
        else:
            await interaction.response.send_message("❌ Could not save your bet. Is the database configured?", ephemeral=True)

class BetActions(discord.ui.View):
    def __init__(self, bet: dict):
        super().__init__(timeout=None)  # keep buttons active
        self.bet = bet

    @discord.ui.button(label="I Placed (Conservative)", style=discord.ButtonStyle.secondary, emoji="💵")
    async def btn_cons(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StakeModal(interaction.user, self.bet, "conservative"))

    @discord.ui.button(label="I Placed (Smart)", style=discord.ButtonStyle.primary, emoji="🧠")
    async def btn_smart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StakeModal(interaction.user, self.bet, "smart"))

    @discord.ui.button(label="I Placed (Aggressive)", style=discord.ButtonStyle.danger, emoji="🔥")
    async def btn_aggr(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StakeModal(interaction.user, self.bet, "aggressive"))

# =======================
# Value channel helpers
# =======================
async def get_value_channel():
    if not VALUE_BETS_CHANNEL_ID:
        return None
    ch = bot.get_channel(VALUE_BETS_CHANNEL_ID)
    if ch is None:
        try:
            ch = await bot.fetch_channel(VALUE_BETS_CHANNEL_ID)
        except Exception as e:
            print(f"⚠️ fetch_channel failed for VALUE_BETS_CHANNEL_ID={VALUE_BETS_CHANNEL_ID}: {e}")
            ch = None
    return ch

async def duplicate_to_value_channel(b: dict, embed: discord.Embed):
    if not b.get("is_value") or not VALUE_BETS_CHANNEL_ID:
        return
    k = bet_key(b)
    if k in posted_by_channel["value"]:
        return
    vchan = await get_value_channel()
    if not vchan:
        print("⚠️ Value Bets channel not found or no permission.")
        return
    await vchan.send(embed=embed, view=BetActions(b))
    posted_by_channel["value"].add(k)

# =======================
# Posting flow
# =======================
async def post_bets(bets):
    if not bets:
        return

    best = max(bets, key=lambda x: (x["consensus"], x["edge"])) if bets else None

    bchan = bot.get_channel(BEST_BETS_CHANNEL) if BEST_BETS_CHANNEL else None
    qchan = bot.get_channel(QUICK_RETURNS_CHANNEL) if QUICK_RETURNS_CHANNEL else None
    lchan = bot.get_channel(LONG_PLAYS_CHANNEL) if LONG_PLAYS_CHANNEL else None

    # Best
    if best and bchan:
        k = bet_key(best)
        if k not in posted_by_channel["best"]:
            embed = format_bet(best, "⭐ Best Bet", 0xFFD700)
            await bchan.send(embed=embed, view=BetActions(best))
            posted_by_channel["best"].add(k)
            save_bet_to_db(best, "best")
        await duplicate_to_value_channel(best, format_bet(best, "🟢 Value Bet (Testing)", 0x2ECC71))

    # Quick
    quick = [b for b in bets if b["quick_return"]]
    if qchan:
        for b in quick[:5]:
            k = bet_key(b)
            if k in posted_by_channel["quick"]:
                continue
            embed = format_bet(b, "⏱ Quick Return Bet", 0x2ECC71)
            await qchan.send(embed=embed, view=BetActions(b))
            posted_by_channel["quick"].add(k)
            save_bet_to_db(b, "quick")
            await duplicate_to_value_channel(b, format_bet(b, "🟢 Value Bet (Testing)", 0x2ECC71))

    # Long
    long_plays = [b for b in bets if b["long_play"]]
    if lchan:
        for b in long_plays[:5]:
            k = bet_key(b)
            if k in posted_by_channel["long"]:
                continue
            embed = format_bet(b, "📅 Longer Play Bet", 0x3498DB)
            await lchan.send(embed=embed, view=BetActions(b))
            posted_by_channel["long"].add(k)
            save_bet_to_db(b, "long")
            await duplicate_to_value_channel(b, format_bet(b, "🟢 Value Bet (Testing)", 0x2ECC71))

# =======================
# Slash commands
# =======================
@bot.tree.command(name="ping", description="Check if the bot is alive.")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! ✅", ephemeral=True)

@bot.tree.command(name="fetchbets", description="Manually fetch & preview top edges (ephemeral).")
async def fetchbets_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = fetch_odds()
    bets = calculate_bets(data)
    bets = sorted(bets, key=lambda x: (-x["edge"], -x["consensus"]))[:5]
    if not bets:
        await interaction.followup.send("No bets found right now.", ephemeral=True)
        return
    lines = []
    for b in bets:
        lines.append(f"**{b['match']}** — *{b['team']}* @ {b['odds']} ({b['bookmaker']}) | Edge: {b['edge']}%")
    await interaction.followup.send("🎲 **Bets Preview:**\n" + "\n".join(lines), ephemeral=True)

@bot.tree.command(name="stats", description="Your stats: ROI, win rate, total bets, and P&L (units).")
async def stats_cmd(interaction: discord.Interaction):
    """Shows the requesting user's stats based on user_bets (settled only for ROI/win rate/P&L)."""
    if not DB_URL or not psycopg2:
        await interaction.response.send_message("DB not configured.", ephemeral=True)
        return

    user_id = interaction.user.id
    try:
        with psycopg2.connect(DB_URL, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                # total placed (all statuses)
                cur.execute("SELECT COUNT(*) AS c FROM user_bets WHERE user_id=%s;", (user_id,))
                total = cur.fetchone()["c"]

                # settled rows with sums
                cur.execute("""
                    SELECT
                      COUNT(*)                                   AS settled,
                      SUM(CASE WHEN result='win'  THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) AS losses,
                      SUM(COALESCE(stake_units,0))               AS staked,
                      SUM(COALESCE(return_units,0)-COALESCE(stake_units,0)) AS profit
                    FROM user_bets
                    WHERE user_id=%s AND result IN ('win','loss','push')
                """, (user_id,))
                row = cur.fetchone() or {}
    except Exception as e:
        await interaction.response.send_message(f"Stats error: {e}", ephemeral=True)
        return

    settled = int(row.get("settled") or 0)
    wins    = int(row.get("wins") or 0)
    losses  = int(row.get("losses") or 0)
    staked  = float(row.get("staked") or 0.0)
    profit  = float(row.get("profit") or 0.0)

    # win rate from settled wins/losses only
    wl = wins + losses
    win_rate = (wins / wl * 100.0) if wl > 0 else None
    roi = (profit / staked * 100.0) if staked > 0 else None

    em = discord.Embed(title=f"📊 Your Stats — {interaction.user.name}", color=0x6C5CE7)
    em.add_field(name="Total Bets Logged", value=str(total), inline=True)
    em.add_field(name="Settled Bets", value=str(settled), inline=True)
    em.add_field(name="Wins / Losses", value=f"{wins} / {losses}", inline=True)
    em.add_field(name="Win Rate", value=("—" if win_rate is None else f"{win_rate:.2f}%"), inline=True)
    em.add_field(name="Staked (settled)", value=f"{staked:.2f}u", inline=True)
    em.add_field(name="P&L (settled)", value=f"{profit:+.2f}u", inline=True)
    em.add_field(name="ROI (settled)", value=("—" if roi is None else f"{roi:.2f}%"), inline=True)

    await interaction.response.send_message(embed=em, ephemeral=True)

@bot.tree.command(name="valuechannel", description="Show configured Value Bets channel and access status.")
async def valuechannel_cmd(interaction: discord.Interaction):
    ch = await get_value_channel()
    if ch:
        await interaction.response.send_message(f"Value Bets channel resolved: {ch.mention} (ID {ch.id})", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"Could not resolve Value Bets channel from ID `{VALUE_BETS_CHANNEL_ID}`. "
            "Check env var and bot permissions.", ephemeral=True
        )

# =======================
# Lifecycle
# =======================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await bot.tree.sync()
        print("✅ Slash commands synced.")
    except Exception as e:
        print(f"❌ Slash sync failed: {e}")
    init_db()
    if not bet_loop.is_running():
        bet_loop.start()

@tasks.loop(seconds=60)
async def bet_loop():
    data = fetch_odds()
    bets = calculate_bets(data)
    await post_bets(bets)

# =======================
# Main
# =======================
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN env var")

logging.basicConfig(level=logging.INFO)
bot.run(TOKEN)















