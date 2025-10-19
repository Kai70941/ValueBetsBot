import os
import json
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import psycopg2
import psycopg2.extras

# ============
# ENV & CONFIG
# ============
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

BEST_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_RETURNS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_PLAYS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))

# optional: value-bets (testing/duplicate) channel
VALUE_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_VALUE", "0"))

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")

INTENTS = discord.Intents.default()
INTENTS.message_content = True

# Use Bot directly; Bot already has .tree
bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ===================
# DATABASE UTILITIES
# ===================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable not set.")
    if not hasattr(bot, "_db_conn") or bot._db_conn is None:
        bot._db_conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        bot._db_conn.autocommit = True
    return bot._db_conn

def ensure_tables():
    """
    Ensures we have the two core tables used by the bot:
      - bets:      system (paper-trade) feed of ideas
      - user_bets: actual user-clicked stakes for tracking personal stats
    """
    db = get_db()
    with db.cursor() as cur:
        # System feed of ideas (paper trading)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                id SERIAL PRIMARY KEY,
                event_id TEXT,
                sport TEXT,
                league TEXT,
                match TEXT,
                bookmaker TEXT,
                team TEXT,
                odds NUMERIC,
                edge NUMERIC,
                bet_time TIMESTAMP,
                category TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

        # Per-user placed bets via buttons
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_bets (
                id SERIAL PRIMARY KEY,
                user_id TEXT,
                username TEXT,
                bet_key TEXT,
                event_id TEXT,
                sport TEXT,
                league TEXT,
                match TEXT,
                bookmaker TEXT,
                team TEXT,
                odds NUMERIC,
                stake_type TEXT,     -- conservative / smart / aggressive
                stake_units NUMERIC, -- how many units they clicked
                result TEXT,         -- win/loss/void or NULL until known
                created_at TIMESTAMP DEFAULT NOW(),
                settled_at TIMESTAMP
            );
        """)

# ======================
# EMBEDS / UI COMPONENTS
# ======================

def value_indicator(edge_percent: float) -> str:
    return "🟢 Value Bet" if (edge_percent or 0) >= 2.0 else "🔴 Low Value"

def sport_icon(sport: str) -> str:
    s = (sport or "").lower()
    if "soccer" in s or ("football" in s and "american" not in s):
        return "⚽"
    if "american" in s or s == "nfl":
        return "🏈"
    if "basketball" in s or "nba" in s or "nbl" in s:
        return "🏀"
    if "tennis" in s:
        return "🎾"
    if "cricket" in s:
        return "🏏"
    if "baseball" in s:
        return "⚾"
    if "hockey" in s:
        return "🏒"
    return "🎲"

def embed_bet_card(b: dict, title: str, color: int) -> discord.Embed:
    league = b.get("league") or "Unknown League"
    sport = b.get("sport") or "Unknown"
    sport_line = f"{sport_icon(sport)} {sport.title()} ({league})"

    indicator = value_indicator(b.get("edge") or 0)
    description = (
        f"{indicator}\n\n"
        f"**{sport_line}**\n\n"
        f"**Match:** {b.get('match')}\n"
        f"**Pick:** {b.get('team')} @ {b.get('odds')}\n"
        f"**Bookmaker:** {b.get('bookmaker')}\n"
        f"**Consensus %:** {b.get('consensus', 0)}%\n"
        f"**Implied %:** {b.get('probability', 0)}%\n"
        f"**Edge:** {b.get('edge', 0)}%\n"
        f"**Time:** {b.get('time')}\n\n"
        f"💵 **Conservative Stake:** {b.get('cons_stake_units', 0)} units → "
        f"Payout: {b.get('cons_payout_units', 0)} | Exp. Profit: {b.get('cons_exp_profit_units', 0)}\n"
        f"🧠 **Smart Stake:** {b.get('sm_stake_units', 0)} units → "
        f"Payout: {b.get('sm_payout_units', 0)} | Exp. Profit: {b.get('sm_exp_profit_units', 0)}\n"
        f"🔥 **Aggressive Stake:** {b.get('agg_stake_units', 0)} units → "
        f"Payout: {b.get('agg_payout_units', 0)} | Exp. Profit: {b.get('agg_exp_profit_units', 0)}\n"
    )
    em = discord.Embed(title=title, description=description, color=color)
    return em

# ========
# BUTTONS
# ========

class ViewButtons(discord.ui.View):
    def __init__(self, bet_payload: dict, timeout: float = 1800):
        super().__init__(timeout=timeout)
        self.bet_payload = bet_payload

    async def _save_user_bet(self, interaction: discord.Interaction, stake_type: str, units: float):
        try:
            ensure_tables()
            db = get_db()
            b = self.bet_payload

            with db.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_bets (user_id, username, bet_key, event_id, sport, league, match,
                                            bookmaker, team, odds, stake_type, stake_units)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id;
                """, (
                    str(interaction.user.id),
                    interaction.user.name,
                    b.get("bet_key"),
                    b.get("event_id"),
                    b.get("sport"),
                    b.get("league"),
                    b.get("match"),
                    b.get("bookmaker"),
                    b.get("team"),
                    float(b.get("odds", 0.0)),
                    stake_type,
                    float(units),
                ))
                row = cur.fetchone()
            await interaction.response.send_message(
                f"✅ Saved your **{stake_type}** bet ({units:.2f} units). Entry **#{row['id']}**.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ Could not save your bet. `{e}`", ephemeral=True)

    @discord.ui.button(label="Conservative", style=discord.ButtonStyle.secondary, emoji="🪙")
    async def conservative(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._save_user_bet(interaction, "conservative", float(self.bet_payload.get("cons_stake_units", 0)))

    @discord.ui.button(label="Smart", style=discord.ButtonStyle.primary, emoji="🧠")
    async def smart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._save_user_bet(interaction, "smart", float(self.bet_payload.get("sm_stake_units", 0)))

    @discord.ui.button(label="Aggressive", style=discord.ButtonStyle.danger, emoji="🔥")
    async def aggressive(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._save_user_bet(interaction, "aggressive", float(self.bet_payload.get("agg_stake_units", 0)))

# ===================
# SYSTEM-WIDE COMMAND
# ===================

@bot.tree.command(name="roi", description="System-wide ROI and Win% across all users.")
async def roi_cmd(interaction: discord.Interaction):
    try:
        ensure_tables()
        db = get_db()
        with db.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*)::int                                             AS total_bets,
                    COALESCE(SUM(CASE WHEN result='win'  THEN 1 ELSE 0 END),0)::int AS wins,
                    COALESCE(SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END),0)::int AS losses,
                    COALESCE(SUM(CASE WHEN result='void' THEN 1 ELSE 0 END),0)::int AS voids,
                    COALESCE(SUM(stake_units),0)                              AS stake_units,
                    COALESCE(SUM(
                        CASE
                            WHEN result='win'  THEN stake_units*(odds-1)
                            WHEN result='loss' THEN -stake_units
                            ELSE 0
                        END
                    ),0)                                                      AS pnl_units
                FROM user_bets;
            """)
            agg = cur.fetchone()

        total = agg["total_bets"] or 0
        wins  = agg["wins"] or 0
        losses = agg["losses"] or 0
        voids = agg["voids"] or 0
        stake = float(agg["stake_units"] or 0.0)
        pnl   = float(agg["pnl_units"] or 0.0)

        decided = wins + losses
        win_pct = (wins / decided * 100.0) if decided > 0 else 0.0
        roi_pct = (pnl / stake * 100.0) if stake > 0 else 0.0

        msg = (
            f"**System ROI**\n"
            f"• Total bets: **{total}** (wins: {wins}, losses: {losses}, voids: {voids})\n"
            f"• Units staked: **{stake:.2f}**\n"
            f"• P/L (units): **{pnl:.2f}**\n"
            f"• ROI: **{roi_pct:.2f}%**\n"
            f"• Win rate (excl. voids): **{win_pct:.2f}%**"
        )
        await interaction.response.send_message(msg, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: `{e}`", ephemeral=True)

# =================
# PERSONAL COMMAND
# =================

@bot.tree.command(name="stats", description="Your personal stats and per-strategy breakdown.")
async def stats_cmd(interaction: discord.Interaction):
    try:
        ensure_tables()
        uid = str(interaction.user.id)
        db = get_db()
        with db.cursor() as cur:
            # Aggregate for this user
            cur.execute("""
                SELECT
                    COUNT(*)::int                                             AS total_bets,
                    COALESCE(SUM(CASE WHEN result='win'  THEN 1 ELSE 0 END),0)::int AS wins,
                    COALESCE(SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END),0)::int AS losses,
                    COALESCE(SUM(CASE WHEN result='void' THEN 1 ELSE 0 END),0)::int AS voids,
                    COALESCE(SUM(stake_units),0)                              AS stake_units,
                    COALESCE(SUM(
                        CASE
                            WHEN result='win'  THEN stake_units*(odds-1)
                            WHEN result='loss' THEN -stake_units
                            ELSE 0
                        END
                    ),0)                                                      AS pnl_units
                FROM user_bets
                WHERE user_id = %s;
            """, (uid,))
            agg = cur.fetchone()

            # Per-strategy breakdown
            cur.execute("""
                SELECT
                    stake_type,
                    COUNT(*)::int AS total_bets,
                    COALESCE(SUM(stake_units),0) AS stake_units,
                    COALESCE(SUM(
                        CASE
                            WHEN result='win'  THEN stake_units*(odds-1)
                            WHEN result='loss' THEN -stake_units
                            ELSE 0
                        END
                    ),0) AS pnl_units,
                    COALESCE(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END),0)::int AS wins,
                    COALESCE(SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END),0)::int AS losses
                FROM user_bets
                WHERE user_id = %s
                GROUP BY stake_type
                ORDER BY stake_type;
            """, (uid,))
            rows = cur.fetchall()

        total = agg["total_bets"] or 0
        wins  = agg["wins"] or 0
        losses = agg["losses"] or 0
        voids  = agg["voids"] or 0
        stake  = float(agg["stake_units"] or 0.0)
        pnl    = float(agg["pnl_units"] or 0.0)

        decided = wins + losses
        win_pct = (wins / decided * 100.0) if decided > 0 else 0.0
        roi_pct = (pnl / stake * 100.0) if stake > 0 else 0.0

        lines = [f"**Your Stats**"]
        lines.append(f"• Total bets: **{total}** (wins: {wins}, losses: {losses}, voids: {voids})")
        lines.append(f"• Units staked: **{stake:.2f}** | P/L: **{pnl:.2f}** | ROI: **{roi_pct:.2f}%** | Win%: **{win_pct:.2f}%**")

        if rows:
            lines.append("\n**By Strategy**")
            for r in rows:
                stype = (r["stake_type"] or "unknown").title()
                t     = r["total_bets"] or 0
                s     = float(r["stake_units"] or 0.0)
                p     = float(r["pnl_units"] or 0.0)
                w     = r["wins"] or 0
                l     = r["losses"] or 0
                d     = w + l
                wr    = (w / d * 100.0) if d > 0 else 0.0
                roi_s = (p / s * 100.0) if s > 0 else 0.0
                lines.append(f"• **{stype}** → {t} bets | {s:.2f} units | P/L {p:.2f} | ROI {roi_s:.2f}% | Win% {wr:.2f}%")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    except Exception as e:
        await interaction.response.send_message(f"❌ Error: `{e}`", ephemeral=True)

# ==================================
# (OPTIONAL) TEST POST COMMAND ONLY
# ==================================
@bot.tree.command(name="posttest", description="Post a sample bet card (for quick testing).")
async def posttest(interaction: discord.Interaction):
    sample = {
        "bet_key": "SAMPLE-123",
        "event_id": "E-123",
        "sport": "soccer",
        "league": "Brazil Série B",
        "match": "América Mineiro vs Volta Redonda",
        "bookmaker": "Betfair",
        "team": "América Mineiro @ 1.99",
        "odds": 1.99,
        "consensus": 53.66,
        "probability": 50.25,
        "edge": 3.49,
        "time": datetime.now(timezone.utc).strftime("%d/%m/%y %H:%M"),
        "cons_stake_units": 15.0,
        "sm_stake_units": 5.0,
        "agg_stake_units": 66.0,
        "cons_payout_units": 29.85,
        "sm_payout_units": 9.95,
        "agg_payout_units": 131.46,
        "cons_exp_profit_units": 1.02,
        "sm_exp_profit_units": 0.34,
        "agg_exp_profit_units": 4.48,
    }
    em = embed_bet_card(sample, "⭐ Best Bet", 0xFFD700)
    view = ViewButtons(sample)
    await interaction.response.send_message(embed=em, view=view)

# =========
# LIFECYCLE
# =========

@bot.event
async def on_ready():
    ensure_tables()
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Slash sync failed:", e)
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN env var")

bot.run(TOKEN)


