# bot.py
import os
import asyncio
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

import discord
from discord import Interaction, Embed, Color
from discord.ext import commands, tasks

import psycopg2
import psycopg2.extras
import requests

# =========================
# ENV / CONFIG
# =========================
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()

# Channels
BEST_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))

# Daily Picks (12pm Perth)
DAILY_PICKS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_DAILY", "1454041180336554116"))

# Matched Bets channel
MATCHED_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_MATCHED", "1454055086631157884"))

# TheOddsAPI key
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip() or os.getenv("THEODDS_API_KEY", "").strip()

# Postgres connection (prefer DATABASE_URL, else DATABASE_PUBLIC_URL)
DATABASE_URL = (
    os.getenv("DATABASE_URL", "").strip()
    or os.getenv("DATABASE_PUBLIC_URL", "").strip()
)

# =========================
# CONSTANTS / RULES
# =========================
PERTH_TZ = ZoneInfo("Australia/Perth")

BANKROLL_UNITS = 1000.0
CONSERVATIVE_PCT = 0.015

# Remove low value bets entirely:
MIN_EDGE_PCT = float(os.getenv("MIN_EDGE_PCT", "2.0"))

# Event horizon
MAX_EVENT_DAYS = int(os.getenv("MAX_EVENT_DAYS", "150"))

# Matched-betting preview knobs (no exchange feed)
MATCHED_ENABLED = os.getenv("MATCHED_ENABLED", "1").strip() != "0"
MATCHED_INTERVAL_MIN = int(os.getenv("MATCHED_INTERVAL_MINUTES", "30"))
MATCHED_MAX_POSTS_PER_RUN = int(os.getenv("MATCHED_MAX_POSTS_PER_RUN", "8"))
EST_LAY_OFFSET = float(os.getenv("EST_LAY_OFFSET", "0.03"))  # lay approx = back_odds - offset
EST_LAY_RANGE = float(os.getenv("EST_LAY_RANGE", "0.06"))   # +/- range displayed
EXCHANGE_COMMISSION = float(os.getenv("EXCHANGE_COMMISSION", "0.02"))  # 2%

# Default promo stake used for preview examples
DEFAULT_PROMO_STAKE = float(os.getenv("MATCHED_DEFAULT_STAKE", "50"))

# Bookmakers allowed
BOOKMAKER_WHITELIST = {
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
}

# Your bookmaker channels (duplicate bets into correct channel)
BOOKMAKER_CHANNELS = {
    "tabtouch": 1452828790567993415,
    "sportsbet": 1452828858658324596,
    "bet365": 1452828976060956753,
    "neds": 1452829020306800681,
    "ladbrokes": 1452829097440055306,
    "pointsbet": 1452829191945981963,
    "tab": 1452829245490335967,
    "betfair": 1452829323747659849,
}

# Sport emoji + league mapper
SPORT_EMOJI = {
    "soccer": "⚽",
    "basketball": "🏀",
    "tennis": "🎾",
    "americanfootball": "🏈",
    "icehockey": "🏒",
    "baseball": "⚾",
    "aussierules": "🏉",
    "mma": "🥊",
    "boxing": "🥊",
    "cricket": "🏏",
    "formula1": "🏎️",
    "rugbyleague": "🏉",
    "rugbynunion": "🏉",
}

# =========================
# IN-MEMORY INDEX FOR BUTTONS
# =========================
POSTED_BETS: dict[str, dict] = {}  # bet_key -> bet dict


