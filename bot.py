import os
import math
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

import discord
from discord.ext import commands, tasks
from discord import app_commands

import psycopg2
import psycopg2.extras

# =========================
# Environment
# =========================
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

BEST_BETS_CHANNEL_ID      = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_RETURNS_CHANNEL_ID  = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_PLAYS_CHANNEL_ID     = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
VALUE_BETS_CHANNEL_ID     = int(os.getenv("VALUE_BETS_CHANNEL_ID", "0"))   # duplicate value-bets channel

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

# Database URL (Railway)
DATABASE_URL = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("❌ Missing DATABASE_PUBLIC_URL or DATABASE_URL")

# =========================
# Discord Bot
# =========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Global DB connection (sync psycopg2)
conn: Optional[psycopg2.extensions.connection] = None

# =========================
# Schema guard (Option B)
# =========================
def ensure_schema(conn):
    """
    Create/extend tables & indexes. Safe to run on every boot.
    """
    cur = conn.cursor()

    # Base tables
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bets (
        id          BIGSERIAL PRIMARY KEY,
        match       TEXT,
        bookmaker   TEXT,
        team        TEXT,
        odds        NUMERIC,
        edge        NUMERIC,
        bet_time    TIMESTAMPTZ,
        category    TEXT,
        created_at  TIMESTAMPTZ DEFAULT now(),
        sport       TEXT,
        league      TEXT
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_bets (
        id          BIGSERIAL PRIMARY KEY,
        user_id     TEXT NOT NULL,
        username    TEXT NOT NULL,
        bet_key     TEXT NOT NULL,
        event_id    TEXT,
        sport       TEXT,
        league      TEXT,
        market      TEXT,
        selection   TEXT,
        odds        NUMERIC,
        stake_type  TEXT,
        stake_units NUMERIC,
        placed_at   TIMESTAMPTZ DEFAULT now(),
        result      TEXT,
        pnl_units   NUMERIC DEFAULT 0,
        settled_at  TIMESTAMPTZ
    );
    """)

    # Backfill columns (idempotent)
    for sql in [
        "ALTER TABLE user_bets ADD COLUMN IF NOT EXISTS stake_type  TEXT",
        "ALTER TABLE user_bets ADD COLUMN IF NOT EXISTS stake_units NUMERIC",
        "ALTER TABLE user_bets ADD COLUMN IF NOT EXISTS result      TEXT",
        "ALTER TABLE user_bets ADD COLUMN IF NOT EXISTS pnl_units   NUMERIC DEFAULT 0",
        "ALTER TABLE user_bets ADD COLUMN IF NOT EXISTS settled_at  TIMESTAMPTZ",
        "ALTER TABLE user_bets ADD COLUMN IF NOT EXISTS league      TEXT",
        "ALTER TABLE user_bets ADD COLUMN IF NOT EXISTS market      TEXT",
        "ALTER TABLE user_bets ADD COLUMN IF NOT EXISTS selection   TEXT",

        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS sport  TEXT",
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS league TEXT",

        "CREATE INDEX IF NOT EXISTS idx_user_bets_user  ON user_bets(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_user_bets_key   ON user_bets(bet_key)",
        "CREATE INDEX IF NOT EXISTS idx_user_bets_time  ON user_bets(placed_at)"
    ]:
        cur.execute(sql)

    conn.commit()
    cur.close()

# =========================
# Allowed bookmakers (as before)
# =========================
ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
]

def allowed_bookmaker(title: str) -> bool:
    return any(k in (title or "").lower() for k in ALLOWED_BOOKMAKER_KEYS)

# =========================
# Odds fetch & bet generation (stub – replace with your working logic)
# =========================
async def fetch_live_bets() -> list[dict]:
    """
    Replace this with your real odds pull. Here we yield a stub
    bet dict matching the embed formatter & button callbacks.
    """
    now = datetime.now(timezone.utc)
    sample = [
        {
            "event_id": "evt_1",
            "match": "Team A vs Team B",
            "team": "Team A @ 1.90",
            "bookmaker": "SportsBet (AU)",
            "odds": 1.90,
            "consensus": 62.0,         # %
            "implied": round(100 / 1.90, 2),
            "edge": 62.0 - round(100 / 1.90, 2),
            "bet_time": (now + timedelta(hours=10)).isoformat(),
            "category": "best",        # "best" | "quick" | "long"
            "sport": "Soccer",
            "league": "A-League",
            "market": "h2h",
            "selection": "Team A",
        }
    ]
    return sample

# =========================
# Embed formatting (unchanged style + sport/league line)
# =========================
COLOR_VALUE   = 0x2ecc71  # green
COLOR_LOW     = 0xe74c3c  # red
COLOR_BEST    = 0xffd700  # gold
COLOR_QUICK   = 0x2ecc71
COLOR_LONG    = 0x3498db

def value_indicator(edge: float) -> str:
    return "🟢 Value Bet" if edge >= 2.0 else "🔴 Low Value"

def sport_emoji(sport: str) -> str:
    s = (sport or "").lower()
    if s in {"football", "nfl", "ncaa football"}:
        return "🏈"
    if s in {"soccer", "football (soccer)"}:
        return "⚽"
    if s in {"basketball", "nba", "nbl"}:
        return "🏀"
    if s in {"baseball", "mlb"}:
        return "⚾"
    if s in {"tennis"}:
        return "🎾"
    if s in {"cricket"}:
        return "🏏"
    if s in {"ice hockey", "nhl"}:
        return "🏒"
    return "🎯"

def make_bet_embed(b: Dict[str, Any], title: str, color: int) -> discord.Embed:
    """
    Keep prior layout exactly, with sport + league shown under the indicator.
    Stakes are shown in units.
    """
    indicator = value_indicator(b.get("edge", 0.0))
    sport = b.get("sport") or "Unknown"
    league = b.get("league") or "Unknown League"
    line = f"{sport_emoji(sport)} {sport} ({league})"

    # Basic stakes (units) – same math as before, just in units
    cons_units = 15.0                # base unit we used for display
    edge = max(0.0, float(b.get("edge", 0.0)))
    smart_units = round(cons_units * (1.0 + edge / 100.0), 2)
    aggr_units  = round(cons_units * (1.0 + (edge/100.0) * 1.75), 2)

    odds = float(b.get("odds", 1.0))
    cons_payout = round(cons_units * odds, 2)
    smart_payout = round(smart_units * odds, 2)
    aggr_payout  = round(aggr_units  * odds, 2)

    implied = float(b.get("implied", 0.0))
    consensus = float(b.get("consensus", 0.0))

    desc = (
        f"{indicator}\n\n"
        f"**{line}**\n\n"
        f"**Match:** {b.get('match')}\n"
        f"**Pick:** {b.get('team')}\n"
        f"**Bookmaker:** {b.get('bookmaker')}\n"
        f"**Consensus %:** {consensus:.2f}%\n"
        f"**Implied %:** {implied:.2f}%\n"
        f"**Edge:** {edge:.2f}%\n"
        f"**Time:** {b.get('bet_time_fmt', b.get('bet_time'))}\n\n"
        f"🪙 **Conservative Stake:** {cons_units} units → Payout: {cons_payout} | Exp. Profit: {round(consensus/100*cons_payout - cons_units, 2)}\n"
        f"🧠 **Smart Stake:** {smart_units} units → Payout: {smart_payout} | Exp. Profit: {round(consensus/100*smart_payout - smart_units, 2)}\n"
        f"🔥 **Aggressive Stake:** {aggr_units} units → Payout: {aggr_payout} | Exp. Profit: {round(consensus/100*aggr_payout - aggr_units, 2)}\n"
    )

    embed = discord.Embed(title=title, description=desc, color=color)
    embed.set_footer(text=f"Odds {odds} | Event {b.get('event_id','-')}")
    return embed

# =========================
# Buttons for paper trading (Conservative / Smart / Aggressive)
# =========================
class StakeView(discord.ui.View):
    def __init__(self, bet: Dict[str, Any]):
        super().__init__(timeout=3600)
        self.bet = bet  # dict that includes event_id, sport, league, selection, odds, etc.

    async def record_bet(self, interaction: discord.Interaction, stake_type: str, stake_units: float):
        global conn
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO user_bets
                  (user_id, username, bet_key, event_id, sport, league, market, selection, odds,
                   stake_type, stake_units, placed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                RETURNING id
            """, (
                str(interaction.user.id),
                interaction.user.name,
                # bet_key: stable ID for the bet (event + selection)
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
            conn.commit()
            new_id = cur.fetchone()[0]
            cur.close()
            await interaction.response.send_message(
                f"✅ Saved your **{stake_type}** bet ({stake_units} units). Entry #{new_id}.", ephemeral=True
            )
        except Exception as e:
            if conn:
                conn.rollback()
            await interaction.response.send_message(
                f"❌ Could not save your bet. Is the database configured?\n```\n{e}\n```",
                ephemeral=True,
            )

    @discord.ui.button(label="Conservative", emoji="🪙", style=discord.ButtonStyle.secondary)
    async def btn_cons(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.record_bet(interaction, "conservative", 15.0)

    @discord.ui.button(label="Smart", emoji="🧠", style=discord.ButtonStyle.primary)
    async def btn_smart(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Smart uses edge to scale units a bit
        edge = max(0.0, float(self.bet.get("edge", 0.0)))
        smart_units = round(15.0 * (1.0 + edge/100.0), 2)
        await self.record_bet(interaction, "smart", smart_units)

    @discord.ui.button(label="Aggressive", emoji="🔥", style=discord.ButtonStyle.danger)
    async def btn_aggr(self, interaction: discord.Interaction, button: discord.ui.Button):
        edge = max(0.0, float(self.bet.get("edge", 0.0)))
        aggr_units = round(15.0 * (1.0 + (edge/100.0) * 1.75), 2)
        await self.record_bet(interaction, "aggressive", aggr_units)

# =========================
# Posting bets to channels (and duplicating value bets to VALUE_BETS_CHANNEL_ID)
# =========================
async def post_bet(bet: Dict[str, Any]):
    # Format time
    try:
        dt = datetime.fromisoformat(bet["bet_time"].replace("Z", "+00:00"))
        bet["bet_time_fmt"] = dt.strftime("%d/%m/%y %H:%M")
    except Exception:
        pass

    category = (bet.get("category") or "").lower()
    title = "⭐ Best Bet" if category == "best" else ("⏱ Quick Return Bet" if category == "quick" else "📅 Longer Play Bet")
    color = COLOR_BEST if category == "best" else (COLOR_QUICK if category == "quick" else COLOR_LONG)

    embed = make_bet_embed(bet, title, color)
    view  = StakeView(bet)

    # Select the primary channel by category
    channel_id = BEST_BETS_CHANNEL_ID if category == "best" else (
        QUICK_RETURNS_CHANNEL_ID if category == "quick" else LONG_PLAYS_CHANNEL_ID
    )
    if channel_id:
        ch = bot.get_channel(channel_id)
        if ch:
            await ch.send(embed=embed, view=view)

    # Duplicate "value" bets to the VALUE_BETS channel if configured
    if VALUE_BETS_CHANNEL_ID:
        # consider anything with edge>=2 as value
        if float(bet.get("edge", 0)) >= 2.0:
            dup_ch = bot.get_channel(VALUE_BETS_CHANNEL_ID)
            if dup_ch:
                # same embed + view
                await dup_ch.send(embed=embed, view=StakeView(bet))

# =========================
# Stats & ROI
# =========================
def fetch_stats() -> Dict[str, Any]:
    """
    Aggregate stats from user_bets.
    For ROI we use sum(pnl_units)/sum(stake_units).
    """
    out = {
        "total_bets": 0,
        "total_units": 0.0,
        "pnl_units": 0.0,
        "roi": 0.0,
        "per_strategy": {}   # {stake_type: {...}}
    }
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT stake_type,
               COUNT(*)               AS n,
               COALESCE(SUM(stake_units),0) AS units,
               COALESCE(SUM(pnl_units),0)   AS pnl
        FROM user_bets
        GROUP BY stake_type
    """)
    rows = cur.fetchall()
    cur.close()

    tot_units = 0.0
    tot_pnl   = 0.0
    tot_bets  = 0

    for r in rows:
        st = r["stake_type"] or "unknown"
        n = int(r["n"])
        u = float(r["units"] or 0)
        p = float(r["pnl"] or 0)
        roi = (p / u * 100.0) if u > 0 else 0.0

        out["per_strategy"][st] = {
            "bets": n, "units": u, "pnl": p, "roi": roi
        }
        tot_bets  += n
        tot_units += u
        tot_pnl   += p

    out["total_bets"]  = tot_bets
    out["total_units"] = round(tot_units, 2)
    out["pnl_units"]   = round(tot_pnl, 2)
    out["roi"]         = round((tot_pnl/tot_units*100.0) if tot_units>0 else 0.0, 2)
    return out

# =========================
# Slash commands
# =========================
@bot.tree.command(name="ping", description="Check bot latency.")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency*1000)} ms", ephemeral=True)

@bot.tree.command(name="stats", description="Your paper-trading stats across all strategies.")
async def stats_cmd(interaction: discord.Interaction):
    s = fetch_stats()
    lines = []
    for k, v in s["per_strategy"].items():
        lines.append(f"• **{k}** → {v['bets']} bets | {v['units']:.2f} units | P/L {v['pnl']:.2f} | ROI {v['roi']:.2f}%")
    if not lines:
        lines = ["No saved bets yet."]
    total_line = f"\n**Total** → {s['total_units']:.2f} units | P/L {s['pnl_units']:.2f} | ROI {s['roi']:.2f}%"
    await interaction.response.send_message("\n".join(lines) + total_line, ephemeral=True)

@bot.tree.command(name="roi", description="Portfolio ROI / P&L from saved bets.")
async def roi_cmd(interaction: discord.Interaction):
    s = fetch_stats()
    msg = f"📈 ROI: **{s['roi']:.2f}%** | P/L **{s['pnl_units']:.2f}** units | Bets **{s['total_bets']}**"
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="fetchbets", description="Manually fetch and post bets now.")
async def fetchbets_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    bets = await fetch_live_bets()
    posted = 0
    for b in bets:
        try:
            await post_bet(b)
            posted += 1
        except Exception:
            pass
    await interaction.followup.send(f"Fetched & posted {posted} bet(s).", ephemeral=True)

# =========================
# Background loop to fetch bets
# =========================
@tasks.loop(minutes=5)
async def bet_loop():
    try:
        bets = await fetch_live_bets()
        for b in bets:
            await post_bet(b)
    except Exception as e:
        print("bet_loop error:", e)

# =========================
# Bot lifecycle
# =========================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    # Sync slash commands
    try:
        await bot.tree.sync()
        print("✅ Slash commands synced.")
    except Exception as e:
        print("Slash sync failed:", e)

    # Start fetch loop
    if not bet_loop.is_running():
        bet_loop.start()

def main():
    global conn
    # Connect DB (require SSL on Railway)
    conn = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
        sslmode="require",
    )
    # Auto-migrate
    ensure_schema(conn)

    # Run bot
    if not DISCORD_BOT_TOKEN:
        raise SystemExit("❌ Missing DISCORD_BOT_TOKEN")
    bot.run(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    main()




