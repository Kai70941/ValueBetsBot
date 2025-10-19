import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import discord
from discord.ext import commands, tasks
import requests
import psycopg2
import psycopg2.extras

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("valuebets")

# ----------------------------
# Config / Env
# ----------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()

CH_BEST  = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0") or "0")
CH_QUICK = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0") or "0")
CH_LONG  = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0") or "0")
CH_VALUE = int(os.getenv("DISCORD_CHANNEL_ID_VALUE", "0") or "0")  # duplicate value stream

DATABASE_URL = os.getenv("DATABASE_PUBLIC_URL", "").strip()

# Units config
CONSERVATIVE_UNITS = float(os.getenv("CONSERVATIVE_UNITS", "15.0"))

# Allowed bookmakers (lowercase substrings)
DEFAULT_BOOKS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
]
ALLOWED_BOOKMAKER_KEYS = [
    s.strip().lower() for s in os.getenv("ALLOWED_BOOKMAKERS", ",".join(DEFAULT_BOOKS)).split(",") if s.strip()
]

# Edge threshold for a "Value Bet" badge
VALUE_EDGE_THRESHOLD = 2.0  # percent

# ----------------------------
# Discord bot
# ----------------------------
intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

# In-memory dedupe for this runtime
posted_keys = set()

# ----------------------------
# DB helpers (psycopg2)
# ----------------------------
DB_OK = bool(DATABASE_URL)

def _connect():
    # Railway public URL works with sslmode=require
    return psycopg2.connect(DATABASE_URL, sslmode="require", cursor_factory=psycopg2.extras.RealDictCursor)

def _migrate():
    if not DB_OK:
        return
    conn = _connect()
    cur = conn.cursor()
    # bets table
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
        consensus DOUBLE PRECISION,
        implied DOUBLE PRECISION,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """)
    # user_bets table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_bets (
        id SERIAL PRIMARY KEY,
        user_id TEXT,
        username TEXT,
        bet_key TEXT,
        event_id TEXT,
        sport TEXT,
        league TEXT,
        strategy TEXT,
        units DOUBLE PRECISION,
        odds DOUBLE PRECISION,
        exp_profit DOUBLE PRECISION,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """)
    # non-destructive adds
    cur.execute("""ALTER TABLE bets
        ADD COLUMN IF NOT EXISTS consensus DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS implied DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS sport TEXT,
        ADD COLUMN IF NOT EXISTS league TEXT,
        ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
        ADD COLUMN IF NOT EXISTS bet_key TEXT UNIQUE;""")
    cur.execute("""ALTER TABLE user_bets
        ADD COLUMN IF NOT EXISTS event_id TEXT,
        ADD COLUMN IF NOT EXISTS sport TEXT,
        ADD COLUMN IF NOT EXISTS league TEXT,
        ADD COLUMN IF NOT EXISTS strategy TEXT,
        ADD COLUMN IF NOT EXISTS units DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS odds DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS exp_profit DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS bet_key TEXT;""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_bet_key ON bets(bet_key);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_bet_key ON user_bets(bet_key);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_user ON user_bets(user_id);")
    conn.commit()
    cur.close()
    conn.close()

def save_bet_row(bet: dict):
    if not DB_OK:
        return
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bets
              (bet_key, match, bookmaker, team, odds, edge, bet_time, category, sport, league, consensus, implied)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
            float(bet.get("consensus") or 0),
            float(bet.get("implied") or 0),
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        log.exception("Failed to save bet row")

def save_user_bet(user: discord.User | discord.Member, bet: dict, strategy: str, units: float, odds: float, exp_profit: float):
    if not DB_OK:
        return False
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_bets
               (user_id, username, bet_key, event_id, sport, league, strategy, units, odds, exp_profit)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
        """, (
            str(user.id), str(user),
            bet.get("bet_key"), bet.get("event_id") or None,
            bet.get("sport"), bet.get("league"),
            strategy, float(units or 0), float(odds or 0), float(exp_profit or 0)
        ))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception:
        log.exception("Failed to save user bet")
        return False