# =========================
# DB HELPERS
# =========================
def get_db_conn():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def ensure_schema():
    """Create tables if missing and ensure expected columns exist."""
    if not DATABASE_URL:
        return
    conn = get_db_conn()
    conn.autocommit = True
    cur = conn.cursor()

    # bets table (audit feed)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bets (
          id SERIAL PRIMARY KEY,
          event_id TEXT,
          bet_key TEXT UNIQUE,
          match TEXT,
          bookmaker TEXT,
          team TEXT,
          odds NUMERIC,
          edge NUMERIC,
          bet_time TIMESTAMPTZ,
          category TEXT,
          sport TEXT,
          league TEXT,
          created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    # user_bets table (paper-trade settlement)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_bets (
          id SERIAL PRIMARY KEY,
          user_id BIGINT,
          username TEXT,
          bet_key TEXT,
          event_id TEXT,
          sport TEXT,
          league TEXT,
          stake_type TEXT,
          stake_units NUMERIC,
          odds NUMERIC,
          placed_at TIMESTAMPTZ DEFAULT NOW(),
          result TEXT,
          settled_at TIMESTAMPTZ,
          pnl_units NUMERIC
        );
    """)

    # results cache table (so we don't hammer API)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS event_results (
          id SERIAL PRIMARY KEY,
          event_id TEXT UNIQUE,
          sport_key TEXT,
          home_team TEXT,
          away_team TEXT,
          commence_time TIMESTAMPTZ,
          completed BOOLEAN DEFAULT FALSE,
          winner TEXT,
          updated_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    # add missing columns defensively
    for (table, col, typ) in [
        ("user_bets", "stake_type", "TEXT"),
        ("user_bets", "pnl_units", "NUMERIC"),
        ("user_bets", "result", "TEXT"),
        ("user_bets", "settled_at", "TIMESTAMPTZ"),
        ("user_bets", "league", "TEXT"),
        ("event_results", "winner", "TEXT"),
        ("event_results", "completed", "BOOLEAN"),
    ]:
        cur.execute(f"""
          DO $$
          BEGIN
            IF NOT EXISTS (
              SELECT 1 FROM information_schema.columns
               WHERE table_name='{table}'
                 AND column_name='{col}'
            ) THEN
              ALTER TABLE {table} ADD COLUMN {col} {typ};
            END IF;
          END$$;
        """)

    cur.close()
    conn.close()


def save_bet_row(bet: dict):
    if not DATABASE_URL:
        return
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO bets (event_id, bet_key, match, bookmaker, team, odds, edge, bet_time,
                        category, sport, league)
      VALUES (%(event_id)s, %(bet_key)s, %(match)s, %(bookmaker)s, %(team)s, %(odds)s,
              %(edge)s, %(bet_time)s, %(category)s, %(sport)s, %(league)s)
      ON CONFLICT (bet_key) DO NOTHING;
    """, bet)
    conn.commit()
    cur.close()
    conn.close()


def save_user_bet(user: discord.User | discord.Member, bet: dict, stake_type: str, stake_units: float) -> int:
    if not DATABASE_URL:
        raise RuntimeError("DB not configured")
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO user_bets
        (user_id, username, bet_key, event_id, sport, league, stake_type, stake_units, odds)
      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
      RETURNING id;
    """, (
        int(user.id), str(user.name), bet["bet_key"], bet.get("event_id"),
        bet.get("sport"), bet.get("league"), stake_type, stake_units, bet.get("odds")
    ))
    row_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()
    return row_id


def db_agg_total() -> dict:
    if not DATABASE_URL:
        return {"bets": 0, "staked": 0.0, "pnl": 0.0, "wins": 0, "settled": 0}
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
      SELECT
        COUNT(*)::INT as bets,
        COALESCE(SUM(stake_units),0) as staked,
        COALESCE(SUM(pnl_units),0) as pnl,
        COALESCE(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END),0)::INT as wins,
        COALESCE(SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END),0)::INT as settled
      FROM user_bets;
    """)
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


