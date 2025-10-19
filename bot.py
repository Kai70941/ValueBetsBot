import os
import re
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
CH_VALUE = int(os.getenv("DISCORD_CHANNEL_ID_VALUE", "0") or "0")  # duplicate stream for value bets (testing)

# Optional: role ping for Best Bet alerts
ALERT_ROLE_ID = int(os.getenv("ALERT_ROLE_ID", "0") or "0")

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
# DB URL auto-detect (no renaming needed)
# ----------------------------
def _get_db_url():
    candidates = [
        "DATABASE_PUBLIC_URL",   # Railway public/proxy
        "DATABASE_URL",          # Generic/Heroku-style
        "DATABASE_INTERNAL_URL", # sometimes used in templates
        "DB_URL",                # fallback/custom
    ]
    for name in candidates:
        val = (os.getenv(name) or "").strip()
        if val:
            return name, val
    return None, ""

DB_VAR_NAME, DATABASE_URL = _get_db_url()
DB_OK = bool(DATABASE_URL)

def _mask(url: str) -> str:
    # mask credentials in logs: keep scheme/host/port/db, hide user:pass
    return re.sub(r"//[^:@/]+:[^@/]+@", "//***:***@", url)

# ----------------------------
# DB helpers (psycopg2)
# ----------------------------
def _connect():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor
    )

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
    # settings table (for mode etc.)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        id BOOLEAN PRIMARY KEY DEFAULT TRUE,
        mode TEXT DEFAULT 'live',  -- 'live' or 'test'
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """)
    cur.execute("""
        INSERT INTO settings (id, mode) VALUES (TRUE, 'live')
        ON CONFLICT (id) DO NOTHING;
    """)
    # indexes
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_bet_key ON bets(bet_key);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_bet_key ON user_bets(bet_key);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_user ON user_bets(user_id);")
    conn.commit()
    cur.close()
    conn.close()
    log.info("DB migration complete (using %s -> %s)", DB_VAR_NAME or "NONE", _mask(DATABASE_URL) if DB_OK else "N/A")

def get_mode() -> str:
    if not DB_OK:
        return "live"
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("SELECT mode FROM settings WHERE id=TRUE;")
        row = cur.fetchone()
        cur.close()
        conn.close()
        return (row["mode"] if row and row.get("mode") else "live")
    except Exception:
        log.exception("get_mode failed")
        return "live"

def set_mode(new_mode: str) -> bool:
    if not DB_OK:
        return False
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("UPDATE settings SET mode=%s, updated_at=NOW() WHERE id=TRUE;", (new_mode,))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception:
        log.exception("set_mode failed")
        return False

def save_bet_row(bet: dict):
    if not DB_OK:
        return False
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
        logging.info("Saved bet: %s", bet.get("bet_key"))
        return True
    except Exception:
        log.exception("Failed to save bet row")
        return False

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

def fetch_bet_row(bet_key: str):
    if not DB_OK:
        return None
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("SELECT * FROM bets WHERE bet_key=%s LIMIT 1;", (bet_key,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row
    except Exception:
        log.exception("fetch_bet_row failed")
        return None

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

def compute_units_and_profit(edge: float, odds: float):
    cons_units = CONSERVATIVE_UNITS
    kelly_frac = max(0.0, min((edge or 0) / 100.0, 0.10))
    smart_units = round(cons_units * (1.0 + 2.0 * kelly_frac), 2)
    aggr_units  = round(cons_units * (1.0 + 5.0 * kelly_frac), 2)
    return cons_units, smart_units, aggr_units

def exp_profit(units: float, odds: float, consensus_pct: float):
    p = max(0.0, min(1.0, (consensus_pct or 0) / 100.0))
    return round(p * (units * (odds - 1.0)) - (1 - p) * units, 2)

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
            for m in book in book.get("markets", []):
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
                    # class
                    delta = commence - now
                    is_quick = delta <= timedelta(hours=48)
                    category = "quick" if is_quick else "long"

                    # stakes (units)
                    cons_units = CONSERVATIVE_UNITS
                    kelly_frac = max(0.0, min(edge / 100.0, 0.10))  # cap at 10% of cons stake
                    smart_units = round(cons_units * (1.0 + 2.0 * kelly_frac), 2)
                    aggr_units  = round(cons_units * (1.0 + 5.0 * kelly_frac), 2)

                    # expected profit (with consensus as win prob)
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
# Embeds + Button View (ID-routed)
# ----------------------------
def build_bet_view(bet: dict) -> discord.ui.View:
    """Return a view whose buttons carry custom_ids so we can handle them in on_interaction."""
    view = discord.ui.View(timeout=None)
    # custom_id schema: place|<bet_key>|<strategy>
    view.add_item(discord.ui.Button(
        label="Conservative", emoji="💵", style=discord.ButtonStyle.secondary,
        custom_id=f"place|{bet['bet_key']}|conservative"
    ))
    view.add_item(discord.ui.Button(
        label="Smart", emoji="🧠", style=discord.ButtonStyle.primary,
        custom_id=f"place|{bet['bet_key']}|smart"
    ))
    view.add_item(discord.ui.Button(
        label="Aggressive", emoji="🔥", style=discord.ButtonStyle.danger,
        custom_id=f"place|{bet['bet_key']}|aggressive"
    ))
    return view

def embed_for_bet(title: str, bet: dict, color: int):
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
    emb = discord.Embed(title=title, description=desc, color=color)
    return emb

# ----------------------------
# Posting logic (respects mode + role alert)
# ----------------------------
async def post_bet_to_channels(bet: dict):
    # Save first (so buttons can fetch from DB even after restart)
    save_bet_row(bet)

    # Determine mode
    mode = get_mode()  # 'live' or 'test'

    # Prepare embed/view
    title = "Quick Return Bet" if bet["category"] == "quick" else "Longer Play Bet"
    color = 0x2ecc71 if bet.get("edge", 0) >= VALUE_EDGE_THRESHOLD else 0xe74c3c
    if bet.get("_is_best"):
        title = "Best Bet"
        color = 0xf1c40f
    emb  = embed_for_bet(title, bet, color)
    view = build_bet_view(bet)

    # Post destination(s)
    targets = []
    if bet.get("_is_best"):
        # Best bet goes to best channel; in test mode, also mirror to value test channel only if CH_VALUE set
        if mode == "live" and CH_BEST:
            targets.append(CH_BEST)
        elif mode == "test" and CH_VALUE:
            targets.append(CH_VALUE)
    else:
        if mode == "live":
            ch_id = CH_QUICK if bet["category"] == "quick" else CH_LONG
            if ch_id:
                targets.append(ch_id)
        elif mode == "test" and CH_VALUE:
            targets.append(CH_VALUE)

    # Duplicate value bets to value channel (testing stream) in both modes if configured
    duplicate_to_value = (bet.get("edge", 0) >= VALUE_EDGE_THRESHOLD and CH_VALUE)

    # Send messages
    for ch_id in targets:
        channel = bot.get_channel(ch_id)
        if channel:
            try:
                # Role ping for Best Bet if configured and in live mode
                if bet.get("_is_best") and ALERT_ROLE_ID and mode == "live":
                    role_mention = f"<@&{ALERT_ROLE_ID}>"
                    await channel.send(content=role_mention)
                await channel.send(embed=emb, view=view)
            except Exception:
                log.exception("Failed to send embed")

    if duplicate_to_value:
        vch = bot.get_channel(CH_VALUE)
        if vch:
            try:
                v_title = "Value Bet (Testing)" if not bet.get("_is_best") else "Best Bet (Testing Mirror)"
                v_emb = embed_for_bet(v_title, bet, 0x2ecc71 if not bet.get("_is_best") else 0xf1c40f)
                await vch.send(embed=v_emb, view=view)
            except Exception:
                log.exception("Failed duplicate to value channel")

async def post_pack(bets: list[dict]):
    if not bets:
        return
    # select a genuinely strong best bet
    candidates = [b for b in bets if b.get("edge", 0) >= VALUE_EDGE_THRESHOLD and b.get("consensus", 0) >= 50]
    if candidates:
        best = max(candidates, key=lambda b: (b.get("edge", 0), b.get("consensus", 0)))
        if best["bet_key"] not in posted_keys:
            best["_is_best"] = True
            posted_keys.add(best["bet_key"])
            await post_bet_to_channels(best)

    # rest
    for b in bets:
        if b.get("_is_best"):
            continue
        if b["bet_key"] in posted_keys:
            continue
        posted_keys.add(b["bet_key"])
        await post_bet_to_channels(b)

# ----------------------------
# Button handler (survives restarts)
# ----------------------------
@bot.listen("on_interaction")
async def handle_place_buttons(inter: discord.Interaction):
    """Handle clicks on our custom-id buttons: place|<bet_key>|<strategy>"""
    try:
        if inter.type != discord.InteractionType.component:
            return
        cid = inter.data.get("custom_id", "")
        if not cid.startswith("place|"):
            return

        await inter.response.defer(ephemeral=True, thinking=True)

        try:
            _, bet_key, strategy = cid.split("|", 2)
        except ValueError:
            await inter.followup.send("Invalid button payload.", ephemeral=True)
            return

        row = fetch_bet_row(bet_key)
        if not row:
            await inter.followup.send("Sorry, I couldn't find this bet in the database.", ephemeral=True)
            return

        # Recompute stakes using stored edge/odds and expected profit using stored consensus
        edge = float(row.get("edge") or 0.0)
        odds = float(row.get("odds") or 0.0)
        consensus = float(row.get("consensus") or 50.0)

        cons_units, smart_units, aggr_units = compute_units_and_profit(edge, odds)
        if strategy == "conservative":
            units = cons_units
        elif strategy == "smart":
            units = smart_units
        else:
            strategy = "aggressive"
            units = aggr_units

        ep = exp_profit(units, odds, consensus)

        bet_payload = {
            "bet_key": row["bet_key"],
            "event_id": None,
            "sport": row.get("sport"),
            "league": row.get("league"),
        }

        ok = save_user_bet(inter.user, bet_payload, strategy, units, odds, ep)
        if ok:
            # Ephemeral confirm
            await inter.followup.send(
                f"Saved **{strategy.capitalize()}** bet for **{row['match']}** — {row['team']} | {units:.2f} units",
                ephemeral=True
            )
            # Light DM (optional, ignore errors)
            try:
                await inter.user.send(f"✅ Saved your {strategy} bet: {row['match']} — {row['team']} ({units:.2f} units at {odds})")
            except Exception:
                pass
        else:
            await inter.followup.send("❌ Could not save your bet. Is the database configured?", ephemeral=True)

    except Exception:
        log.exception("handle_place_buttons error")

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

@bot.tree.command(name="dblatest", description="Show 5 latest bets")
async def dblatest(ctx: discord.Interaction):
    if not DB_OK:
        await ctx.response.send_message("DB not configured.", ephemeral=True)
        return
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT match, team, bookmaker, category, created_at
            FROM bets ORDER BY created_at DESC LIMIT 5;
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if not rows:
            await ctx.response.send_message("No rows found.", ephemeral=True)
            return
        msg = "\n".join([f"• {r['match']} | {r['team']} | {r['bookmaker']} | {r['category']} | {r['created_at']:%Y-%m-%d %H:%M}" for r in rows])
        await ctx.response.send_message(msg, ephemeral=True)
    except Exception:
        log.exception("dblatest failed")
        await ctx.response.send_message("dblatest failed.", ephemeral=True)

# (#3) Stats with Top Performing League
@bot.tree.command(name="stats", description="Paper-trade stats (bets, win rate, expected P&L, ROI) + best league")
async def stats(ctx: discord.Interaction):
    if not DB_OK:
        await ctx.response.send_message("DB not configured.", ephemeral=True)
        return
    try:
        conn = _connect()
        cur = conn.cursor()
        # Per-strategy
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
        # Total
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
        # Top league by ROI (min 5 bets)
        cur.execute("""
            SELECT
              b.league,
              COUNT(*) AS n,
              COALESCE(SUM(ub.units),0) AS units,
              COALESCE(SUM(ub.exp_profit),0) AS ep
            FROM user_bets ub
            JOIN bets b ON b.bet_key = ub.bet_key
            GROUP BY b.league
            HAVING COUNT(*) >= 5
            ORDER BY (CASE WHEN COALESCE(SUM(ub.units),0) > 0 THEN COALESCE(SUM(ub.exp_profit),0) / COALESCE(SUM(ub.units),0) ELSE -999 END) DESC
            LIMIT 1;
        """)
        top = cur.fetchone()
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

        if top and float(top["units"] or 0) > 0:
            troi = float(top["ep"])/float(top["units"]) * 100.0
            lines.append(f"\n🏆 **Top Performing League:** {top.get('league') or 'Unknown'} — ROI **{troi:.2f}%** over **{int(top['n'])}** bets")

        await ctx.response.send_message("\n".join(lines), ephemeral=True)
    except Exception:
        log.exception("/stats failed")
        await ctx.response.send_message("Stats failed.", ephemeral=True)

# (#6) My bets (personal history)
@bot.tree.command(name="mybets", description="Show your recent bets (optionally filter by strategy) and mini-stats")
async def mybets(ctx: discord.Interaction, strategy: str | None = None, limit: int = 10):
    if not DB_OK:
        await ctx.response.send_message("DB not configured.", ephemeral=True)
        return
    limit = max(1, min(limit, 25))
    try:
        conn = _connect()
        cur = conn.cursor()
        if strategy:
            cur.execute("""
                SELECT ub.created_at, b.match, b.team, ub.strategy, ub.units, ub.odds, ub.exp_profit, b.league
                FROM user_bets ub
                LEFT JOIN bets b ON b.bet_key = ub.bet_key
                WHERE ub.user_id=%s AND ub.strategy=%s
                ORDER BY ub.created_at DESC
                LIMIT %s;
            """, (str(ctx.user.id), strategy.lower(), limit))
        else:
            cur.execute("""
                SELECT ub.created_at, b.match, b.team, ub.strategy, ub.units, ub.odds, ub.exp_profit, b.league
                FROM user_bets ub
                LEFT JOIN bets b ON b.bet_key = ub.bet_key
                WHERE ub.user_id=%s
                ORDER BY ub.created_at DESC
                LIMIT %s;
            """, (str(ctx.user.id), limit))
        rows = cur.fetchall()

        # mini-stats for this user (and strategy if provided)
        if strategy:
            cur.execute("""
                SELECT COUNT(*) AS n, COALESCE(SUM(units),0) AS units, COALESCE(SUM(exp_profit),0) AS ep
                FROM user_bets
                WHERE user_id=%s AND strategy=%s;
            """, (str(ctx.user.id), strategy.lower()))
        else:
            cur.execute("""
                SELECT COUNT(*) AS n, COALESCE(SUM(units),0) AS units, COALESCE(SUM(exp_profit),0) AS ep
                FROM user_bets
                WHERE user_id=%s;
            """, (str(ctx.user.id),))
        mini = cur.fetchone()
        cur.close()
        conn.close()

        if not rows:
            await ctx.response.send_message("No bets saved yet.", ephemeral=True)
            return

        lines = [f"**Your last {len(rows)} bets**" + (f" (strategy: {strategy})" if strategy else "")]
        for r in rows:
            lines.append(f"• {r['created_at']:%d/%m/%y %H:%M} — {r['match']} — {r['team']} — {r['strategy']} | {float(r['units']):.2f}u @ {float(r['odds']):.2f} | Exp.P&L {float(r['exp_profit']):.2f} | {r.get('league') or 'League ?'}")

        n = int(mini["n"] or 0); u = float(mini["units"] or 0); ep = float(mini["ep"] or 0)
        roi = (ep/u*100.0) if u>0 else 0.0
        lines.append(f"\n**Mini-Stats:** {n} bets | {u:.2f} units | P&L {ep:.2f} | ROI {roi:.2f}%")

        await ctx.response.send_message("\n".join(lines), ephemeral=True)
    except Exception:
        log.exception("/mybets failed")
        await ctx.response.send_message("mybets failed.", ephemeral=True)

@bot.tree.command(name="dbsource", description="Show which DB env var the bot is using")
async def dbsource(ctx: discord.Interaction):
    if not DB_OK:
        await ctx.response.send_message("DB not configured (no URL found).", ephemeral=True)
        return
    await ctx.response.send_message(
        f"Using **{DB_VAR_NAME}**\n`{_mask(DATABASE_URL)}`",
        ephemeral=True
    )

# (#8) Mode controls
@bot.tree.command(name="setmode", description="Set bot mode: live or test (admin only)")
@discord.app_commands.describe(mode="live or test")
async def setmode_cmd(ctx: discord.Interaction, mode: str):
    # Admin check: require Manage Guild
    perms = ctx.user.guild_permissions if hasattr(ctx.user, "guild_permissions") else None
    if not perms or not perms.manage_guild:
        await ctx.response.send_message("You need **Manage Server** permission to change mode.", ephemeral=True)
        return
    mode = mode.lower().strip()
    if mode not in ("live", "test"):
        await ctx.response.send_message("Mode must be `live` or `test`.", ephemeral=True)
        return
    ok = set_mode(mode)
    if ok:
        await ctx.response.send_message(f"✅ Mode set to **{mode}**.", ephemeral=True)
    else:
        await ctx.response.send_message("❌ Failed to set mode.", ephemeral=True)

@bot.tree.command(name="getmode", description="Show current bot mode")
async def getmode_cmd(ctx: discord.Interaction):
    await ctx.response.send_message(f"Current mode: **{get_mode()}**", ephemeral=True)

# ----------------------------
# Events
# ----------------------------
@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    try:
        _migrate()
        await bot.tree.sync()
        log.info("Slash commands synced. DB source: %s -> %s", DB_VAR_NAME or "NONE", _mask(DATABASE_URL) if DB_OK else "N/A")
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





