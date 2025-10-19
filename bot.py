# bot.py
import os, math, logging, asyncio, json
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks

import requests
import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO)

# ------------- ENV -----------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHAN_BEST  = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
CHAN_QUICK = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
CHAN_LONG  = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
CHAN_VALUE = int(os.getenv("VALUE_BETS_CHANNEL_ID", "0"))  # duplicate value bets
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DB_URL = os.getenv("DATABASE_PUBLIC_URL")  # use the public URL on Railway

# ------------- CONSTANTS -----------
BANKROLL_UNITS = 1000  # internal bankroll units
CONSERVATIVE_PCT = 0.015  # 1.5% default conservative stake
VALUE_EDGE_THRESHOLD = 2.0  # % edge to call it a "Value Bet"

ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
]

def allowed_bookmaker(title: str) -> bool:
    return any(k in (title or "").lower() for k in ALLOWED_BOOKMAKER_KEYS)

SPORT_EMOJI = {
    "soccer": "⚽", "basketball": "🏀", "americanfootball": "🏈",
    "icehockey": "🏒", "mma": "🥊", "boxing": "🥊",
    "baseball": "⚾", "tennis": "🎾", "tabletennis": "🏓",
    "volleyball": "🏐", "aussierules": "🏉", "cricket": "🏏",
    "darts": "🎯", "rugby": "🏉", "handball": "🤾", "csgo": "🎮",
}

def sport_label(sport_key: str) -> str:
    if not sport_key: return "Sport"
    k = sport_key.lower().replace("-", "").replace("_","")
    # normalise soccer vs football
    if "soccer" in sport_key.lower(): k = "soccer"
    if "americanfootball" in k or "nfl" in k or "ncaaf" in k: k = "americanfootball"
    emoji = SPORT_EMOJI.get(k, "🏟️")
    simple = " ".join(part.capitalize() for part in sport_key.replace("_"," ").split())
    return f"{emoji} {simple}"

# ---------- DB helpers & migration ----------
def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)

def init_db():
    """Create/upgrade tables safely (idempotent)."""
    if not DB_URL: 
        logging.warning("DATABASE_PUBLIC_URL not set")
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # base: bets (system paper-trades / feed)
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
                    ALTER TABLE bets
                      ADD COLUMN IF NOT EXISTS event_id TEXT,
                      ADD COLUMN IF NOT EXISTS sport_key TEXT,
                      ADD COLUMN IF NOT EXISTS match TEXT,
                      ADD COLUMN IF NOT EXISTS bookmaker TEXT,
                      ADD COLUMN IF NOT EXISTS team TEXT,
                      ADD COLUMN IF NOT EXISTS odds NUMERIC,
                      ADD COLUMN IF NOT EXISTS edge NUMERIC,
                      ADD COLUMN IF NOT EXISTS bet_time TIMESTAMP,
                      ADD COLUMN IF NOT EXISTS category TEXT,
                      ADD COLUMN IF NOT EXISTS sport TEXT,
                      ADD COLUMN IF NOT EXISTS league TEXT,
                      ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();
                """)
                # user_bets (click-to-log buttons go here)
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
                        return_units NUMERIC,  -- profit in units (payout - stake)
                        settled_at TIMESTAMP
                    );
                """)
                cur.execute("""
                    ALTER TABLE user_bets
                      ADD COLUMN IF NOT EXISTS event_id TEXT,
                      ADD COLUMN IF NOT EXISTS sport_key TEXT,
                      ADD COLUMN IF NOT EXISTS match TEXT,
                      ADD COLUMN IF NOT EXISTS team TEXT,
                      ADD COLUMN IF NOT EXISTS bookmaker TEXT,
                      ADD COLUMN IF NOT EXISTS odds NUMERIC,
                      ADD COLUMN IF NOT EXISTS strategy TEXT,
                      ADD COLUMN IF NOT EXISTS stake_units NUMERIC,
                      ADD COLUMN IF NOT EXISTS placed_at TIMESTAMP DEFAULT NOW(),
                      ADD COLUMN IF NOT EXISTS result TEXT,
                      ADD COLUMN IF NOT EXISTS return_units NUMERIC,
                      ADD COLUMN IF NOT EXISTS settled_at TIMESTAMP;
                """)
    except Exception as e:
        logging.exception(f"DB init error: {e}")

