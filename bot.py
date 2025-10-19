# bot.py
import os
import math
import json
import logging
import requests
import datetime as dt
from collections import defaultdict

import discord
from discord.ext import commands, tasks
from discord import app_commands

import psycopg2
import psycopg2.extras  # IMPORTANT: needed for RealDictCursor

# --------------- Logging -----------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("valuebets")

# --------------- Config / Env -----------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
BEST_CH_ID = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_CH_ID = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_CH_ID = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
VALUE_TEST_CH_ID = int(os.getenv("VALUE_BETS_TEST_CHANNEL_ID", "0"))
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

BANKROLL_UNITS = 1000
CONSERVATIVE_PCT = 0.015  # 1.5% of bankroll per conservative rule
SMART_BASE = 0.03         # smart stake base as fraction of bankroll
EDGE_SCALE_FOR_AGG = 1.0  # aggressive scales with edge %

ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab", "unibet", "grosvenor"
]

# --------------- Discord Bot -----------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

posted_bet_keys = set()   # de-dupe

# --------------- DB helpers -----------------
DB_OK = False

def _connect():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor
    )

def _ensure_tables(conn):
    with conn.cursor() as cur:
        # Core feed table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id SERIAL PRIMARY KEY,
            bet_key TEXT UNIQUE,
            match TEXT,
            bookmaker TEXT,
            team TEXT,
            odds DOUBLE PRECISION,
            edge DOUBLE PRECISION,
            bet_time TIMESTAMPTZ,
            category TEXT,
            sport TEXT,
            league TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
        # User button clicks
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_bets (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            username TEXT,
            bet_key TEXT,
            sport TEXT,
            league TEXT,
            strategy TEXT,
            units DOUBLE PRECISION,
            odds DOUBLE PRECISION,
            exp_profit DOUBLE PRECISION,
            placed_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
    conn.commit()

def _migrate(conn):
    """Non-destructive migrations — adds any missing columns / indexes."""
    with conn.cursor() as cur:
        # Add columns if they don't exist
        cur.execute("""ALTER TABLE bets
            ADD COLUMN IF NOT EXISTS bet_key TEXT,
            ADD COLUMN IF NOT EXISTS sport TEXT,
            ADD COLUMN IF NOT EXISTS league TEXT,
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();""")
        cur.execute("""ALTER TABLE user_bets
            ADD COLUMN IF NOT EXISTS bet_key TEXT,
            ADD COLUMN IF NOT EXISTS sport TEXT,
            ADD COLUMN IF NOT EXISTS league TEXT,
            ADD COLUMN IF NOT EXISTS strategy TEXT,
            ADD COLUMN IF NOT EXISTS units DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS odds DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS exp_profit DOUBLE PRECISION;""")
        # Indexes
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_bet_key ON bets(bet_key);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_bet_key ON user_bets(bet_key);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_user ON user_bets(user_id);")
    conn.commit()

def db_init():
    global DB_OK
    if not DATABASE_URL:
        DB_OK = False
        log.info("DATABASE_URL not set: running without DB persistence.")
        return
    try:
        conn = _connect()
        _ensure_tables(conn)
        _migrate(conn)
        conn.close()
        DB_OK = True
        log.info("DB ready and migrated.")
    except Exception:
        DB_OK = False
        log.exception("DB init/migrate failed")

def save_bet_row(bet: dict):
    if not DB_OK:
        return
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bets (bet_key, match, bookmaker, team, odds, edge, bet_time, category, sport, league)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (bet_key) DO NOTHING;
            """, (
                bet.get("bet_key"),
                bet.get("match"),
                bet.get("bookmaker"),
                bet.get("team"),
                float(bet.get("odds") or 0),
                float(bet.get("edge") or 0),
                bet.get("bet_time"),
                bet.get("category"),
                bet.get("sport"),
                bet.get("league"),
            ))
        conn.commit()
        conn.close()
    except Exception:
        log.exception("Failed to save bet row")

def save_user_bet(interaction: discord.Interaction, bet: dict, strategy: str, units: float):
    if not DB_OK:
        return "I can’t save that bet right now (database isn’t configured)."

    try:
        # pick expected profit appropriate to strategy
        exp_profit = bet.get("smart_exp_profit", 0.0)
        if strategy == "conservative":
            exp_profit = bet.get("cons_exp_profit", 0.0)
        elif strategy == "aggressive":
            exp_profit = bet.get("agg_exp_profit", 0.0)

        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_bets
                    (user_id, username, bet_key, sport, league, strategy, units, odds, exp_profit)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s);
            """, (
                str(interaction.user.id),
                str(interaction.user),
                bet["bet_key"],
                bet["sport"],
                bet["league"],
                strategy,
                float(units),
                float(bet["odds"]),
                float(exp_profit),
            ))
        conn.commit()
        conn.close()
        return f"✅ Saved your **{strategy}** bet (*{units} units*) on **{bet['match']}**."
    except Exception:
        log.exception("Could not save user_bet")
        return "❌ Could not save your bet. Please check database connectivity."

