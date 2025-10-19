# bot.py
import os
import asyncio
import math
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

import aiohttp
import psycopg2
from psycopg2.extras import RealDictCursor
import discord
from discord.ext import commands, tasks
from discord import app_commands

# -----------------------------
# Environment / Config
# -----------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

CHAN_BEST  = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
CHAN_QUICK = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
CHAN_LONG  = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
CHAN_VALUE = int(os.getenv("DISCORD_CHANNEL_ID_VALUE", "0"))  # duplicate of Value Bets

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# Value classification
MIN_EDGE_VALUE = 2.0      # % edge to be considered a "Value Bet"
BEST_LOOKBACK_HOURS = 48  # find best bet within reasonable window

# Stake "units"
CONSERVATIVE_UNITS = 15.0    # fixed baseline units (was $15 → now "units")
SMART_MIN = 5.0              # never below this for Smart
AGG_BONUS_PER_EDGE = 0.5     # scale factor for edge into Aggressive units

# Allowed bookmakers (lowercase substring match)
ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
]

# Sport emoji
SPORT_EMOJI = {
    "soccer": "⚽",
    "americanfootball": "🏈",
    "baseball": "⚾",
    "basketball": "🏀",
    "icehockey": "🏒",
    "tennis": "🎾",
    "mma": "🥊",
    "boxing": "🥊",
    "golf": "⛳",
    "rugby": "🏉",
    "cricket": "🏏",
    "esports": "🎮",
}

# -----------------------------
# Discord Bot
# -----------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# -----------------------------
# DB Helpers
# -----------------------------
def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, sslmode="require")

def init_db():
    with db_conn() as conn, conn.cursor() as cur:
        # Feed bets we create as cards
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bets (
          id SERIAL PRIMARY KEY,
          bet_key TEXT UNIQUE,
          event_id TEXT,
          sport_key TEXT,
          sport_title TEXT,
          league TEXT,
          match TEXT,
          team TEXT,
          odds NUMERIC,
          edge NUMERIC,
          consensus NUMERIC,
          implied NUMERIC,
          bookmaker TEXT,
          category TEXT,
          bet_time TIMESTAMP,
          created_at TIMESTAMP DEFAULT NOW()
        );
        """)
        # User paper trades via buttons
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_bets (
          id SERIAL PRIMARY KEY,
          user_id TEXT,
          username TEXT,
          bet_key TEXT,
          event_id TEXT,
          sport TEXT,
          stake_type TEXT,      -- conservative/smart/aggressive
          stake_units NUMERIC,  -- units placed
          result TEXT,          -- win/loss/push/unknown
          settled_at TIMESTAMP,
          exp_pl NUMERIC,       -- expected P/L (units)
          created_at TIMESTAMP DEFAULT NOW()
        );
        """)
    print("✅ DB ready")


def save_feed_bet(b: Dict[str, Any]) -> None:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        INSERT INTO bets
          (bet_key, event_id, sport_key, sport_title, league, match, team, odds,
           edge, consensus, implied, bookmaker, category, bet_time)
        VALUES
          (%(bet_key)s, %(event_id)s, %(sport_key)s, %(sport_title)s, %(league)s, %(match)s, %(team)s, %(odds)s,
           %(edge)s, %(consensus)s, %(implied)s, %(bookmaker)s, %(category)s, %(bet_time)s)
        ON CONFLICT (bet_key) DO NOTHING;
        """, b)


def log_user_bet(
    user: discord.User,
    b: Dict[str, Any],
    stake_type: str,
    stake_units: float,
    exp_pl_units: float
) -> int:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        INSERT INTO user_bets
          (user_id, username, bet_key, event_id, sport, stake_type, stake_units, result, settled_at, exp_pl)
        VALUES
          (%s, %s, %s, %s, %s, %s, %s, 'unknown', NULL, %s)
        RETURNING id;
        """, (
            str(user.id), str(user), b["bet_key"], b["event_id"], b["sport_key"],
            stake_type, stake_units, exp_pl_units
        ))
        rid = cur.fetchone()["id"]
        return rid


def lookup_bet_by_key(bet_key: str) -> Optional[Dict[str, Any]]:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM bets WHERE bet_key=%s;", (bet_key,))
        row = cur.fetchone()
        return row