def save_bet_row(row: dict):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                  INSERT INTO bets (event_id,sport_key,match,bookmaker,team,odds,edge,bet_time,category,sport,league)
                  VALUES (%(event_id)s,%(sport_key)s,%(match)s,%(bookmaker)s,%(team)s,%(odds)s,%(edge)s,%(bet_time)s,%(category)s,%(sport)s,%(league)s);
                """, row)
    except Exception as e:
        logging.exception(f"save_bet_row failed: {e}")

def save_user_bet(user_id, username, row, strategy, stake_units):
    payload = {
        "user_id": user_id,
        "username": username,
        "bet_key": row["bet_key"],
        "event_id": row.get("event_id"),
        "sport_key": row.get("sport_key"),
        "match": row.get("match"),
        "team": row.get("team"),
        "bookmaker": row.get("bookmaker"),
        "odds": row.get("odds"),
        "strategy": strategy,
        "stake_units": stake_units
    }
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_bets (user_id,username,bet_key,event_id,sport_key,match,team,bookmaker,odds,strategy,stake_units)
                    VALUES (%(user_id)s,%(username)s,%(bet_key)s,%(event_id)s,%(sport_key)s,%(match)s,%(team)s,%(bookmaker)s,%(odds)s,%(strategy)s,%(stake_units)s);
                """, payload)
        return True, None
    except Exception as e:
        logging.exception("save_user_bet failed")
        return False, str(e)

# ------------- Discord bot ------------
intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

# posted bet keys to avoid duplicates
posted = set()

# -------- Odds fetch & compute ----------
def fetch_odds():
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"
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
        logging.warning(f"Odds API error: {e}")
        return []

def calc_bets(data):
    now = datetime.now(timezone.utc)
    out = []
    for ev in data:
        home, away = ev.get("home_team"), ev.get("away_team")
        match = f"{home} vs {away}"
        commence = ev.get("commence_time")
        try:
            dt = datetime.fromisoformat(commence.replace("Z","+00:00"))
        except:
            continue
        d = dt - now
        if d.total_seconds() <= 0 or d > timedelta(days=150):
            continue

        # consensus from allowed books
        market_prices = []
        for bk in ev.get("bookmakers", []):
            if not allowed_bookmaker(bk.get("title","")):
                continue
            for m in bk.get("markets", []):
                for o in m.get("outcomes", []):
                    p = o.get("price")
                    if p and p > 1.01:
                        market_prices.append(1.0/p)

        if not market_prices: 
            continue
        consensus_p = sum(market_prices)/len(market_prices)

        # for each book/outcome – compute edge etc.
        for bk in ev.get("bookmakers", []):
            if not allowed_bookmaker(bk.get("title","")):
                continue
            for m in bk.get("markets", []):
                for o in m.get("outcomes", []):
                    price = o.get("price")
                    name  = o.get("name")
                    if not price or not name: 
                        continue
                    implied = 1.0/price
                    edge = (consensus_p - implied) * 100.0
                    category = "quick" if d <= timedelta(hours=48) else "long"
                    # build result row
                    sport_key = ev.get("sport_key") or ev.get("sport", "")
                    league = ev.get("sport_title") or ""  # Odds API often provides sport_title (league-ish)
                    row = {
                        "event_id": ev.get("id") or f"{match}-{dt.isoformat()}",
                        "sport_key": sport_key,
                        "match": match,
                        "bookmaker": bk.get("title","Unknown"),
                        "team": name,
                        "odds": price,
                        "edge": round(edge,2),
                        "bet_time": dt,
                        "category": category,
                        "sport": sport_key,
                        "league": league
                    }
                    # enrich for embed
                    row["consensus_pct"] = round(consensus_p*100.0,2)
                    row["implied_pct"]   = round(implied*100.0,2)
                    row["win_prob"]      = consensus_p
                    row["bet_key"]       = f"{row['match']}|{row['team']}|{row['bookmaker']}|{dt.isoformat()}"
                    out.append(row)
    return out