# --------------- Odds API -----------------
ODDS_BASE = "https://api.the-odds-api.com/v4"

def allowed_bookmaker(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in ALLOWED_BOOKMAKER_KEYS)

def fetch_odds():
    url = f"{ODDS_BASE}/sports/upcoming/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,uk,us",
        "markets": "h2h,totals,spreads",
        "oddsFormat": "decimal"
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("Odds API fetch error: %s", e)
        return []

SPORT_EMOJI = {
    "soccer": "⚽", "football": "🏈", "basketball": "🏀", "tennis": "🎾",
    "baseball": "⚾", "ice_hockey": "🏒", "mma": "🥊", "boxing": "🥊", "golf": "⛳",
    "cricket": "🏏", "rugby_union": "🏉", "rugby_league": "🏉", "handball": "🤾",
    "aussierules": "🏉", "esports": "🎮", "volleyball": "🏐"
}

def sport_to_label_emoji(sport_key: str):
    key = (sport_key or "").lower()
    if "soccer" in key:
        return "Soccer", SPORT_EMOJI.get("soccer", "⚽")
    if key in SPORT_EMOJI:
        return key.replace("_", " ").title(), SPORT_EMOJI[key]
    return "Sport", "🎲"

# --------------- Betting logic -----------------
def calc_bets(data):
    now = dt.datetime.now(dt.timezone.utc)
    out = []

    for ev in data:
        home = ev.get("home_team")
        away = ev.get("away_team")
        if not (home and away):
            continue
        match_name = f"{home} vs {away}"
        commence = ev.get("commence_time")
        try:
            event_dt = dt.datetime.fromisoformat(commence.replace("Z", "+00:00"))
        except Exception:
            continue

        delta = event_dt - now
        if delta.total_seconds() <= 0 or delta > dt.timedelta(days=150):
            continue

        sport_key = ev.get("sport_key", "")
        sport_title = ev.get("sport_title", "")
        league = sport_title or "Unknown League"
        sport_name, sport_emoji = sport_to_label_emoji(sport_key)

        # collect allowed odds
        all_prob = []
        book_outcomes = []
        for book in ev.get("bookmakers", []):
            if not allowed_bookmaker(book.get("title", "")):
                continue
            for market in book.get("markets", []):
                for oc in market.get("outcomes", []):
                    price = oc.get("price")
                    name = oc.get("name")
                    if not price or not name:
                        continue
                    book_outcomes.append((book.get("title"), market.get("key"), name, price))
                    try:
                        all_prob.append(1.0 / float(price))
                    except Exception:
                        pass
        if not all_prob or not book_outcomes:
            continue

        global_consensus = sum(all_prob) / len(all_prob)
        cons_by_outcome = defaultdict(list)
        for btitle, mkey, name, price in book_outcomes:
            try:
                cons_by_outcome[f"{mkey}:{name}"].append(1.0 / float(price))
            except Exception:
                pass

        for btitle, mkey, name, price in book_outcomes:
            try:
                price = float(price)
            except Exception:
                continue
            implied = 1.0 / price
            ok = f"{mkey}:{name}"
            consensus = (sum(cons_by_outcome[ok]) / len(cons_by_outcome[ok])) if cons_by_outcome.get(ok) else global_consensus
            edge = (consensus - implied) * 100.0

            cons_units = round(BANKROLL_UNITS * CONSERVATIVE_PCT, 2)
            smart_units = round(BANKROLL_UNITS * SMART_BASE * max(edge / 10.0, 0.2), 2)
            agg_units = round(cons_units * (1.0 + max(edge, 0)/100.0 * EDGE_SCALE_FOR_AGG), 2)

            cons_payout = round(cons_units * price, 2)
            smart_payout = round(smart_units * price, 2)
            agg_payout = round(agg_units * price, 2)

            cons_exp_profit = round(consensus * cons_payout - cons_units, 2)
            smart_exp_profit = round(consensus * smart_payout - smart_units, 2)
            agg_exp_profit = round(consensus * agg_payout - agg_units, 2)

            out.append({
                "bet_key": f"{match_name}|{name}|{btitle}|{event_dt.isoformat()}",
                "match": match_name,
                "bookmaker": btitle or "Unknown",
                "team": name,
                "odds": price,
                "consensus": round(consensus * 100.0, 2),
                "implied": round(implied * 100.0, 2),
                "edge": round(edge, 2),

                "cons_stake": cons_units,
                "smart_stake": smart_units,
                "agg_stake": agg_units,

                "cons_exp_profit": cons_exp_profit,
                "smart_exp_profit": smart_exp_profit,
                "agg_exp_profit": agg_exp_profit,

                "bet_time": event_dt,
                "quick": (delta <= dt.timedelta(hours=48)),
                "long": (dt.timedelta(hours=48) < delta <= dt.timedelta(days=150)),

                "sport": sport_name,
                "league": league,
                "emoji": sport_emoji
            })

    return out