# ----------------------------
# Sports emojis + league naming
# ----------------------------
SPORT_EMOJI = {
    "soccer": "⚽",
    "americanfootball": "🏈",
    "basketball": "🏀",
    "baseball": "⚾",
    "icehockey": "🏒",
    "tennis": "🎾",
    "cricket": "🏏",
    "mma": "🥊",
    "boxing": "🥊",
    "aussierules": "🏉",
    "rugbyleague": "🏉",
    "rugbyunion": "🏉",
    "golf": "⛳",
    "esports": "🎮",
}

def sport_label_and_emoji(sport_key: str, league: str | None) -> str:
    key = (sport_key or "").lower()
    emoji = SPORT_EMOJI.get(key, "🎲")
    # “Soccer” instead of “Football” for that sport
    if key in ("soccer",):
        sport_name = "Soccer"
    elif key == "americanfootball":
        sport_name = "American Football"
    else:
        sport_name = key.capitalize() if key else "Sport"
    lg = league or "Unknown League"
    return f"{emoji} {sport_name} ({lg})"

# ----------------------------
# Helpers
# ----------------------------
def _allowed_bookmaker(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in ALLOWED_BOOKMAKER_KEYS)

def bet_key_from(event_id: str, book: str, market_key: str, outcome_name: str) -> str:
    return f"{event_id}|{book}|{market_key}|{outcome_name}".lower()

# ----------------------------
# Odds fetching + calculations
# ----------------------------
API_BASE = "https://api.the-odds-api.com/v4"

def fetch_upcoming_odds():
    url = f"{API_BASE}/sports/upcoming/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal"
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error("Odds API error: %s", e)
        return []

def calculate_bets(raw):
    now = datetime.now(timezone.utc)
    bets = []

    for ev in raw:
        home = ev.get("home_team")
        away = ev.get("away_team")
        teams = f"{home} vs {away}" if home and away else ev.get("sport_title", "Unknown matchup")

        # event time
        try:
            commence = datetime.fromisoformat(ev.get("commence_time").replace("Z", "+00:00"))
        except Exception:
            continue
        if commence <= now or commence - now > timedelta(days=150):
            continue

        # sport + league
        sport_key = (ev.get("sport_key") or "").lower()
        league = ev.get("sport_title") or None

        # Build consensus probabilities per outcome across allowed books
        per_outcome = defaultdict(list)  # key: f"{market_key}:{outcome_name}" -> [1/price,...]
        for book in ev.get("bookmakers", []):
            if not _allowed_bookmaker(book.get("title", "")):
                continue
            for m in book.get("markets", []):
                mkey = m.get("key")
                for out in m.get("outcomes", []):
                    price = out.get("price")
                    name = out.get("name")
                    if price and name:
                        per_outcome[f"{mkey}:{name}"].append(1.0 / price)

        if not per_outcome:
            continue

        # global average fallback
        all_inv = [p for lst in per_outcome.values() for p in lst]
        global_cons = sum(all_inv) / max(1, len(all_inv))

        # produce candidate bets from each allowed bookmaker
        for book in ev.get("bookmakers", []):
            btitle = book.get("title", "Unknown")
            if not _allowed_bookmaker(btitle):
                continue
            for m in book.get("markets", []):
                mkey = m.get("key")
                for out in m.get("outcomes", []):
                    price = out.get("price")
                    name = out.get("name")
                    if not price or not name:
                        continue

                    implied = 100.0 * (1.0 / price)
                    oc_key = f"{mkey}:{name}"
                    if oc_key in per_outcome and per_outcome[oc_key]:
                        cons = 100.0 * (sum(per_outcome[oc_key]) / len(per_outcome[oc_key]))
                    else:
                        cons = 100.0 * global_cons

                    edge = cons - implied
                    if edge <= 0:
                        # not value; still could be posted as best/other if you want; we skip non-value
                        pass

                    # class
                    delta = commence - now
                    is_quick = delta <= timedelta(hours=48)
                    is_long = not is_quick
                    category = "quick" if is_quick else "long"

                    # stakes (units)
                    cons_units = CONSERVATIVE_UNITS
                    # Smart uses Kelly-ish scale on edge (bounded)
                    kelly_frac = max(0.0, min(edge / 100.0, 0.10))  # cap at 10% of cons stake
                    smart_units = round(cons_units * (1.0 + 2.0 * kelly_frac), 2)
                    aggr_units  = round(cons_units * (1.0 + 5.0 * kelly_frac), 2)

                    # expected profit = p * (units * (odds-1)) - (1-p)*units
                    p = cons / 100.0
                    cons_exp = round(p * (cons_units * (price - 1.0)) - (1 - p) * cons_units, 2)
                    smart_exp = round(p * (smart_units * (price - 1.0)) - (1 - p) * smart_units, 2)
                    aggr_exp  = round(p * (aggr_units * (price - 1.0))  - (1 - p) * aggr_units, 2)

                    bet = {
                        "event_id": ev.get("id") or ev.get("event_id") or ev.get("sport_event_id"),
                        "bet_key": bet_key_from(ev.get("id") or "", btitle, mkey, name),
                        "match": teams,
                        "bookmaker": btitle,
                        "team": f"{name} @ {price}",
                        "odds": float(price),
                        "edge": round(edge, 2),
                        "bet_time": commence,
                        "category": category,
                        "sport": sport_key,
                        "league": league,
                        "consensus": round(cons, 2),
                        "implied": round(implied, 2),

                        "cons_units": round(cons_units, 2),
                        "smart_units": round(smart_units, 2),
                        "aggr_units": round(aggr_units, 2),
                        "cons_exp": cons_exp,
                        "smart_exp": smart_exp,
                        "aggr_exp": aggr_exp,
                    }
                    bets.append(bet)

    return bets