def db_agg_user(user_id: int) -> dict:
    if not DATABASE_URL:
        return {"bets": 0, "staked": 0.0, "pnl": 0.0, "wins": 0, "settled": 0}
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
      SELECT
        COUNT(*)::INT as bets,
        COALESCE(SUM(stake_units),0) as staked,
        COALESCE(SUM(pnl_units),0) as pnl,
        COALESCE(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END),0)::INT as wins,
        COALESCE(SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END),0)::INT as settled
      FROM user_bets
      WHERE user_id = %s;
    """, (user_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


# =========================
# ODDS FETCH (TheOddsAPI)
# =========================
def allowed_book(title: str) -> bool:
    return any(k in (title or "").lower() for k in BOOKMAKER_WHITELIST)


def theodds_fetch_upcoming():
    """Fetch upcoming odds (keep small-ish to respect credits)."""
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,uk,us",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal"
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return []
        return r.json()
    except Exception:
        return []


def theodds_fetch_scores(days_from: int = 3):
    """
    Fetch scores for completed events.
    TheOddsAPI scores endpoint.
    """
    url = "https://api.the-odds-api.com/v4/sports/upcoming/scores/"
    params = {
        "apiKey": ODDS_API_KEY,
        "daysFrom": str(days_from)
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return []
        return r.json()
    except Exception:
        return []


def compute_bets_from_payload(payload):
    """
    Compute value bets:
    - consensus implied probability vs offered implied probability
    - only keep edge >= MIN_EDGE_PCT
    """
    now = datetime.now(timezone.utc)
    results = []

    for ev in payload:
        home = ev.get("home_team"); away = ev.get("away_team")
        if not home or not away:
            continue

        match_name = f"{home} vs {away}"
        commence = ev.get("commence_time")
        try:
            dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        except Exception:
            continue

        if dt <= now or dt > now + timedelta(days=MAX_EVENT_DAYS):
            continue

        sport_key = (ev.get("sport_key") or "").lower()
        league = ev.get("sport_title") or ev.get("sport_title_long") or "Unknown League"
        emoji = SPORT_EMOJI.get(sport_key, "🎲")

        # Build consensus probability from allowed books
        cs_map = defaultdict(list)
        for bk in ev.get("bookmakers", []):
            if not allowed_book(bk.get("title", "")):
                continue
            for m in bk.get("markets", []):
                for oc in m.get("outcomes", []):
                    nm = oc.get("name"); pr = oc.get("price")
                    if nm and pr:
                        try:
                            cs_map[f"{m['key']}:{nm}"].append(1 / float(pr))
                        except Exception:
                            continue

        if not cs_map:
            continue

        tot_ps = [p for arr in cs_map.values() for p in arr]
        global_c = sum(tot_ps) / max(1, len(tot_ps))

        for bk in ev.get("bookmakers", []):
            if not allowed_book(bk.get("title", "")):
                continue
            for m in bk.get("markets", []):
                for oc in m.get("outcomes", []):
                    nm = oc.get("name"); pr = oc.get("price")
                    if not nm or not pr:
                        continue
                    try:
                        pr_f = float(pr)
                        implied = 1 / pr_f
                    except Exception:
                        continue

                    keyo = f"{m['key']}:{nm}"
                    consensus = (sum(cs_map[keyo]) / len(cs_map[keyo])) if keyo in cs_map else global_c
                    edge = (consensus - implied) * 100.0

                    # remove low-value bets
                    if edge < MIN_EDGE_PCT:
                        continue

                    conservative_units = round(BANKROLL_UNITS * CONSERVATIVE_PCT, 2)
                    smart_units = round(conservative_units * max(1.0, (consensus * 100) / 50.0), 2)
                    aggressive_units = round(conservative_units * (1 + (edge / 10.0)), 2)

                    bet_key = f"{match_name}|{nm}|{bk['title']}|{dt.isoformat()}|{m.get('key','')}"
                    results.append({
                        "event_id": ev.get("id") or bet_key,
                        "bet_key": bet_key,
                        "match": match_name,
                        "bookmaker": bk.get("title", "Unknown"),
                        "bookmaker_key": (bk.get("key") or bk.get("title", "")).lower(),
                        "team": nm,
                        "odds": pr_f,
                        "edge": round(edge, 2),
                        "consensus": round(consensus * 100, 2),
                        "bet_time": dt,
                        "category": "value",
                        "sport": sport_key or "unknown",
                        "league": league,
                        "emoji": emoji,
                        "conservative_units": conservative_units,
                        "smart_units": smart_units,
                        "aggressive_units": aggressive_units,
                        "market": m.get("key", "unknown")
                    })

    return results


# =========================
# EMBEDS + BUTTONS
# =========================
def bet_embed(bet: dict, title: str, color: int) -> Embed:
    sport_line = f"{bet['emoji']} {bet['sport'].title()} ({bet.get('league') or 'Unknown League'})"
    implied_pct = round((1 / bet["odds"]) * 100, 2)

    desc = (
        f"🟢 **Value Bet** (edge ≥ {MIN_EDGE_PCT:.1f}%)\n\n"
        f"**{sport_line}**\n\n"
        f"**Match:** {bet['match']}\n"
        f"**Market:** {bet.get('market','h2h')}\n"
        f"**Pick:** {bet['team']} @ {bet['odds']}\n"
        f"**Bookmaker:** {bet['bookmaker']}\n"
        f"**Consensus %:** {bet['consensus']}%\n"
        f"**Implied %:** {implied_pct}%\n"
        f"**Edge:** {bet['edge']}%\n"
        f"**Time (Perth):** {bet['bet_time'].astimezone(PERTH_TZ).strftime('%d/%m/%y %H:%M')}\n\n"
        f"💵 **Conservative Stake:** {bet['conservative_units']} units\n"
        f"🧠 **Smart Stake:** {bet['smart_units']} units\n"
        f"🔥 **Aggressive Stake:** {bet['aggressive_units']} units\n"
    )
    e = Embed(title=title, description=desc, color=color)
    e.set_footer(text="Click a stake button below to record your paper-trade.")
    return e


class StakeButtons(discord.ui.View):
    def __init__(self, bet_key: str, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.bet_key = bet_key

    @discord.ui.button(label="Conservative", emoji="💵", style=discord.ButtonStyle.secondary)
    async def cons_btn(self, interaction: Interaction, button: discord.ui.Button):
        await self._save(interaction, "conservative")

    @discord.ui.button(label="Smart", emoji="🧠", style=discord.ButtonStyle.primary)
    async def smart_btn(self, interaction: Interaction, button: discord.ui.Button):
        await self._save(interaction, "smart")

    @discord.ui.button(label="Aggressive", emoji="🔥", style=discord.ButtonStyle.danger)
    async def aggr_btn(self, interaction: Interaction, button: discord.ui.Button):
        await self._save(interaction, "aggressive")

    async def _save(self, interaction: Interaction, stake_type: str):
        bet = POSTED_BETS.get(self.bet_key)
        if not bet:
            await interaction.response.send_message(
                "Sorry, I couldn't find this bet yet. Try again in a few seconds.",
                ephemeral=True
            )
            return

        units = {
            "conservative": bet["conservative_units"],
            "smart": bet["smart_units"],
            "aggressive": bet["aggressive_units"]
        }[stake_type]

        try:
            row_id = save_user_bet(interaction.user, bet, stake_type, units)
        except Exception:
            await interaction.response.send_message(
                "❌ Could not save your bet. Is the database configured?",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ Saved your **{stake_type}** bet ({units} units). Entry #{row_id}.",
            ephemeral=True
        )


def matched_bet_embed(bet: dict) -> Embed:
    """
    Matched bet preview WITHOUT exchange odds:
    - show suggested back stake
    - show estimated lay odds range
    - warn to confirm lay odds manually
    """
    back_odds = float(bet["odds"])
    est_lay = max(1.01, round(back_odds - EST_LAY_OFFSET, 2))
    lay_low = max(1.01, round(est_lay - EST_LAY_RANGE, 2))
    lay_high = max(1.01, round(est_lay + EST_LAY_RANGE, 2))

    # Simple “hedge” calc example (approx)
    # Lay stake ≈ (BackStake * BackOdds) / (LayOdds - CommissionAdj)
    # Commission applied to winnings on exchange; approximate by increasing lay stake slightly.
    back_stake = DEFAULT_PROMO_STAKE
    denom = max(1.01, est_lay - (EXCHANGE_COMMISSION * (est_lay - 1)))
    lay_stake = round((back_stake * back_odds) / denom, 2)

    sport_line = f"{bet['emoji']} {bet['sport'].title()} ({bet.get('league') or 'Unknown League'})"
    desc = (
        f"🧩 **Matched Bet Opportunity (PREVIEW)**\n"
        f"⚠️ *This is generated without live exchange odds — confirm lay price before placing.*\n\n"
        f"**{sport_line}**\n\n"
        f"**Match:** {bet['match']}\n"
        f"**Bookmaker Back:** {bet['bookmaker']} → **{bet['team']} @ {back_odds}**\n"
        f"**Suggested Back Stake:** {back_stake:.2f} units (example promo stake)\n\n"
        f"**Estimated Exchange Lay Odds:** ~{est_lay}  (range {lay_low}–{lay_high})\n"
        f"**Estimated Lay Stake:** {lay_stake} units  (commission {EXCHANGE_COMMISSION*100:.0f}% assumed)\n\n"
        f"✅ If you can lay close to the estimate, this is typically near-risk-free.\n"
        f"🧠 Always re-check odds on the exchange right before placing."
    )
    e = Embed(title="🎯 Matched Bet (Preview)", description=desc, color=0x9B59B6)
    return e


# =========================
# DISCORD BOT
# =========================
intents = discord.Intents.default()
intents.message_content = True


class ValueBetsBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.synced = False

    async def setup_hook(self):
        ensure_schema()


bot = ValueBetsBot()


@bot.event
async def on_ready():
    if not bot.synced:
        await bot.tree.sync()
        bot.synced = True
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")


# =========================
# SLASH COMMANDS
# =========================
@bot.tree.command(name="ping", description="Check bot latency.")
async def ping_cmd(interaction: Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency*1000)}ms", ephemeral=True)


@bot.tree.command(name="fetchbets", description="Manually fetch a preview of incoming value bets.")
async def fetchbets_cmd(interaction: Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    payload = theodds_fetch_upcoming()
    if not payload:
        await interaction.followup.send("No odds available (or API limit/unauthorized).", ephemeral=True)
        return

    bets = compute_bets_from_payload(payload)
    if not bets:
        await interaction.followup.send(f"No value bets found right now (edge ≥ {MIN_EDGE_PCT:.1f}%).", ephemeral=True)
        return

    bets.sort(key=lambda x: (x["edge"], x["consensus"]), reverse=True)
    lines = []
    for b in bets[:5]:
        lines.append(f"**{b['match']}** · {b['team']} @ {b['odds']} ({b['bookmaker']}) | Edge: {b['edge']}%")
    await interaction.followup.send("🟢 Value Bets Preview:\n" + "\n".join(lines), ephemeral=True)


@bot.tree.command(name="roi", description="System-wide ROI (all recorded user paper trades).")
async def roi_cmd(interaction: Interaction):
    agg = db_agg_total()
    staked = float(agg["staked"])
    pnl = float(agg["pnl"])
    roi = (pnl / staked * 100.0) if staked > 0 else 0.0
    wr = (agg["wins"] / agg["settled"] * 100.0) if agg["settled"] > 0 else 0.0
    msg = (
        f"📊 **System ROI**\n"
        f"- Bets: {agg['bets']}\n"
        f"- Settled: {agg['settled']}\n"
        f"- Wins: {agg['wins']}\n"
        f"- Staked: {staked:.2f} units\n"
        f"- P/L: {pnl:.2f} units\n"
        f"- ROI: {roi:.2f}%\n"
        f"- Win rate (settled): {wr:.2f}%"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="stats", description="Your personal paper-trading stats.")
async def stats_cmd(interaction: Interaction):
    agg = db_agg_user(interaction.user.id)
    staked = float(agg["staked"])
    pnl = float(agg["pnl"])
    roi = (pnl / staked * 100.0) if staked > 0 else 0.0
    wr = (agg["wins"] / agg["settled"] * 100.0) if agg["settled"] > 0 else 0.0
    msg = (
        f"🧾 **Your Stats**\n"
        f"- Bets: {agg['bets']}\n"
        f"- Settled: {agg['settled']}\n"
        f"- Wins: {agg['wins']}\n"
        f"- Staked: {staked:.2f} units\n"
        f"- P/L: {pnl:.2f} units\n"
        f"- ROI: {roi:.2f}%\n"
        f"- Win rate (settled): {wr:.2f}%"
    )
    await interaction.response.send_message(msg, ephemeral=True)


# =========================
# POSTING HELPERS
# =========================
def normalize_bookmaker_key(book_title: str) -> str:
    """
    Convert bookmaker title into a key that matches BOOKMAKER_CHANNELS
    """
    t = (book_title or "").lower().strip()
    # common normalizations
    if "tabtouch" in t:
        return "tabtouch"
    if t == "tab" or "tab " in t or "tab-" in t:
        return "tab"
    if "sportsbet" in t:
        return "sportsbet"
    if "bet365" in t:
        return "bet365"
    if "neds" in t:
        return "neds"
    if "ladbrokes" in t:
        return "ladbrokes"
    if "pointsbet" in t:
        return "pointsbet"
    if "betfair" in t:
        return "betfair"
    return t.replace(" ", "")


async def send_to_channel(channel_id: int, embed: Embed, view: discord.ui.View | None = None):
    if not channel_id:
        return
    ch = bot.get_channel(channel_id)
    if ch:
        await ch.send(embed=embed, view=view)


async def post_value_bet(bet: dict):
    """
    Posts a value bet to its bookmaker channel.
    """
    # index for buttons
    POSTED_BETS[bet["bet_key"]] = bet
    try:
        save_bet_row(bet)
    except Exception:
        pass

    view = StakeButtons(bet["bet_key"])
    embed = bet_embed(bet, "🟢 Value Bet", Color.green().value)

    bk_key = normalize_bookmaker_key(bet.get("bookmaker", ""))
    channel_id = BOOKMAKER_CHANNELS.get(bk_key)
    if channel_id:
        await send_to_channel(channel_id, embed, view=view)


async def post_best_bet(best_bet: dict):
    """
    Posts best bet to BEST_BETS_CHANNEL
    AND duplicates it into the correct bookmaker channel.
    """
    POSTED_BETS[best_bet["bet_key"]] = best_bet
    try:
        save_bet_row(best_bet)
    except Exception:
        pass

    view = StakeButtons(best_bet["bet_key"])
    embed_best = bet_embed(best_bet, "⭐ Best Bet", Color.gold().value)

    # 1) Best bets channel
    await send_to_channel(BEST_BETS_CHANNEL, embed_best, view=view)

    # 2) Duplicate into bookmaker channel
    bk_key = normalize_bookmaker_key(best_bet.get("bookmaker", ""))
    channel_id = BOOKMAKER_CHANNELS.get(bk_key)
    if channel_id:
        await send_to_channel(channel_id, embed_best, view=view)


async def post_daily_picks(bets: list[dict]):
    """
    Posts top 10 highest-edge value bets into DAILY_PICKS_CHANNEL.
    """
    if not DAILY_PICKS_CHANNEL:
        return
    if not bets:
        return

    bets.sort(key=lambda x: (x["edge"], x["consensus"]), reverse=True)
    top10 = bets[:10]

    lines = []
    for i, b in enumerate(top10, start=1):
        perth_time = b["bet_time"].astimezone(PERTH_TZ).strftime("%d/%m %H:%M")
        lines.append(
            f"**#{i}** {b['emoji']} **{b['match']}**\n"
            f"• {b['team']} @ {b['odds']} (**{b['bookmaker']}**) | Edge: **{b['edge']}%** | {perth_time}\n"
        )

    e = Embed(
        title="📌 Daily Picks (Top 10 Value Bets)",
        description="\n".join(lines),
        color=0x1ABC9C
    )
    e.set_footer(text="Top 10 highest-edge value bets at publish time. Confirm odds before placing.")
    await send_to_channel(DAILY_PICKS_CHANNEL, e)


async def post_matched_opportunities(bets: list[dict]):
    """
    Posts matched-bet preview opportunities (without exchange odds).
    Uses a subset of strong value bets, typically higher odds/liquidity markets.
    """
    if not MATCHED_ENABLED or not MATCHED_BETS_CHANNEL:
        return
    if not bets:
        return

    # pick candidates: prefer H2H-ish and mid odds (more common for promos)
    candidates = []
    for b in bets:
        o = float(b["odds"])
        if o < 1.4 or o > 6.0:
            continue
        # prefer h2h where possible
        if (b.get("market") or "").lower() not in {"h2h", "head_to_head", "moneyline", "h2h_lay"}:
            # still allow but de-prioritize
            pass
        candidates.append(b)

    if not candidates:
        return

    candidates.sort(key=lambda x: (x["edge"], x["consensus"]), reverse=True)
    to_post = candidates[:MATCHED_MAX_POSTS_PER_RUN]

    for b in to_post:
        e = matched_bet_embed(b)
        await send_to_channel(MATCHED_BETS_CHANNEL, e, view=None)
        await asyncio.sleep(0.8)


# =========================
# AUTO SETTLEMENT (scores -> DB)
# =========================
def _calc_pnl(stake_units: float, odds: float, result: str) -> float:
    if result == "win":
        return round(stake_units * (odds - 1.0), 4)
    if result == "loss":
        return round(-stake_units, 4)
    if result == "void":
        return 0.0
    return 0.0


def _upsert_event_result(event_id: str, sport_key: str, home: str, away: str, commence_time: datetime,
                         completed: bool, winner: str | None):
    if not DATABASE_URL:
        return
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO event_results (event_id, sport_key, home_team, away_team, commence_time, completed, winner, updated_at)
      VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
      ON CONFLICT (event_id)
      DO UPDATE SET
        sport_key = EXCLUDED.sport_key,
        home_team = EXCLUDED.home_team,
        away_team = EXCLUDED.away_team,
        commence_time = EXCLUDED.commence_time,
        completed = EXCLUDED.completed,
        winner = EXCLUDED.winner,
        updated_at = NOW();
    """, (event_id, sport_key, home, away, commence_time, completed, winner))
    conn.commit()
    cur.close()
    conn.close()