def value_indicator(b):
    if b["edge"] >= 2.0:
        return "🟢 Value Bet", discord.Colour.green()
    else:
        return "🔴 Low Value", discord.Colour.red()

def pick_best_value(bets):
    vals = [x for x in bets if x["edge"] >= 2.0]
    if not vals:
        return None
    return max(vals, key=lambda x: (x["consensus"] + 1.5 * x["edge"]))

# --------------- Rendering -----------------
def format_bet_embed(b: dict, title: str) -> discord.Embed:
    label, col = value_indicator(b)
    em = discord.Embed(title=title, colour=col)
    em.add_field(name="", value=f"{b['emoji']} **{b['sport']} ({b['league']})**", inline=False)

    em.add_field(name="Match", value=b["match"], inline=False)
    em.add_field(name="Pick", value=f"{b['team']} @ {b['odds']}", inline=False)
    em.add_field(name="Bookmaker", value=b["bookmaker"], inline=False)
    em.add_field(name="Consensus %", value=f"{b['consensus']}%", inline=True)
    em.add_field(name="Implied %", value=f"{b['implied']}%", inline=True)
    em.add_field(name="Edge", value=f"{b['edge']}%", inline=True)
    em.add_field(name="Time", value=b["bet_time"].strftime("%d/%m/%y %H:%M"), inline=False)

    em.add_field(
        name="💵 Conservative Stake",
        value=f"{b['cons_stake']} units → Payout: {(b['cons_stake']*b['odds']):.2f} | Exp. Profit: {b['cons_exp_profit']:.2f}",
        inline=False
    )
    em.add_field(
        name="🧠 Smart Stake",
        value=f"{b['smart_stake']} units → Payout: {(b['smart_stake']*b['odds']):.2f} | Exp. Profit: {b['smart_exp_profit']:.2f}",
        inline=False
    )
    em.add_field(
        name="🔥 Aggressive Stake",
        value=f"{b['agg_stake']} units → Payout: {(b['agg_stake']*b['odds']):.2f} | Exp. Profit: {b['agg_exp_profit']:.2f}",
        inline=False
    )
    em.description = label
    return em