# -----------------------------
# Odds / Fetch Logic
# -----------------------------
def allowed_bookmaker(title: str) -> bool:
    t = (title or "").lower()
    return any(key in t for key in ALLOWED_BOOKMAKER_KEYS)

def implied_from_price(price: float) -> float:
    return 100.0 / price

def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d/%m/%y %H:%M")

def sport_emoji_and_league(sport_key: str, sport_title: str) -> Tuple[str, str]:
    # sport_key like "soccer_brazil_serie_b"
    root = sport_key.split("_")[0] if sport_key else ""
    emoji = SPORT_EMOJI.get(root, "🏟️")
    league = sport_title or "Unknown League"
    return emoji, league

async def fetch_odds() -> List[Dict[str, Any]]:
    if not ODDS_API_KEY:
        return []
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal"
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as sess:
            async with sess.get(url, params=params) as resp:
                if resp.status != 200:
                    print("❌ Odds API error:", resp.status, await resp.text())
                    return []
                return await resp.json()
    except Exception as e:
        print("❌ Odds API exception:", e)
        return []


def compute_consensus_prob(event: Dict[str, Any]) -> Dict[str, float]:
    """
    Build consensus per outcome across allowed bookmakers (simple average of inverse odds).
    """
    consensus: Dict[str, List[float]] = {}
    bms = event.get("bookmakers", [])
    for bm in bms:
        if not allowed_bookmaker(bm.get("title", "")):
            continue
        for mkt in bm.get("markets", []):
            for oc in mkt.get("outcomes", []):
                name = oc.get("name")
                price = oc.get("price")
                if not (name and price):
                    continue
                consensus.setdefault(name, []).append(1.0 / float(price))
    # Convert averages to probability %
    return {name: (sum(values) / len(values)) * 100.0 for name, values in consensus.items() if values}


def classify(event: Dict[str, Any], outcome_name: str, price: float,
             consensus_p: float, commence_dt: datetime) -> Tuple[str, bool, bool]:
    implied_p = 100.0 / price
    edge = consensus_p - implied_p
    delta = commence_dt - datetime.now(timezone.utc)
    quick = delta <= timedelta(hours=48)
    longp = timedelta(hours=48) < delta <= timedelta(days=150)
    label = "value" if edge >= MIN_EDGE_VALUE else "low"
    # category names used for channel routing/card header
    category = "quick" if quick else ("long" if longp else "other")
    return category, (label == "value"), edge >= MIN_EDGE_VALUE


def make_stakes(edge_pct: float, price: float) -> Dict[str, Dict[str, float]]:
    """
    Convert edge into three stake suggestions in UNITS.
    """
    cons = round(CONSERVATIVE_UNITS, 2)

    # Smart: grows with edge, but never below SMART_MIN
    smart = max(SMART_MIN, round(cons * (1.0 + (edge_pct / 100.0) * 0.6), 2))

    # Aggressive: more leverage on edge
    agg = round(cons * (1.0 + (edge_pct / 100.0) * (1.0 + AGG_BONUS_PER_EDGE)), 2)

    def calc(stake):
        payout = round(stake * price, 2)
        return {"stake": stake, "payout": payout}

    return {
        "conservative": calc(cons),
        "smart": calc(smart),
        "aggressive": calc(agg)
    }


def build_bet_dict(event: Dict[str, Any],
                   outcome_name: str,
                   price: float,
                   bookmaker: str,
                   consensus_p: float,
                   commence_dt: datetime) -> Dict[str, Any]:
    sport_key = event.get("sport_key", "")  # e.g., soccer_brazil_serie_b
    sport_title = event.get("sport_title", "")  # e.g., Soccer - Brazil Serie B
    emoji, league = sport_emoji_and_league(sport_key, sport_title)

    implied_p = 100.0 / price
    edge = round(consensus_p - implied_p, 2)
    match_name = f"{event.get('home_team')} vs {event.get('away_team')}"
    event_id = event.get("id") or f"{match_name}|{commence_dt.isoformat()}"

    bet_key = f"{event_id}|{bookmaker}|{outcome_name}|{price:.2f}"

    stakes = make_stakes(edge, price)

    b = {
        "bet_key": bet_key,
        "event_id": event_id,
        "sport_key": sport_key,
        "sport_title": sport_title,
        "league": league,
        "emoji": emoji,
        "match": match_name,
        "team": outcome_name,
        "odds": float(price),
        "consensus": round(consensus_p, 2),
        "implied": round(implied_p, 2),
        "edge": edge,
        "bookmaker": bookmaker,
        "commence_dt": commence_dt,
        "stakes": stakes
    }
    return b