def _settle_user_bets_for_event(event_id: str, winner_name: str | None, completed: bool):
    """
    Settle all user_bets rows for this event_id if result is NULL.
    If winner_name is None, mark void when completed.
    """
    if not DATABASE_URL or not completed:
        return

    conn = get_db_conn()
    cur = conn.cursor()

    # get unsettled bets
    cur.execute("""
      SELECT id, bet_key, stake_units, odds
      FROM user_bets
      WHERE event_id = %s AND result IS NULL;
    """, (event_id,))
    rows = cur.fetchall()
    if not rows:
        cur.close(); conn.close()
        return

    for r in rows:
        bet_key = r["bet_key"]
        stake = float(r["stake_units"] or 0.0)
        odds = float(r["odds"] or 0.0)

        # Determine pick from bet_key (format: match|team|book|time|market)
        # This is consistent with how we build bet_key.
        parts = bet_key.split("|")
        pick = parts[1] if len(parts) > 1 else ""

        if not winner_name:
            result = "void"
        else:
            # If winner matches pick: win else loss
            result = "win" if pick.strip().lower() == winner_name.strip().lower() else "loss"

        pnl = _calc_pnl(stake, odds, result)

        cur.execute("""
          UPDATE user_bets
          SET result=%s, pnl_units=%s, settled_at=NOW()
          WHERE id=%s;
        """, (result, pnl, r["id"]))

    conn.commit()
    cur.close()
    conn.close()