class StakeButtons(discord.ui.View):
    def __init__(self, bet: dict):
        super().__init__(timeout=None)
        self.bet_payload = json.dumps({
            "bet_key": bet["bet_key"],
            "match": bet["match"],
            "sport": bet["sport"],
            "league": bet["league"],
            "odds": bet["odds"],
            "cons_units": bet["cons_stake"],
            "smart_units": bet["smart_stake"],
            "agg_units": bet["agg_stake"],
            "cons_exp_profit": bet["cons_exp_profit"],
            "smart_exp_profit": bet["smart_exp_profit"],
            "agg_exp_profit": bet["agg_exp_profit"],
        })

    @discord.ui.button(label="Conservative", emoji="💵", style=discord.ButtonStyle.secondary, custom_id="stake_cons")
    async def stake_cons(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "conservative")

    @discord.ui.button(label="Smart", emoji="🧠", style=discord.ButtonStyle.primary, custom_id="stake_smart")
    async def stake_smart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "smart")

    @discord.ui.button(label="Aggressive", emoji="🔥", style=discord.ButtonStyle.danger, custom_id="stake_agg")
    async def stake_agg(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "aggressive")

    async def _handle(self, interaction: discord.Interaction, strat: str):
        try:
            b = json.loads(self.bet_payload)
            merged = {
                "bet_key": b["bet_key"],
                "match": b["match"],
                "sport": b["sport"],
                "league": b["league"],
                "odds": b["odds"],
                "cons_exp_profit": b["cons_exp_profit"],
                "smart_exp_profit": b["smart_exp_profit"],
                "agg_exp_profit": b["agg_exp_profit"],
            }
            units = b["cons_units"] if strat == "conservative" else (b["smart_units"] if strat == "smart" else b["agg_units"])
            msg = save_user_bet(interaction, merged, strat, float(units))
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            log.exception("Stake handler failed")
            try:
                await interaction.response.send_message("Sorry, that didn’t work.", ephemeral=True)
            except Exception:
                pass

async def post_one(channel_id: int, bet: dict, title: str, category: str):
    if channel_id <= 0:
        return
    ch = bot.get_channel(channel_id)
    if not ch:
        return
    view = StakeButtons(bet)
    await ch.send(embed=format_bet_embed(bet, title), view=view)
    save_bet_row({**bet, "category": category})

# --------------- Posting/Loop -----------------
async def post_bets(bets):
    if not bets:
        return
    best = pick_best_value(bets)
    if best and best["bet_key"] not in posted_bet_keys:
        posted_bet_keys.add(best["bet_key"])
        await post_one(BEST_CH_ID, best, "⭐ Best Bet", "best")

    quicks = [b for b in bets if b["quick"] and b["bet_key"] not in posted_bet_keys]
    for b in quicks[:5]:
        posted_bet_keys.add(b["bet_key"])
        await post_one(QUICK_CH_ID, b, "⏱ Quick Return Bet", "quick")

    longs = [b for b in bets if b["long"] and b["bet_key"] not in posted_bet_keys]
    for b in longs[:5]:
        posted_bet_keys.add(b["bet_key"])
        await post_one(LONG_CH_ID, b, "📅 Longer Play Bet", "long")

    if VALUE_TEST_CH_ID > 0:
        for b in bets:
            if b["edge"] >= 2.0:
                await post_one(VALUE_TEST_CH_ID, b, "🟢 Value Bet (Testing)", "value-test")

# --------------- Events / Loop -----------------
@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    db_init()
    try:
        await bot.tree.sync()
        log.info("Slash commands synced.")
    except Exception:
        log.exception("Slash sync failed.")
    if not bet_loop.is_running():
        bet_loop.start()

@tasks.loop(seconds=45)
async def bet_loop():
    try:
        data = fetch_odds()
        bets = calc_bets(data)
        await post_bets(bets)
    except Exception:
        log.exception("bet_loop crashed")

