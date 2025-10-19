# bot.py
import os
import time
import math
import json
import random
import logging
from datetime import datetime, timezone, timedelta

import requests
import discord
from discord.ext import commands, tasks

import psycopg2
import psycopg2.extras

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("valuebets")

# ---------------------------
# Config / ENV
# ---------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

CHANNEL_ID_BEST = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
CHANNEL_ID_QUICK = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
CHANNEL_ID_LONG  = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))

# Optional "duplicate value bets" channel for testing
CHANNEL_ID_VALUE_DUP = int(os.getenv("DISCORD_CHANNEL_ID_VALUE_DUP", "0"))

ODDS_API_KEY  = os.getenv("ODDS_API_KEY")

# Paper-trading DB
DATABASE_URL  = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")

# stake policy (units)
BANKROLL_UNITS   = 1000.0
CONSERVATIVE_PCT = 0.015  # 1.5% of bankroll
SMART_BASE_UNITS  = 0.35   # adjustable base (example)
AGG_FACTOR_PER_EDGE = 6.0  # multiplier influenced by edge

# ---------------------------
# Discord Bot
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------------------------
# Psycopg2 connection (global)
# ---------------------------
conn = None

def ensure_schema(_conn):
    """Create/upgrade the schema if missing. Safe to run repeatedly."""
    with _conn.cursor() as c:
        # bets table: global (paper-trading feed)
        c.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id SERIAL PRIMARY KEY,
            event_id TEXT,
            match TEXT,
            bookmaker TEXT,
            team TEXT,
            odds NUMERIC,
            edge NUMERIC,
            bet_time TIMESTAMPTZ,
            category TEXT,           -- 'best', 'quick', 'long'
            sport TEXT,
            league TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        );
        """)
        # user_bets: individual user clicks via buttons
        c.execute("""
        CREATE TABLE IF NOT EXISTS user_bets (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            username TEXT,
            bet_key TEXT,           -- hash for de-dup/tracking
            event_id TEXT,
            sport TEXT,
            league TEXT,
            market TEXT,
            selection TEXT,
            odds NUMERIC,
            stake_type TEXT,        -- 'conservative'|'smart'|'aggressive'
            stake_units NUMERIC,
            placed_at TIMESTAMPTZ DEFAULT now(),
            -- settlement for paper trading:
            result TEXT,            -- 'win'|'loss'|'void'|NULL
            settled_at TIMESTAMPTZ,
            payout NUMERIC,
            pnl NUMERIC
        );
        """)
    _conn.commit()

def get_db():
    """
    Ensure we always have a usable psycopg2 connection.
    Reconnect if Railway closed the connection.
    """
    global conn, DATABASE_URL
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL (or DATABASE_PUBLIC_URL) is not set")
    if conn is None or getattr(conn, "closed", 0):
        conn = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
            sslmode="require",
        )
        ensure_schema(conn)
    return conn

# ---------------------------
# Odds / Sports helpers
# ---------------------------
ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
]

SPORT_EMOJI = {
    "soccer": "⚽",
    "basketball": "🏀",
    "americanfootball": "🏈",
    "football": "🏈",
    "icehockey": "🏒",
    "baseball": "⚾",
    "tennis": "🎾",
    "mma": "🥊",
    "boxing": "🥊",
    "aussierules": "🏉",
    "cricket": "🏏",
    "golf": "⛳",
    "esports": "🎮",
}

def sport_label(key: str) -> str:
    k = (key or "").lower()
    if k == "football":
        # treat as American football
        return "American Football"
    mapping = {
        "soccer": "Soccer",
        "basketball": "Basketball",
        "americanfootball": "American Football",
        "icehockey": "Ice Hockey",
        "baseball": "Baseball",
        "tennis": "Tennis",
        "mma": "MMA",
        "boxing": "Boxing",
        "aussierules": "Aussie Rules",
        "cricket": "Cricket",
        "golf": "Golf",
        "esports": "Esports",
    }
    return mapping.get(k, key.title() if key else "Sport")

def sport_emoji(key: str) -> str:
    return SPORT_EMOJI.get((key or "").lower(), "🎲")

def _allowed_bookmaker(title: str) -> bool:
    return any(key in (title or "").lower() for key in ALLOWED_BOOKMAKER_KEYS)

def fetch_odds():
    """Pull upcoming odds from TheOddsAPI"""
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal"
    }
    try:
        resp = requests.get(url, params=params, timeout=12)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.exception("Odds API error: %s", e)
        return []

def calculate_bets(data):
    """Turn raw odds into our bet dicts (value calculations, stakes, etc.)."""
    now = datetime.now(timezone.utc)
    bets = []

    for event in data:
        home, away = event.get("home_team"), event.get("away_team")
        match_name = f"{home} vs {away}"
        commence_time = event.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        except Exception:
            continue

        delta = commence_dt - now
        # Keep next ~5 months max, skip past events
        if delta.total_seconds() <= 0 or delta > timedelta(days=150):
            continue

        # Best-effort sport/league
        sport_key = (event.get("sport_key") or "").lower()
        league = event.get("sport_title") or None

        # consensus from allowed books
        from collections import defaultdict
        consensus_by_outcome = defaultdict(list)

        for book in event.get("bookmakers", []):
            if not _allowed_bookmaker(book.get("title", "")):
                continue
            for m in book.get("markets", []):
                for outcome in m.get("outcomes", []):
                    price = outcome.get("price")
                    name  = outcome.get("name")
                    if not price or not name:
                        continue
                    key = f"{m.get('key')}:{name}"
                    consensus_by_outcome[key].append(1 / price)

        if not consensus_by_outcome:
            continue

        denom = sum(len(v) for v in consensus_by_outcome.values()) or 1
        global_cons = sum(p for lst in consensus_by_outcome.values() for p in lst) / denom

        # Compose bets
        for book in event.get("bookmakers", []):
            title = book.get("title") or "Book"
            if not _allowed_bookmaker(title):
                continue
            for m in book.get("markets", []):
                for outcome in m.get("outcomes", []):
                    price = outcome.get("price")
                    name  = outcome.get("name")
                    if not price or not name:
                        continue

                    implied_p  = 1 / price
                    outcome_key = f"{m.get('key')}:{name}"
                    if outcome_key in consensus_by_outcome:
                        consensus_p = sum(consensus_by_outcome[outcome_key]) / len(consensus_by_outcome[outcome_key])
                    else:
                        consensus_p = global_cons

                    edge = consensus_p - implied_p
                    if edge <= 0:
                        continue

                    # Units
                    cons_units = round(BANKROLL_UNITS * CONSERVATIVE_PCT, 2)
                    smart_units = round(SMART_BASE_UNITS + max(0, edge) * 20, 2)
                    agg_units = round(cons_units * (1 + edge * AGG_FACTOR_PER_EDGE), 2)

                    # Payouts / Exp profits (in units)
                    cons_payout = round(cons_units * price, 2)
                    smart_payout = round(smart_units * price, 2)
                    agg_payout  = round(agg_units * price, 2)

                    cons_exp_profit = round(consensus_p * cons_payout - cons_units, 2)
                    smart_exp_profit = round(consensus_p * smart_payout - smart_units, 2)
                    agg_exp_profit  = round(consensus_p * agg_payout - agg_units, 2)

                    b = {
                        "event_id": event.get("id"),
                        "match": match_name,
                        "bookmaker": title,
                        "selection": name,
                        "market": m.get("key"),
                        "odds": float(price),
                        "time_dt": commence_dt,
                        "time": commence_dt.strftime("%d/%m/%y %H:%M"),

                        "probability": round(implied_p * 100, 2),
                        "consensus":   round(consensus_p * 100, 2),
                        "edge_pct":    round(edge * 100, 2),
                        "edge":        edge,  # raw edge

                        "cons_units": cons_units,
                        "smart_units": smart_units,
                        "agg_units":   agg_units,

                        "cons_payout": cons_payout,
                        "smart_payout": smart_payout,
                        "agg_payout":   agg_payout,

                        "cons_exp_profit": cons_exp_profit,
                        "smart_exp_profit": smart_exp_profit,
                        "agg_exp_profit":   agg_exp_profit,

                        "quick_return": delta <= timedelta(hours=48),
                        "long_play":    timedelta(hours=48) < delta <= timedelta(days=150),

                        "sport": sport_key,
                        "league": league,
                    }
                    bets.append(b)

    return bets

# ---------------------------
# Embeds + Buttons
# ---------------------------
def format_bet_embed(bet: dict, title: str, color: int) -> discord.Embed:
    # Value indicator
    indicator = "🟢 Value Bet" if bet["edge_pct"] >= 2 else "🛑 Low Value"

    sport = sport_label(bet.get("sport"))
    league = bet.get("league") or "Unknown League"
    header = f"{sport_emoji(bet.get('sport'))} {sport} ({league})"

    desc = (
        f"{indicator}\n\n"
        f"**{header}**\n\n"
        f"**Match:** {bet['match']}\n"
        f"**Pick:** {bet['selection']} @ {bet['odds']}\n"
        f"**Bookmaker:** {bet['bookmaker']}\n"
        f"**Consensus %:** {bet['consensus']}%\n"
        f"**Implied %:** {bet['probability']}%\n"
        f"**Edge:** {bet['edge_pct']}%\n"
        f"**Time:** {bet['time']}\n\n"
        f"💵 **Conservative Stake:** {bet['cons_units']} units → Payout: {bet['cons_payout']} | Exp. Profit: {bet['cons_exp_profit']}\n"
        f"🧠 **Smart Stake:** {bet['smart_units']} units → Payout: {bet['smart_payout']} | Exp. Profit: {bet['smart_exp_profit']}\n"
        f"🔥 **Aggressive Stake:** {bet['agg_units']} units → Payout: {bet['agg_payout']} | Exp. Profit: {bet['agg_exp_profit']}\n"
    )
    return discord.Embed(title=title, description=desc, color=color)

class StakeView(discord.ui.View):
    def __init__(self, bet: dict, timeout: float = 120.0):
        super().__init__(timeout=timeout)
        self.bet = bet

    @discord.ui.button(label="Conservative", style=discord.ButtonStyle.success, emoji="💵")
    async def btn_cons(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_bet(interaction, "conservative", float(self.bet.get("cons_units", 0.0)))

    @discord.ui.button(label="Smart", style=discord.ButtonStyle.primary, emoji="🧠")
    async def btn_smart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_bet(interaction, "smart", float(self.bet.get("smart_units", 0.0)))

    @discord.ui.button(label="Aggressive", style=discord.ButtonStyle.danger, emoji="🔥")
    async def btn_agg(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_bet(interaction, "aggressive", float(self.bet.get("agg_units", 0.0)))

    async def record_bet(self, interaction: discord.Interaction, stake_type: str, stake_units: float):
        try:
            db = get_db()
            cur = db.cursor()
            cur.execute("""
                INSERT INTO user_bets
                  (user_id, username, bet_key, event_id, sport, league, market, selection, odds,
                   stake_type, stake_units, placed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                RETURNING id
            """, (
                str(interaction.user.id),
                interaction.user.name,
                f"{self.bet.get('event_id','-')}|{self.bet.get('selection','-')}|{self.bet.get('odds')}",
                self.bet.get("event_id"),
                self.bet.get("sport"),
                self.bet.get("league"),
                self.bet.get("market"),
                self.bet.get("selection"),
                float(self.bet.get("odds", 1.0)),
                stake_type,
                float(stake_units),
            ))

            row = cur.fetchone()
            db.commit()
            cur.close()

            new_id = row["id"] if isinstance(row, dict) else row[0]
            await interaction.response.send_message(
                f"✅ Saved your **{stake_type}** bet ({stake_units} units). Entry #{new_id}.",
                ephemeral=True
            )

        except Exception as e:
            # Try to rollback cleanly
            try:
                db = get_db()
                db.rollback()
            except Exception:
                pass

            # Detailed diagnostics for psycopg2
            details = repr(e)
            if hasattr(e, "pgcode") and e.pgcode:
                details += f"\npgcode: {e.pgcode}"
            if hasattr(e, "pgerror") and e.pgerror:
                details += f"\npgerror: {e.pgerror}"
            if hasattr(e, "diag") and getattr(e, "diag", None):
                d = e.diag
                pieces = []
                for attr in ("severity", "primary", "message_detail", "message_hint", "context"):
                    val = getattr(d, attr, None)
                    if val:
                        pieces.append(f"{attr}: {val}")
                if pieces:
                    details += "\n" + "\n".join(pieces)

            await interaction.response.send_message(
                "❌ Could not save your bet. Is the database configured?\n"
                f"```{details}```",
                ephemeral=True
            )

# ---------------------------
# Posting + persistence
# ---------------------------
async def post_bets(bets):
    if not bets:
        ch = bot.get_channel(CHANNEL_ID_BEST)
        if ch:
            await ch.send("⚠️ No bets right now.")
        return

    # Best bet = highest (consensus %, then edge)
    best = max(bets, key=lambda x: (x["consensus"], x["edge"]))
    await send_bet(best, "⭐ Best Bet", 0xFFD700, "best")

    # Quick
    quick = [b for b in bets if b["quick_return"]]
    for b in quick[:5]:
        await send_bet(b, "⏱ Quick Return Bet", 0x2ECC71, "quick")

    # Long
    long_bets = [b for b in bets if b["long_play"]]
    for b in long_bets[:5]:
        await send_bet(b, "📅 Longer Play Bet", 0x3498DB, "long")

async def send_bet(b: dict, title: str, color: int, category: str):
    """Send embed + buttons, save to bets table (paper-trading feed)."""
    embed = format_bet_embed(b, title, color)
    view = StakeView(b)

    # Which channel?
    target_id = CHANNEL_ID_BEST if category == "best" else (
        CHANNEL_ID_QUICK if category == "quick" else CHANNEL_ID_LONG
    )
    ch = bot.get_channel(target_id)
    if ch:
        await ch.send(embed=embed, view=view)

    # Duplicate to value-bets testing channel if this is a value bet
    if CHANNEL_ID_VALUE_DUP and b["edge_pct"] >= 2:
        val_ch = bot.get_channel(CHANNEL_ID_VALUE_DUP)
        if val_ch:
            # keep same embed
            title2 = "🔁 Value Bet (Testing)"
            embed2 = format_bet_embed(b, title2, 0x00B894)
            await val_ch.send(embed=embed2, view=StakeView(b))

    # Save to paper-trading feed (bets)
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO bets (event_id, match, bookmaker, team, odds, edge, bet_time, category, sport, league)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                b.get("event_id"),
                b.get("match"),
                b.get("bookmaker"),
                b.get("selection"),
                float(b.get("odds", 1.0)),
                float(b.get("edge", 0.0)),
                b.get("time_dt"),
                category,
                b.get("sport"),
                b.get("league"),
            ))
        db.commit()
    except Exception as e:
        log.exception("Failed to save bet to DB: %s", e)

# ---------------------------
# Tasks / Events
# ---------------------------
@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    try:
        await tree.sync()
        log.info("Slash commands synced.")
    except Exception as e:
        log.exception("Slash sync failed: %s", e)

    # Prime DB connection (optional)
    if DATABASE_URL:
        try:
            get_db()
            log.info("DB ready.")
        except Exception as e:
            log.exception("DB not ready: %s", e)

    if not bet_loop.is_running():
        bet_loop.start()

@tasks.loop(seconds=45)
async def bet_loop():
    data = fetch_odds()
    bets = calculate_bets(data)
    await post_bets(bets)

# ---------------------------
# Slash commands
# ---------------------------
@tree.command(name="ping", description="Latency check.")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)

@tree.command(name="fetchbets", description="Manually fetch and post bets.")
async def fetchbets_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = fetch_odds()
    bets = calculate_bets(data)
    await post_bets(bets)
    await interaction.followup.send(f"Fetched {len(bets)} potential bets.", ephemeral=True)

@tree.command(name="stats", description="Paper-trading stats (from DB).")
async def stats_cmd(interaction: discord.Interaction):
    """Simple aggregate stats example from saved user_bets table."""
    try:
        db = get_db()
        with db.cursor() as cur:
            # total stakes + pnl by stake_type
            cur.execute("""
                SELECT stake_type,
                       COUNT(*) as cnt,
                       COALESCE(SUM(stake_units),0) as units,
                       COALESCE(SUM(pnl),0) as pnl
                FROM user_bets
                GROUP BY stake_type
                ORDER BY stake_type;
            """)
            rows = cur.fetchall()

            # global winrate and ROI
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE result='win')::float / NULLIF(COUNT(*),0) AS win_rate,
                    COALESCE(SUM(pnl),0) / NULLIF(SUM(stake_units),0) AS roi
                FROM user_bets;
            """)
            agg = cur.fetchone()

        lines = []
        total_units = 0.0
        total_pnl = 0.0
        for r in rows:
            st = r["stake_type"] or "unknown"
            cnt = r["cnt"]
            units = float(r["units"] or 0)
            pnl = float(r["pnl"] or 0)
            total_units += units
            total_pnl += pnl
            lines.append(f"• **{st}** → {cnt} bets | {units:.2f} units | P/L {pnl:.2f}")

        win_rate = (agg["win_rate"] or 0) * 100 if agg else 0
        roi = (agg["roi"] or 0) * 100 if agg else 0

        msg = "\n".join(lines) + f"\n\n**Total** → {total_units:.2f} units | **P/L** {total_pnl:.2f} | **ROI** {roi:.2f}% | **Win rate** {win_rate:.1f}%"
        await interaction.response.send_message(msg, ephemeral=True)

    except Exception as e:
        await interaction.response.send_message(f"❌ Error: `{e}`", ephemeral=True)

# ---------------------------
# Entrypoint
# ---------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ Missing DISCORD_BOT_TOKEN")

    bot.run(TOKEN)