def process_scores_and_settle():
    """
    Pull recent scores from TheOddsAPI and settle any matching user_bets.
    """
    if not ODDS_API_KEY or not DATABASE_URL:
        return

    scores = theodds_fetch_scores(days_from=3)
    if not scores:
        return

    for ev in scores:
        # Expected fields (TheOddsAPI): id, sport_key, commence_time, completed, home_team, away_team, scores, last_update
        event_id = ev.get("id")
        if not event_id:
            continue
        sport_key = (ev.get("sport_key") or "").lower()
        home = ev.get("home_team") or ""
        away = ev.get("away_team") or ""
        commence = ev.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat(commence.replace("Z", "+00:00")) if commence else datetime.now(timezone.utc)
        except Exception:
            commence_dt = datetime.now(timezone.utc)

        completed = bool(ev.get("completed", False))

        winner = None
        # If completed, derive winner from scores if present
        # TheOddsAPI often provides scores = [{"name": "Team", "score": "x"}, ...]
        if completed:
            sc = ev.get("scores")
            if isinstance(sc, list) and len(sc) >= 2:
                try:
                    a = sc[0]
                    b = sc[1]
                    sa = float(a.get("score", 0))
                    sb = float(b.get("score", 0))
                    if sa > sb:
                        winner = a.get("name")
                    elif sb > sa:
                        winner = b.get("name")
                    else:
                        winner = None  # draw/void for our simple model
                except Exception:
                    winner = None

        _upsert_event_result(event_id, sport_key, home, away, commence_dt, completed, winner)
        _settle_user_bets_for_event(event_id, winner, completed)