# ---------- stake calc (units) ----------
def stake_lines(row):
    edge = max(row["edge"], 0.0)
    cons = round(BANKROLL_UNITS * CONSERVATIVE_PCT, 2)
    # smart grows mildly with edge; aggressive grows a bit more
    smart = round(cons * (1.0 + edge/100.0), 2)
    aggr  = round(cons * (1.5 + edge/80.0), 2)

    cons_pay = round(cons * row["odds"], 2)
    smart_pay = round(smart * row["odds"], 2)
    aggr_pay  = round(aggr  * row["odds"], 2)

    cons_exp = round(row["win_prob"] * cons_pay - cons, 2)
    smart_exp= round(row["win_prob"] * smart_pay - smart, 2)
    aggr_exp = round(row["win_prob"] * aggr_pay  - aggr, 2)

    return {
        "conservative": (cons, cons_pay, cons_exp),
        "smart":        (smart, smart_pay, smart_exp),
        "aggressive":   (aggr,  aggr_pay,  aggr_exp)
    }

def value_indicator(edge):
    return "🟢 Value Bet" if edge >= VALUE_EDGE_THRESHOLD else "🔴 Low Value"

def title_for_category(cat):
    return "⏱ Quick Return Bet" if cat=="quick" else "📅 Longer Play Bet"

def sport_header(row):
    label = sport_label(row.get("sport_key") or row.get("sport") or "Sport")
    league = row.get("league") or "Unknown League"
    return f"{label} ({league})"

class LogBetView(discord.ui.View):
    def __init__(self, row, stakes, timeout=180):
        super().__init__(timeout=timeout)
        self.row = row
        self.stakes = stakes

    @discord.ui.button(label="Conservative", style=discord.ButtonStyle.secondary, emoji="💵")
    async def cons_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._save(interaction, "conservative", self.stakes["conservative"][0])

    @discord.ui.button(label="Smart", style=discord.ButtonStyle.primary, emoji="🧠")
    async def smart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._save(interaction, "smart", self.stakes["smart"][0])

    @discord.ui.button(label="Aggressive", style=discord.ButtonStyle.danger, emoji="🔥")
    async def aggr_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._save(interaction, "aggressive", self.stakes["aggressive"][0])

    async def _save(self, interaction: discord.Interaction, strategy: str, stake_units: float):
        ok, err = save_user_bet(
            interaction.user.id, interaction.user.display_name or interaction.user.name,
            self.row, strategy, stake_units
        )
        if ok:
            await interaction.response.send_message("✅ Saved your bet.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Could not save your bet. {err or 'Is the database configured?'}", ephemeral=True)

def build_embed(row):
    title = title_for_category(row["category"])
    indicator = value_indicator(row["edge"])
    color = 0x2ECC71 if row["edge"] >= VALUE_EDGE_THRESHOLD else 0xE74C3C
    sportline = sport_header(row)
    stakes = stake_lines(row)
    cons, cons_pay, cons_exp   = stakes["conservative"]
    smart, smart_pay, smart_exp= stakes["smart"]
    aggr, aggr_pay, aggr_exp   = stakes["aggressive"]

    desc = (
        f"{indicator}\n\n"
        f"**{sportline}**\n\n"
        f"**Match:** {row['match']}\n"
        f"**Pick:** {row['team']} @ {row['odds']}\n"
        f"**Bookmaker:** {row['bookmaker']}\n"
        f"**Consensus %:** {row['consensus_pct']}%\n"
        f"**Implied %:** {row['implied_pct']}%\n"
        f"**Edge:** {row['edge']}%\n"
        f"**Time:** {row['bet_time'].strftime('%d/%m/%y %H:%M')}\n\n"
        f"💵 **Conservative Stake:** {cons} units → Payout: {cons_pay} | Exp. Profit: {cons_exp}\n"
        f"🧠 **Smart Stake:** {smart} units → Payout: {smart_pay} | Exp. Profit: {smart_exp}\n"
        f"🔥 **Aggressive Stake:** {aggr} units → Payout: {aggr_pay} | Exp. Profit: {aggr_exp}\n"
    )
    em = discord.Embed(title=title, description=desc, color=color)
    return em, stakes