def pick_best(bets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Choose the best among value bets by highest expected profit of the aggressive stake.
    """
    vbs = [b for b in bets if b["edge"] >= MIN_EDGE_VALUE]
    if not vbs:
        return None
    def exp_profit(b):
        p = b["consensus"] / 100.0
        stake = b["stakes"]["aggressive"]["stake"]
        payout = b["stakes"]["aggressive"]["payout"]
        return p * payout - stake
    return max(vbs, key=exp_profit)


# -----------------------------
# Discord Card + Buttons
# -----------------------------
class StakeButtons(discord.ui.View):
    def __init__(self, bet_key: str):
        super().__init__(timeout=None)
        self.bet_key = bet_key

    @discord.ui.button(label="Conservative", style=discord.ButtonStyle.success, emoji="💵", custom_id="stake_cons")
    async def btn_cons(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "conservative")

    @discord.ui.button(label="Smart", style=discord.ButtonStyle.primary, emoji="🧠", custom_id="stake_smart")
    async def btn_smart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "smart")

    @discord.ui.button(label="Aggressive", style=discord.ButtonStyle.danger, emoji="🔥", custom_id="stake_agg")
    async def btn_agg(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "aggressive")

    async def _handle(self, interaction: discord.Interaction, stake_type: str):
        b = lookup_bet_by_key(self.bet_key)
        if not b:
            await interaction.response.send_message(
                "Sorry, I couldn't find this bet yet. Please try again in a few seconds.",
                ephemeral=True
            )
            return

        # reconstruct stakes to compute expected P/L (units)
        # expected P/L uses estimated consensus probability
        odds = float(b["odds"])
        p = float(b["consensus"]) / 100.0
        edge = float(b["edge"])
        stakes = make_stakes(edge, odds)
        units = float(stakes[stake_type]["stake"])
        exp_pl_units = round(p * stakes[stake_type]["payout"] - units, 2)

        try:
            rid = log_user_bet(interaction.user, {
                "bet_key": b["bet_key"],
                "event_id": b["event_id"],
                "sport_key": b["sport_key"]
            }, stake_type, units, exp_pl_units)
            await interaction.response.send_message(
                f"✅ Saved your **{stake_type}** bet ({units} units). Entry #{rid}.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Could not save your bet. Is the database configured?\n```{e}```",
                ephemeral=True
            )


def embed_for_card(title: str, label_text: str, color: int, b: Dict[str, Any]) -> discord.Embed:
    # label_text is "Value Bet" or "Low Value"
    e = discord.Embed(title=title, color=color)
    e.add_field(name="", value=f"🟢 **{label_text}**", inline=False)

    # Sport + League
    e.add_field(
        name="",
        value=f"{b['emoji']} **{b['sport_title'] or 'Unknown sport'}** ({b['league'] or 'Unknown League'})",
        inline=False
    )
    e.add_field(name="Match", value=b["match"], inline=False)
    e.add_field(name="Pick", value=f"{b['team']} @ {b['odds']}", inline=False)
    e.add_field(name="Bookmaker", value=b["bookmaker"], inline=True)
    e.add_field(name="Consensus %", value=f"{b['consensus']}%", inline=True)
    e.add_field(name="Implied %", value=f"{b['implied']}%", inline=True)
    e.add_field(name="Edge", value=f"{b['edge']}%", inline=True)
    e.add_field(name="Time", value=fmt_dt(b["commence_dt"]), inline=True)

    cons = b["stakes"]["conservative"]
    smart = b["stakes"]["smart"]
    agg = b["stakes"]["aggressive"]
    e.add_field(
        name="💵 Conservative Stake",
        value=f"{cons['stake']} units → Payout: {cons['payout']} | Exp. Profit: {round((b['consensus']/100.0)*cons['payout'] - cons['stake'], 2)}",
        inline=False
    )
    e.add_field(
        name="🧠 Smart Stake",
        value=f"{smart['stake']} units → Payout: {smart['payout']} | Exp. Profit: {round((b['consensus']/100.0)*smart['payout'] - smart['stake'], 2)}",
        inline=False
    )
    e.add_field(
        name="🔥 Aggressive Stake",
        value=f"{agg['stake']} units → Payout: {agg['payout']} | Exp. Profit: {round((b['consensus']/100.0)*agg['payout'] - agg['stake'], 2)}",
        inline=False
    )
    return e


async def post_card(channel: discord.TextChannel, header: str, b: Dict[str, Any]) -> None:
    # never show "low value" for best bet; we always treat best as value if posted to best channel
    label_text = "Value Bet" if b["edge"] >= MIN_EDGE_VALUE else "Low Value"
    color = 0x2ECC71 if label_text == "Value Bet" else 0xE67E22
    embed = embed_for_card(header, label_text, color, b)
    view = StakeButtons(b["bet_key"])
    msg = await channel.send(embed=embed, view=view)
    # save into DB feed table
    to_save = {
        "bet_key": b["bet_key"],
        "event_id": b["event_id"],
        "sport_key": b["sport_key"],
        "sport_title": b["sport_title"],
        "league": b["league"],
        "match": b["match"],
        "team": b["team"],
        "odds": b["odds"],
        "edge": b["edge"],
        "consensus": b["consensus"],
        "implied": b["implied"],
        "bookmaker": b["bookmaker"],
        "category": header.split(" ")[0].lower(),  # "Best", "Quick", "📅"? just keep a token
        "bet_time": b["commence_dt"]
    }
    save_feed_bet(to_save)
    # if value bet, also duplicate to dedicated value-bets channel (if set)
    if CHAN_VALUE and b["edge"] >= MIN_EDGE_VALUE:
        vb_chan = bot.get_channel(CHAN_VALUE)
        if isinstance(vb_chan, discord.TextChannel):
            await vb_chan.send(embed=embed, view=StakeButtons(b["bet_key"]))


# -----------------------------
# Build bets from fetched odds
# -----------------------------
def build_bets(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for ev in raw:
        commence = ev.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        except Exception:
            continue
        if commence_dt <= now or commence_dt - now > timedelta(days=150):
            continue

        consensus_map = compute_consensus_prob(ev)
        if not consensus_map:
            continue

        for bm in ev.get("bookmakers", []):
            title = bm.get("title", "Unknown")
            if not allowed_bookmaker(title):
                continue
            for mkt in bm.get("markets", []):
                for oc in mkt.get("outcomes", []):
                    name = oc.get("name")
                    price = oc.get("price")
                    if not (name and price):
                        continue
                    name = str(name)
                    price = float(price)
                    # consensus prob for this outcome
                    if name not in consensus_map:
                        continue
                    consensus_p = float(consensus_map[name])

                    # classify + construct dict
                    category, is_value, _ = classify(ev, name, price, consensus_p, commence_dt)
                    b = build_bet_dict(ev, name, price, title, consensus_p, commence_dt)
                    b["category"] = category
                    results.append(b)
    return results


# -----------------------------
# Scheduler / Posting
# -----------------------------
@tasks.loop(minutes=2)
async def bet_loop():
    raw = await fetch_odds()
    if not raw:
        return
    bets = build_bets(raw)
    if not bets:
        return

    # BEST
    best = pick_best(bets)
    if best and CHAN_BEST:
        ch = bot.get_channel(CHAN_BEST)
        if isinstance(ch, discord.TextChannel):
            await post_card(ch, "⭐ Best Bet", best)

    # QUICK
    if CHAN_QUICK:
        qch = bot.get_channel(CHAN_QUICK)
        if isinstance(qch, discord.TextChannel):
            for b in bets:
                if b["category"] == "quick" and b["edge"] >= MIN_EDGE_VALUE:
                    await post_card(qch, "⏱ Quick Return Bet", b)

    # LONG
    if CHAN_LONG:
        lch = bot.get_channel(CHAN_LONG)
        if isinstance(lch, discord.TextChannel):
            for b in bets:
                if b["category"] == "long" and b["edge"] >= MIN_EDGE_VALUE:
                    await post_card(lch, "📅 Longer Play Bet", b)


# -----------------------------
# Slash Commands
# -----------------------------
@bot.tree.command(name="ping", description="Check if the bot is alive.")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong! I'm alive.", ephemeral=True)

@bot.tree.command(name="fetchbets", description="Force the bot to fetch odds and post cards now.")
async def fetchbets_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    raw = await fetch_odds()
    bets = build_bets(raw)
    posted = 0
    if bets:
        # Post only a small sample to avoid spam on manual trigger
        best = pick_best(bets)
        if best and CHAN_BEST:
            ch = bot.get_channel(CHAN_BEST)
            if isinstance(ch, discord.TextChannel):
                await post_card(ch, "⭐ Best Bet", best)
                posted += 1
        for b in bets:
            if posted >= 5:
                break
            if b["category"] == "quick" and b["edge"] >= MIN_EDGE_VALUE and CHAN_QUICK:
                qch = bot.get_channel(CHAN_QUICK)
                if isinstance(qch, discord.TextChannel):
                    await post_card(qch, "⏱ Quick Return Bet", b)
                    posted += 1
    await interaction.followup.send(f"Done. Posted {posted} card(s).", ephemeral=True)

@bot.tree.command(name="stats", description="Your personal paper-trade stats.")
async def stats_cmd(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        SELECT
            COUNT(*) as cnt,
            COALESCE(SUM(stake_units), 0) as units,
            COALESCE(SUM(exp_pl), 0) as exp_pl
        FROM user_bets
        WHERE user_id = %s
        """, (uid,))
        row = cur.fetchone()
    cnt = int(row["cnt"]) if row else 0
    units = float(row["units"] or 0)
    exp_pl = float(row["exp_pl"] or 0)
    roi = round((exp_pl / units) * 100, 2) if units > 0 else 0.0

    await interaction.response.send_message(
        f"**Your Stats**\n"
        f"- Total bets: **{cnt}**\n"
        f"- Total units placed: **{units:.2f}**\n"
        f"- Expected P/L: **{exp_pl:.2f}** units\n"
        f"- ROI (expected): **{roi:.2f}%**",
        ephemeral=True
    )