# =========================
# BACKGROUND TASKS
# =========================
@tasks.loop(minutes=5)
async def bet_loop():
    """
    Main loop:
    - fetch value bets (edge >= MIN_EDGE_PCT)
    - post ALL value bets to bookmaker channels
    - post single best bet to BEST_BETS_CHANNEL + duplicate to bookmaker channel
    - post matched opportunities (preview) periodically
    """
    if not ODDS_API_KEY:
        return

    payload = theodds_fetch_upcoming()
    if not payload:
        return

    bets = compute_bets_from_payload(payload)
    if not bets:
        return

    # Sort by strength
    bets.sort(key=lambda x: (x["edge"], x["consensus"]), reverse=True)
    best = bets[0]

    # 1) post best bet (best bets channel + duplicate)
    try:
        await post_best_bet(best)
    except Exception:
        pass

    # 2) post remaining value bets into their bookmaker channels
    # (skip best to reduce spam duplicates)
    for b in bets[1:]:
        try:
            await post_value_bet(b)
            await asyncio.sleep(0.4)
        except Exception:
            continue

    # 3) matched bet opportunities (preview) — throttle by interval
    # We'll post these in a separate loop to avoid spamming every 5 minutes.
    # So nothing here.


@tasks.loop(minutes=MATCHED_INTERVAL_MIN)
async def matched_loop():
    """
    Periodic matched bet opportunity posts (preview mode).
    """
    if not MATCHED_ENABLED or not ODDS_API_KEY:
        return
    payload = theodds_fetch_upcoming()
    if not payload:
        return
    bets = compute_bets_from_payload(payload)
    if not bets:
        return
    try:
        await post_matched_opportunities(bets)
    except Exception:
        pass