async def post_rows(rows):
    if not rows: return

    # choose a best bet that is actually "value"
    value_rows = [r for r in rows if r["edge"] >= VALUE_EDGE_THRESHOLD]
    best = max(value_rows, key=lambda r: (r["win_prob"]*r["odds"]), default=None)

    # save + send best
    if best and best["bet_key"] not in posted:
        posted.add(best["bet_key"])
        save_bet_row(best)
        ch = bot.get_channel(CHAN_BEST) if CHAN_BEST else None
        if ch:
            em, stakes = build_embed(best)
            # override title for best
            em.title = "⭐ Best Bet"
            view = LogBetView(best, stakes)
            await ch.send(embed=em, view=view)

    # quick + long
    for r in rows:
        if r["bet_key"] in posted: 
            continue
        save_bet_row(r)
        posted.add(r["bet_key"])

        # duplicate value bets to VALUE channel
        dup_value = (r["edge"] >= VALUE_EDGE_THRESHOLD) and CHAN_VALUE

        em, stakes = build_embed(r)
        view = LogBetView(r, stakes)

        if r["category"] == "quick" and CHAN_QUICK:
            await bot.get_channel(CHAN_QUICK).send(embed=em, view=view)
        elif r["category"] == "long" and CHAN_LONG:
            await bot.get_channel(CHAN_LONG).send(embed=em, view=view)

        if dup_value:
            em2 = em.copy()
            em2.title = "⭐ Value Bet"
            await bot.get_channel(CHAN_VALUE).send(embed=em2, view=LogBetView(r, stakes))

# -------- Bot events & loop ----------
@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user} ({bot.user.id})")
    init_db()
    try:
        await bot.tree.sync()
        logging.info("Slash commands synced.")
    except Exception as e:
        logging.warning(f"Slash sync failed: {e}")
    if not bet_loop.is_running():
        bet_loop.start()

@tasks.loop(seconds=60)
async def bet_loop():
    data = fetch_odds()
    rows = calc_bets(data)
    await post_rows(rows)

# --------- Slash commands ------------
@bot.tree.command(name="ping", description="Bot latency check")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 pong", ephemeral=True)

@bot.tree.command(name="roi", description="Paper-trade ROI (all strategies and by strategy)")
async def roi_cmd(interaction: discord.Interaction):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # total
                cur.execute("""
                  SELECT COALESCE(SUM(stake_units),0) AS st, COALESCE(SUM(return_units),0) AS rt
                  FROM user_bets WHERE result IS NOT NULL
                """)
                row = cur.fetchone()
                st, rt = float(row["st"]), float(row["rt"])
                total_roi = (rt / st * 100.0) if st > 0 else 0.0

                # by strategy
                cur.execute("""
                  SELECT strategy, COALESCE(SUM(stake_units),0) AS st, COALESCE(SUM(return_units),0) AS rt
                  FROM user_bets WHERE result IS NOT NULL
                  GROUP BY strategy
                """)
                parts = []
                for r in cur.fetchall():
                    stg = r["strategy"] or "unknown"
                    stg_st, stg_rt = float(r["st"]), float(r["rt"])
                    roi = (stg_rt/stg_st*100.0) if stg_st>0 else 0.0
                    parts.append(f"- **{stg.capitalize()}**: ROI {roi:.2f}% (stake {stg_st:.2f}u)")

        msg = f"📈 **ROI (all)**: {total_roi:.2f}%\n" + ("\n".join(parts) if parts else "_No settled bets yet_")
        await interaction.response.send_message(msg, ephemeral=True)
    except Exception as e:
        logging.exception("ROI failed")
        await interaction.response.send_message("❌ Failed to compute ROI.", ephemeral=True)

@bot.tree.command(name="stats", description="Win rate, totals and P&L from your click-logged bets")
async def stats_cmd(interaction: discord.Interaction):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                  SELECT COUNT(*) AS total,
                         SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) AS wins,
                         SUM(COALESCE(return_units,0)) AS pnl
                  FROM user_bets
                """)
                r = cur.fetchone()
                total = int(r["total"] or 0)
                wins  = int(r["wins"] or 0)
                pnl   = float(r["pnl"] or 0.0)
                wr = (wins/total*100.0) if total>0 else 0.0
                await interaction.response.send_message(
                    f"📊 **Totals**: {total} bets\n"
                    f"✅ **Wins**: {wins}  |  ⚖️ **Win Rate**: {wr:.2f}%\n"
                    f"💰 **P&L**: {pnl:.2f} units",
                    ephemeral=True
                )
    except Exception as e:
        logging.exception("stats failed")
        await interaction.response.send_message("❌ Failed to compute stats.", ephemeral=True)

# ------------- run -------------------
if not TOKEN:
    raise SystemExit("DISCORD_BOT_TOKEN not set")
bot.run(TOKEN)
