@bot.tree.command(name="roi", description="System-wide ROI and totals from all posted bets (expected).")
async def roi_cmd(interaction: discord.Interaction):
    with db_conn() as conn, conn.cursor() as cur:
        # Estimate expected P/L of the feed if a Conservative stake was hypothetically used for every value bet.
        cur.execute("""
        SELECT
          COUNT(*) as cnt,
          COALESCE(SUM( (consensus/100.0) * ( %(cons)s * odds ) - %(cons)s ), 0) AS exp_pl,
          COALESCE(SUM( %(cons)s ), 0) as units
        FROM bets
        WHERE edge >= %(min_edge)s
        """, {"cons": CONSERVATIVE_UNITS, "min_edge": MIN_EDGE_VALUE})
        row = cur.fetchone()
    cnt = int(row["cnt"]) if row else 0
    exp_pl = float(row["exp_pl"] or 0)
    units = float(row["units"] or 0)
    roi = round((exp_pl / units) * 100, 2) if units > 0 else 0.0

    await interaction.response.send_message(
        f"**System (paper) ROI**\n"
        f"- Value bets counted: **{cnt}**\n"
        f"- Units (hypothetical @ {CONSERVATIVE_UNITS:.0f} per bet): **{units:.2f}**\n"
        f"- Expected P/L: **{exp_pl:.2f}** units\n"
        f"- ROI (expected): **{roi:.2f}%**",
        ephemeral=True
    )


# -----------------------------
# Bot Lifecycle
# -----------------------------
@bot.event
async def on_ready():
    init_db()
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Slash sync err:", e)
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    if not bet_loop.is_running():
        bet_loop.start()
    # small startup message
    if CHAN_BEST:
        ch = bot.get_channel(CHAN_BEST)
        if isinstance(ch, discord.TextChannel):
            await ch.send("🎲 Betting bot is online and rolling bets!")

# -----------------------------
# Run
# -----------------------------
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN")
bot.run(TOKEN)