# --------------- Slash Commands -----------------
@bot.tree.command(name="ping", description="Bot latency check")
async def ping(ctx: discord.Interaction):
    await ctx.response.send_message(f"Pong! {round(bot.latency*1000)}ms", ephemeral=True)

@bot.tree.command(name="fetchbets", description="Preview next few bets")
async def fetchbets(ctx: discord.Interaction):
    await ctx.response.defer(ephemeral=True)
    data = fetch_odds()
    bets = calc_bets(data)
    if not bets:
        await ctx.followup.send("No bets right now.", ephemeral=True)
        return
    for b in bets[:3]:
        await ctx.followup.send(embed=format_bet_embed(b, "🎲 Bets Preview"), ephemeral=True)

@bot.tree.command(name="roi", description="Show paper-trade ROI (all strategies)")
async def roi(ctx: discord.Interaction):
    if not DB_OK:
        await ctx.response.send_message("DB not configured; no ROI available.", ephemeral=True)
        return
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT SUM(units) AS units, SUM(exp_profit) AS exp_p FROM user_bets;")
            row = cur.fetchone()
        conn.close()
        if not row or not row["units"]:
            await ctx.response.send_message("No saved bets yet.", ephemeral=True)
            return
        units = float(row["units"] or 0)
        exp_p = float(row["exp_p"] or 0.0)
        roi_pct = (exp_p / units * 100.0) if units > 0 else 0.0
        await ctx.response.send_message(
            f"📈 ROI (paper, all strategies): **{roi_pct:.2f}%** across **{units:.2f}** units.",
            ephemeral=True
        )
    except Exception:
        log.exception("ROI failed")
        await ctx.response.send_message("ROI failed.", ephemeral=True)

@bot.tree.command(name="stats", description="Basic paper-trade stats")
async def stats(ctx: discord.Interaction):
    if not DB_OK:
        await ctx.response.send_message("DB not configured; no stats available.", ephemeral=True)
        return
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT strategy, COUNT(*) AS n, SUM(units) AS units, SUM(exp_profit) AS exp_p
                FROM user_bets
                GROUP BY strategy;
            """)
            rows = cur.fetchall()
        conn.close()
        if not rows:
            await ctx.response.send_message("No saved bets yet.", ephemeral=True)
            return
        lines = []
        total_u = 0.0
        total_p = 0.0
        for r in rows:
            u = float(r["units"] or 0)
            p = float(r["exp_p"] or 0)
            total_u += u
            total_p += p
            roi_pct = (p / u * 100.0) if u > 0 else 0.0
            lines.append(f"- **{r['strategy']}** → {int(r['n'])} bets | {u:.2f} units | exp P/L {p:.2f} | ROI {roi_pct:.2f}%")
        lines.append(f"\n**Total** → {total_u:.2f} units | exp P/L {total_p:.2f} | ROI {(total_p/total_u*100.0 if total_u>0 else 0):.2f}%")
        await ctx.response.send_message("\n".join(lines), ephemeral=True)
    except Exception:
        log.exception("Stats failed")
        await ctx.response.send_message("Stats failed.", ephemeral=True)

@bot.tree.command(name="dbcheck", description="Check database connectivity and row counts")
async def dbcheck(ctx: discord.Interaction):
    if not DATABASE_URL:
        await ctx.response.send_message("DATABASE_URL is not set.", ephemeral=True)
        return
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM bets;")
            c1 = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM user_bets;")
            c2 = cur.fetchone()["c"]
        conn.close()
        await ctx.response.send_message(f"✅ DB OK. bets={c1}, user_bets={c2}", ephemeral=True)
    except Exception as e:
        await ctx.response.send_message(f"❌ DB error: {e}", ephemeral=True)

# --------------- Run -----------------
if not TOKEN:
    raise SystemExit("Missing DISCORD_BOT_TOKEN")
bot.run(TOKEN)