# ----------------------------
# Embeds + Buttons
# ----------------------------
class BetButtons(discord.ui.View):
    def __init__(self, bet: dict):
        super().__init__(timeout=None)
        self.bet = bet

    @discord.ui.button(label="Conservative", style=discord.ButtonStyle.secondary, emoji="💵")
    async def conservative(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok = save_user_bet(
            interaction.user, self.bet, "conservative",
            self.bet["cons_units"], self.bet["odds"], self.bet["cons_exp"]
        )
        if ok:
            await interaction.response.send_message(
                f"Saved **Conservative** bet: {self.bet['match']} | {self.bet['team']} | {self.bet['cons_units']} units",
                ephemeral=True
            )
        else:
            await interaction.response.send_message("❌ Could not save your bet. Is the database configured?", ephemeral=True)

    @discord.ui.button(label="Smart", style=discord.ButtonStyle.primary, emoji="🧠")
    async def smart(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok = save_user_bet(
            interaction.user, self.bet, "smart",
            self.bet["smart_units"], self.bet["odds"], self.bet["smart_exp"]
        )
        if ok:
            await interaction.response.send_message(
                f"Saved **Smart** bet: {self.bet['match']} | {self.bet['team']} | {self.bet['smart_units']} units",
                ephemeral=True
            )
        else:
            await interaction.response.send_message("❌ Could not save your bet. Is the database configured?", ephemeral=True)

    @discord.ui.button(label="Aggressive", style=discord.ButtonStyle.danger, emoji="🔥")
    async def aggressive(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok = save_user_bet(
            interaction.user, self.bet, "aggressive",
            self.bet["aggr_units"], self.bet["odds"], self.bet["aggr_exp"]
        )
        if ok:
            await interaction.response.send_message(
                f"Saved **Aggressive** bet: {self.bet['match']} | {self.bet['team']} | {self.bet['aggr_units']} units",
                ephemeral=True
            )
        else:
            await interaction.response.send_message("❌ Could not save your bet. Is the database configured?", ephemeral=True)

def embed_for_bet(title: str, bet: dict, color: int):
    # Value badge
    indicator = "🟢 Value Bet" if bet.get("edge", 0) >= VALUE_EDGE_THRESHOLD else "🔴 Low Value"
    sport_line = sport_label_and_emoji(bet.get("sport"), bet.get("league"))

    desc = (
        f"{indicator}\n\n"
        f"**{sport_line}**\n\n"
        f"**Match:** {bet['match']}\n"
        f"**Pick:** {bet['team']}\n"
        f"**Bookmaker:** {bet['bookmaker']}\n"
        f"**Consensus %:** {bet['consensus']}%\n"
        f"**Implied %:** {bet['implied']}%\n"
        f"**Edge:** {bet['edge']}%\n"
        f"**Time:** {bet['bet_time'].strftime('%d/%m/%y %H:%M')}\n\n"
        f"🪙 **Conservative Stake:** {bet['cons_units']} units → Payout: {round(bet['cons_units'] * bet['odds'], 2)} | Exp. Profit: {bet['cons_exp']}\n"
        f"🧠 **Smart Stake:** {bet['smart_units']} units → Payout: {round(bet['smart_units'] * bet['odds'], 2)} | Exp. Profit: {bet['smart_exp']}\n"
        f"🔥 **Aggressive Stake:** {bet['aggr_units']} units → Payout: {round(bet['aggr_units'] * bet['odds'], 2)} | Exp. Profit: {bet['aggr_exp']}\n"
    )
    return discord.Embed(title=title, description=desc, color=color)

# ----------------------------
# Posting logic
# ----------------------------
async def post_bet_to_channels(bet: dict):
    # Decide title & color by category or "best"
    title = "Quick Return Bet" if bet["category"] == "quick" else "Longer Play Bet"
    color = 0x2ecc71 if bet.get("edge", 0) >= VALUE_EDGE_THRESHOLD else 0xe74c3c

    # "Best Bet": we pick best separately outside
    if bet.get("_is_best"):
        title = "Best Bet"
        color = 0xf1c40f

    # embed + buttons
    emb = embed_for_bet(title, bet, color)
    view = BetButtons(bet)

    # main destination
    ch_id = CH_QUICK if bet["category"] == "quick" else CH_LONG
    if bet.get("_is_best"):
        ch_id = CH_BEST

    channel = bot.get_channel(ch_id) if ch_id else None
    if channel:
        try:
            await channel.send(embed=emb, view=view)
        except Exception:
            log.exception("Failed to send embed")

    # duplicate value bets to value channel (testing)
    if bet.get("edge", 0) >= VALUE_EDGE_THRESHOLD and CH_VALUE:
        vch = bot.get_channel(CH_VALUE)
        if vch:
            try:
                v_emb = embed_for_bet("Value Bet (Testing)", bet, 0x2ecc71)
                await vch.send(embed=v_emb, view=view)
            except Exception:
                log.exception("Failed to duplicate to value channel")

    # persist feed bet (once)
    save_bet_row(bet)

async def post_pack(bets: list[dict]):
    if not bets:
        return
    # pick a best bet — highest edge & reasonable consensus (>=50)
    best = max(bets, key=lambda b: (b.get("edge", 0), b.get("consensus", 0)))
    if best and best.get("edge", 0) >= VALUE_EDGE_THRESHOLD and best.get("consensus", 0) >= 50:
        best["_is_best"] = True
        if best["bet_key"] not in posted_keys:
            posted_keys.add(best["bet_key"])
            await post_bet_to_channels(best)

    # remaining
    for b in bets:
        if b.get("_is_best"):
            continue
        if b["bet_key"] in posted_keys:
            continue
        posted_keys.add(b["bet_key"])
        await post_bet_to_channels(b)

# ----------------------------
# Scheduler
# ----------------------------
@tasks.loop(minutes=2)
async def bet_loop():
    raw = fetch_upcoming_odds()
    bets = calculate_bets(raw)
    await post_pack(bets)

# ----------------------------
# Slash commands
# ----------------------------
@bot.tree.command(name="ping", description="Ping the bot")
async def ping(ctx: discord.Interaction):
    await ctx.response.send_message("Pong 🏓", ephemeral=True)

@bot.tree.command(name="fetchbets", description="Manually fetch and post bets now")
async def fetchbets(ctx: discord.Interaction):
    await ctx.response.defer(ephemeral=True)
    raw = fetch_upcoming_odds()
    bets = calculate_bets(raw)
    await post_pack(bets)
    await ctx.followup.send(f"Fetched {len(bets)} candidate bets.", ephemeral=True)

@bot.tree.command(name="dbcheck", description="Show DB counts")
async def dbcheck(ctx: discord.Interaction):
    if not DB_OK:
        await ctx.response.send_message("DB not configured.", ephemeral=True)
        return
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM bets;")
        n_bets = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM user_bets;")
        n_ub  = cur.fetchone()["n"]
        cur.close()
        conn.close()
        await ctx.response.send_message(f"bets: {n_bets} | user_bets: {n_ub}", ephemeral=True)
    except Exception:
        log.exception("dbcheck failed")
        await ctx.response.send_message("dbcheck failed.", ephemeral=True)

@bot.tree.command(name="stats", description="Paper-trade stats (bets, win rate, expected P&L, ROI)")
async def stats(ctx: discord.Interaction):
    if not DB_OK:
        await ctx.response.send_message("DB not configured.", ephemeral=True)
        return
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT
              ub.strategy,
              COUNT(*) AS n,
              COALESCE(SUM(ub.units), 0) AS units,
              COALESCE(SUM(ub.exp_profit), 0) AS exp_p,
              AVG(b.consensus) AS avg_consensus
            FROM user_bets ub
            LEFT JOIN bets b ON b.bet_key = ub.bet_key
            GROUP BY ub.strategy
            ORDER BY ub.strategy;
        """)
        per = cur.fetchall()

        cur.execute("""
            SELECT
              COUNT(*) AS n,
              COALESCE(SUM(ub.units), 0) AS units,
              COALESCE(SUM(ub.exp_profit), 0) AS exp_p,
              AVG(b.consensus) AS avg_consensus
            FROM user_bets ub
            LEFT JOIN bets b ON b.bet_key = ub.bet_key;
        """)
        total = cur.fetchone()
        cur.close()
        conn.close()

        if not total or int(total["n"] or 0) == 0:
            await ctx.response.send_message("No saved bets yet.", ephemeral=True)
            return

        lines = []
        for r in per:
            n  = int(r["n"] or 0)
            u  = float(r["units"] or 0)
            ep = float(r["exp_p"] or 0)
            wr = float(r["avg_consensus"] or 0.0)  # %
            roi = (ep / u * 100.0) if u > 0 else 0.0
            lines.append(f"• **{r['strategy']}** → **{n} bets** | **{u:.2f} units** | **Win rate {wr:.2f}%** | **P&L {ep:.2f}** | **ROI {roi:.2f}%**")

        n  = int(total["n"] or 0)
        u  = float(total["units"] or 0)
        ep = float(total["exp_p"] or 0)
        wr = float(total["avg_consensus"] or 0.0)
        roi = (ep / u * 100.0) if u > 0 else 0.0
        lines.append(f"\n**Total** → **{n} bets** | **{u:.2f} units** | **Win rate {wr:.2f}%** | **P&L {ep:.2f}** | **ROI {roi:.2f}%**")

        await ctx.response.send_message("\n".join(lines), ephemeral=True)
    except Exception:
        log.exception("/stats failed")
        await ctx.response.send_message("Stats failed.", ephemeral=True)

# ----------------------------
# Events
# ----------------------------
@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    try:
        _migrate()
        await bot.tree.sync()
        log.info("Slash commands synced.")
    except Exception:
        log.exception("Slash sync/migrate failed")

    if not bet_loop.is_running():
        bet_loop.start()

# ----------------------------
# Main
# ----------------------------
if not TOKEN:
    raise SystemExit("Missing DISCORD_BOT_TOKEN")
if not ODDS_API_KEY:
    log.warning("No ODDS_API_KEY – bot will run but won't fetch odds.")
bot.run(TOKEN)









