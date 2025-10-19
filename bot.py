import os
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import discord
from discord.ext import commands, tasks
import requests

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # allow bot to run without DB on import; we gate at runtime
    psycopg2 = None

logging.basicConfig(level=logging.INFO)

# ===== ENV =====
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

BEST_CH = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_CH = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_CH = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
VALUE_CH = int(os.getenv("VALUE_BETS_CHANNEL_ID", "0"))

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DB_URL = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL")

# ===== CONFIG (units, thresholds) =====
BANKROLL_UNITS = 1000
CONSERVATIVE_UNIT_PCT = 0.015
SMART_MULTIPLIER_PER_EDGE = 0.15
AGG_MULTIPLIER_BASE = 1.1

VALUE_EDGE_THRESHOLD = 2.0  # % edge required to call “Value”
QUICK_WINDOW_HRS = 48
LONG_WINDOW_DAYS = 150

COMMAND_SYNC_ON_READY = True

ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
]

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

# ===== Discord setup =====
intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)
posted_bets = set()   # de-dupe key store


# ===== DB helpers (sslmode=require) =====
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
    return psycopg2.connect(_dsn_with_ssl(DB_URL))

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
                        strategy TEXT,         -- conservative/smart/aggressive
                        stake_units NUMERIC,
                        placed_at TIMESTAMP DEFAULT NOW(),
                        result TEXT,           -- win/lose/push/void
                        return_units NUMERIC,  -- payout - stake (units)
                        settled_at TIMESTAMP
                    );
                """)
    except Exception as e:
        logging.error(f"DB init error: {e}")

def save_bet_to_db(b: dict, category: str):
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

def log_user_bet(user: discord.abc.User, b: dict, strategy: str, units: float) -> str:
    """Insert a tap into user_bets; return '' if ok else error string."""
    if not DB_URL or not psycopg2:
        return "DB URL not configured or driver missing."
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
        return ""
    except Exception as e:
        logging.error(f"user_bets insert error: {e}")
        return str(e)


# ===== Odds logic =====
def bookmaker_allowed(title: str) -> bool:
    return any(k in (title or "").lower() for k in ALLOWED_BOOKMAKER_KEYS)

def fetch_odds():
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
    now = datetime.now(timezone.utc)
    bets = []

    for ev in data:
        home, away = ev.get("home_team"), ev.get("away_team")
        match = f"{home} vs {away}"
        event_id = ev.get("id") or ev.get("event_id")
        sport_key = (ev.get("sport_key") or "").lower()
        sport_title = ev.get("sport_title") or sport_key
        league = sport_title

        commence_time = ev.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
        except Exception:
            continue

        delta = commence_dt - now
        if delta.total_seconds() <= 0 or delta > timedelta(days=LONG_WINDOW_DAYS):
            continue

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
                    consensus = (sum(consensus_by_outcome[k]) / len(consensus_by_outcome[k])
                                 if k in consensus_by_outcome else global_consensus)

                    edge = (consensus - implied) * 100.0

                    con_units = round(BANKROLL_UNITS * CONSERVATIVE_UNIT_PCT, 2)
                    smart_units = round(con_units * (1.0 + (max(0.0, edge) * SMART_MULTIPLIER_PER_EDGE / 1.0)), 2)
                    agg_units = round(con_units * (AGG_MULTIPLIER_BASE + max(0.0, edge) / 100.0), 2)

                    con_payout = round(con_units * float(price), 2)
                    smart_payout = round(smart_units * float(price), 2)
                    agg_payout = round(agg_units * float(price), 2)

                    exp_cons = round(consensus * con_payout - con_units, 2)
                    exp_smart = round(consensus * smart_payout - smart_units, 2)
                    exp_agg = round(consensus * agg_payout - agg_units, 2)

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

                        "con_units": con_units,
                        "smart_units": smart_units,
                        "agg_units": agg_units,
                        "con_payout": con_payout,
                        "smart_payout": smart_payout,
                        "agg_payout": agg_payout,
                        "exp_cons": exp_cons,
                        "exp_smart": exp_smart,
                        "exp_agg": exp_agg,

                        "quick_return": (commence_dt - now) <= timedelta(hours=QUICK_WINDOW_HRS),
                        "long_play": (commence_dt - now) > timedelta(hours=QUICK_WINDOW_HRS)
                    })

    return bets


# ===== Formatting =====
def value_indicator(edge: float) -> str:
    return "🟢 Value Bet" if edge >= VALUE_EDGE_THRESHOLD else "🔴 Low Value"

def bet_identity(b: dict) -> str:
    return f"{b.get('match')}|{b.get('team')}|{b.get('bookmaker')}|{b.get('time')}"

def embed_bet(b: dict, title: str, color: int) -> discord.Embed:
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
        f"🧾 **Conservative Stake:** {b['con_units']}u → Payout: {b['con_payout']}u | Exp. Profit: {b['exp_cons']}u\n"
        f"🧠 **Smart Stake:** {b['smart_units']}u → Payout: {b['smart_payout']}u | Exp. Profit: {b['exp_smart']}u\n"
        f"🔥 **Aggressive Stake:** {b['agg_units']}u → Payout: {b['agg_payout']}u | Exp. Profit: {b['exp_agg']}u\n"
    )
    e = discord.Embed(title=title, description=desc, color=color)
    return e


# ===== One-tap “I Placed” buttons =====
class BetButtons(discord.ui.View):
    def __init__(self, bet: dict):
        super().__init__(timeout=None)
        self.bet = bet

    async def _handle(self, interaction: discord.Interaction, strategy: str, units: float):
        err = log_user_bet(interaction.user, self.bet, strategy, units)
        if err == "":
            await interaction.response.send_message(
                f"✅ Logged **{units}u** on **{self.bet.get('team')}** ({strategy}).",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ Could not save your bet.\n```{err}```",
                ephemeral=True
            )

    @discord.ui.button(label="Conservative", emoji="🧾", style=discord.ButtonStyle.primary, custom_id="con_btn")
    async def con(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "conservative", float(self.bet.get("con_units", 0)))

    @discord.ui.button(label="Smart", emoji="🧠", style=discord.ButtonStyle.success, custom_id="smart_btn")
    async def smart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "smart", float(self.bet.get("smart_units", 0)))

    @discord.ui.button(label="Aggressive", emoji="🔥", style=discord.ButtonStyle.danger, custom_id="agg_btn")
    async def agg(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "aggressive", float(self.bet.get("agg_units", 0)))


# ===== Posting =====
async def post_bets(bets):
    if not bets:
        return

    # “Best” is chosen only from value bets
    value_bets = [b for b in bets if b["edge"] >= VALUE_EDGE_THRESHOLD]
    best = max(value_bets, key=lambda x: (x["consensus"], x["edge"]), default=None)

    quick = [b for b in bets if b["quick_return"]]
    long_ = [b for b in bets if b["long_play"]]

    # BEST
    if best and bet_identity(best) not in posted_bets and BEST_CH:
        posted_bets.add(bet_identity(best))
        ch = bot.get_channel(BEST_CH)
        if ch:
            em = embed_bet(best, "⭐ Best Bet", 0xFFD700)
            em.insert_field_at(0, name="Indicator", value=value_indicator(best['edge']), inline=False)
            await ch.send(embed=em, view=BetButtons(best))
        save_bet_to_db(best, "best")

    # QUICK
    if QUICK_CH:
        ch = bot.get_channel(QUICK_CH)
        for b in quick[:8]:
            if bet_identity(b) in posted_bets:
                continue
            posted_bets.add(bet_identity(b))
            color = (0x2ECC71 if b["edge"] >= VALUE_EDGE_THRESHOLD else 0x95A5A6)
            em = embed_bet(b, "⏱ Quick Return Bet", color)
            em.insert_field_at(0, name="Indicator", value=value_indicator(b['edge']), inline=False)
            await ch.send(embed=em, view=BetButtons(b))
            save_bet_to_db(b, "quick")

    # LONG
    if LONG_CH:
        ch = bot.get_channel(LONG_CH)
        for b in long_[:8]:
            if bet_identity(b) in posted_bets:
                continue
            posted_bets.add(bet_identity(b))
            color = (0x3498DB if b["edge"] >= VALUE_EDGE_THRESHOLD else 0x95A5A6)
            em = embed_bet(b, "📅 Longer Play Bet", color)
            em.insert_field_at(0, name="Indicator", value=value_indicator(b['edge']), inline=False)
            await ch.send(embed=em, view=BetButtons(b))
            save_bet_to_db(b, "long")

    # Duplicate value bets to VALUE feed
    if VALUE_CH:
        ch = bot.get_channel(VALUE_CH)
        for b in value_bets[:10]:
            key = f"value|{bet_identity(b)}"
            if key in posted_bets:
                continue
            posted_bets.add(key)
            em = embed_bet(b, "🟢 Value Bet (Testing)", 0x2ECC71)
            await ch.send(embed=em, view=BetButtons(b))
            save_bet_to_db(b, "value")


# ===== Commands =====
@bot.tree.command(name="ping", description="Report bot latency.")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency*1000)} ms", ephemeral=True)

@bot.tree.command(name="dbcheck", description="Verify database connectivity & tables.")
async def dbcheck_cmd(interaction: discord.Interaction):
    if not DB_URL or not psycopg2:
        await interaction.response.send_message("DB not configured (missing URL or driver).", ephemeral=True)
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
        await interaction.response.send_message("✅ DB connection OK (sslmode=require).", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ DB error:\n```{e}```", ephemeral=True)

@bot.tree.command(name="fetchbets", description="Preview a few incoming bets now (ephemeral).")
async def fetchbets_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = fetch_odds()
    bets = calc_bets(data)[:5]
    if not bets:
        await interaction.followup.send("No bets right now.", ephemeral=True)
        return
    lines = [f"**{b['match']}** — {b['team']} @ {b['odds']} ({b['bookmaker']}) | Edge {b['edge']}%" for b in bets]
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
                           COALESCE(SUM(return_units),0) AS net
                    FROM user_bets WHERE result IS NOT NULL
                """)
                row = cur.fetchone() or {"stake": 0, "net": 0}
        stake = float(row["stake"] or 0)
        net = float(row["net"] or 0)
        roi = 0.0 if stake <= 0 else (net / stake) * 100.0
        await interaction.response.send_message(
            f"📈 ROI (settled only): **{roi:.2f}%**\nStake: {stake:.2f}u | Net: {net:.2f}u",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(f"ROI error:\n```{e}```", ephemeral=True)

@bot.tree.command(name="stats", description="All-time paper-trading stats (settled user bets).")
async def stats_cmd(interaction: discord.Interaction):
    if not DB_URL or not psycopg2:
        await interaction.response.send_message("DB not configured.", ephemeral=True)
        return
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # overall
                cur.execute("""
                    SELECT COUNT(*) AS n,
                           COALESCE(SUM(stake_units),0) AS stake,
                           COALESCE(SUM(return_units),0) AS net
                    FROM user_bets WHERE result IS NOT NULL
                """)
                o = cur.fetchone() or {"n": 0, "stake": 0, "net": 0}

                # by strategy
                cur.execute("""
                    SELECT strategy,
                           COUNT(*) AS n,
                           COALESCE(SUM(stake_units),0) AS stake,
                           COALESCE(SUM(return_units),0) AS net
                    FROM user_bets WHERE result IS NOT NULL
                    GROUP BY strategy
                """)
                rows = cur.fetchall() or []

        stake = float(o["stake"] or 0)
        net = float(o["net"] or 0)
        roi = 0.0 if stake <= 0 else (net / stake) * 100.0

        lines = [f"**All-time:** bets {o['n']} | stake {stake:.2f}u | net {net:.2f}u | ROI {roi:.2f}%"]
        if rows:
            lines.append("\n**By strategy**")
            for r in rows:
                sstake = float(r["stake"] or 0)
                snet = float(r["net"] or 0)
                sroi = 0.0 if sstake <= 0 else (snet / sstake) * 100.0
                lines.append(f"- {r['strategy']}: n {r['n']} | stake {sstake:.2f}u | net {snet:.2f}u | ROI {sroi:.2f}%")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Stats error:\n```{e}```", ephemeral=True)


# ===== Lifecycle =====
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


# ===== Start =====
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_BOT_TOKEN")
    bot.run(TOKEN)

