@tasks.loop(minutes=30)
async def settlement_loop():
    """
    Auto-settle bets in DB using TheOddsAPI scores endpoint.
    This is what makes /roi and /stats show profit/wins.
    """
    try:
        process_scores_and_settle()
    except Exception:
        pass


@tasks.loop(minutes=1)
async def daily_picks_scheduler():
    """
    Scheduler that runs every minute, posts Daily Picks at 12:00 Perth time.
    """
    now_perth = datetime.now(PERTH_TZ)
    # exactly 12:00 perth time
    if now_perth.hour == 12 and now_perth.minute == 0:
        if not ODDS_API_KEY:
            return
        payload = theodds_fetch_upcoming()
        if not payload:
            return
        bets = compute_bets_from_payload(payload)
        if not bets:
            return
        try:
            await post_daily_picks(bets)
        except Exception:
            pass
        # prevent double-posting within same minute window
        await asyncio.sleep(65)


@bot.event
async def on_connect():
    # Start loops if not running
    if not bet_loop.is_running():
        bet_loop.start()
    if MATCHED_ENABLED and not matched_loop.is_running():
        matched_loop.start()
    if DATABASE_URL and not settlement_loop.is_running():
        settlement_loop.start()
    if not daily_picks_scheduler.is_running():
        daily_picks_scheduler.start()


# =========================
# RUN
# =========================
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN")

bot.run(TOKEN)
