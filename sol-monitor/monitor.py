#!/usr/bin/env python3
"""
SOL链二段交易监控系统 - 后端服务
=================================
功能：
1. DexScreener 筛选新币 (Age≥2h, MCAP≥200k, Liq≥50k, Vol≥300k)
2. Birdeye 安全检查 + Rugcheck LP锁定检查
3. 实时数据轮询 (Birdeye API, 30s间隔)
4. X热度监控 (15min间隔, 含KOL加权)
5. 自动退出逻辑 + Discord Webhook通知
6. REST API + 内置Dashboard静态页面
"""

import asyncio
import aiohttp
from aiohttp import web
import json
import time
import os
import sys
import signal
import logging
import logging.handlers
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ==================== 路径 ====================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
FRONTEND_DIR = BASE_DIR / "frontend" / "static"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ==================== 日志配置 ====================
def setup_logging():
    log_file = LOG_DIR / "monitor.log"
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8'
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

setup_logging()
logger = logging.getLogger("sol-monitor")


# ==================== 配置 ====================
class Config:
    """从环境变量加载, 支持 .env 文件"""

    def __init__(self):
        self._load_dotenv()

        # API Keys
        self.X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")
        self.BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")
        self.HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")

        # Discord
        self.DISCORD_WEBHOOK_URL = os.getenv(
            "DISCORD_WEBHOOK_URL",
            "https://discord.com/api/webhooks/1474357109666873457/"
            "zGiWxWk8QLW8VzXiK33I8qyznxhYw7koyRtV2VhtOAD6sndw0igdabIvCzorNID62PEo"
        )

        # DexScreener 筛选
        self.MIN_AGE_HOURS = int(os.getenv("MIN_AGE_HOURS", "2"))
        self.MIN_MCAP = int(os.getenv("MIN_MCAP", "200000"))
        self.MIN_LIQUIDITY = int(os.getenv("MIN_LIQUIDITY", "50000"))
        self.MIN_VOLUME = int(os.getenv("MIN_VOLUME", "300000"))

        # 退出条件
        self.EXIT_FDV_THRESHOLD = int(os.getenv("EXIT_FDV_THRESHOLD", "50000"))
        self.EXIT_AGE_HOURS = int(os.getenv("EXIT_AGE_HOURS", "24"))
        self.EXIT_LOW_HEAT_THRESHOLD = float(os.getenv("EXIT_LOW_HEAT_THRESHOLD", "50"))
        self.EXIT_LOW_HEAT_CONSECUTIVE = int(os.getenv("EXIT_LOW_HEAT_CONSECUTIVE", "2"))

        # 通知
        self.HEAT_SPIKE_PERCENT = float(os.getenv("HEAT_SPIKE_PERCENT", "50"))
        self.HEAT_SPIKE_MIN_SCORE = float(os.getenv("HEAT_SPIKE_MIN_SCORE", "200"))

        # 轮询间隔 (秒)
        self.BIRDEYE_POLL_INTERVAL = int(os.getenv("BIRDEYE_POLL_INTERVAL", "30"))
        self.X_HEAT_INTERVAL = int(os.getenv("X_HEAT_INTERVAL", "900"))
        self.DEXSCREENER_SCAN_INTERVAL = int(os.getenv("DEXSCREENER_SCAN_INTERVAL", "300"))

        # X API
        self.KOL_FOLLOWERS_THRESHOLD = int(os.getenv("KOL_FOLLOWERS_THRESHOLD", "10000"))
        self.KOL_WEIGHT = float(os.getenv("KOL_WEIGHT", "2.5"))
        self.MAX_HEAT_HISTORY = int(os.getenv("MAX_HEAT_HISTORY", "48"))

        # 服务
        self.API_HOST = os.getenv("API_HOST", "0.0.0.0")
        self.API_PORT = int(os.getenv("API_PORT", "8888"))

        # 持久化
        self.STATE_FILE = str(DATA_DIR / "monitor_state.json")

    @staticmethod
    def _load_dotenv():
        env_file = BASE_DIR / ".env"
        if not env_file.exists():
            return
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


# ==================== 数据模型 ====================
@dataclass
class HeatRecord:
    mentions: int = 0
    avg_eng: float = 0.0
    heat_score: float = 0.0
    kol_ratio: float = 0.0
    kol_count: int = 0
    total_engagement: int = 0
    ca_mention_ratio: float = 0.0
    timestamp: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class MonitoredCoin:
    contract_address: str
    symbol: str
    name: str = ""
    pair_address: str = ""

    # 实时数据
    fdv: float = 0.0
    liquidity: float = 0.0
    volume_24h: float = 0.0
    holders: int = 0
    price: float = 0.0
    price_change_5m: float = 0.0
    price_change_1h: float = 0.0
    price_change_6h: float = 0.0
    price_change_24h: float = 0.0

    # 时间
    created_at: str = ""
    added_at: str = ""
    age_hours: float = 0.0

    # 安全
    security_passed: bool = False
    lp_locked: bool = False
    rugcheck_status: str = ""
    birdeye_security: dict = field(default_factory=dict)

    # FDV历史 (与heat_history对齐, 每次X热度更新时记录)
    fdv_history: List[dict] = field(default_factory=list)

    # X热度
    heat_history: List[dict] = field(default_factory=list)
    current_heat_score: float = 0.0
    heat_trend: str = "flat"
    heat_delta_percent: float = 0.0
    x_since_id: Optional[str] = None
    low_heat_count: int = 0

    # 状态
    status: str = "active"
    exit_reason: str = ""

    @property
    def lp_fdv_ratio(self) -> float:
        return round(self.liquidity / self.fdv, 4) if self.fdv > 0 else 0.0

    @property
    def gmgn_url(self) -> str:
        return f"https://gmgn.ai/sol/token/{self.contract_address}"

    @property
    def birdeye_url(self) -> str:
        return f"https://birdeye.so/token/{self.contract_address}?chain=solana"

    def to_dict(self):
        d = asdict(self)
        d["lp_fdv_ratio"] = self.lp_fdv_ratio
        d["gmgn_url"] = self.gmgn_url
        d["birdeye_url"] = self.birdeye_url
        return d


# ==================== 核心监控 ====================
class SolMonitor:
    def __init__(self):
        self.config = Config()
        self.coins: Dict[str, MonitoredCoin] = {}
        self.session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._last_dex_scan = 0.0
        self._last_x_update = 0.0
        self._last_birdeye_poll = 0.0
        self._notification_cooldown: Dict[str, float] = {}
        self._start_time = 0.0
        self._load_state()

    # ---------- 持久化 ----------
    def _load_state(self):
        try:
            path = self.config.STATE_FILE
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for ca, cd in data.get("coins", {}).items():
                    hh = cd.pop("heat_history", [])
                    fh = cd.pop("fdv_history", [])
                    for k in ("lp_fdv_ratio", "gmgn_url", "birdeye_url"):
                        cd.pop(k, None)
                    coin = MonitoredCoin(**cd)
                    coin.heat_history = hh
                    coin.fdv_history = fh
                    if coin.status == "active":
                        self.coins[ca] = coin
                logger.info("已加载 %d 个活跃监控币", len(self.coins))
        except Exception as e:
            logger.error("加载状态失败: %s", e)

    def _save_state(self):
        try:
            data = {
                "coins": {ca: c.to_dict() for ca, c in self.coins.items()},
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            tmp = self.config.STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.config.STATE_FILE)
        except Exception as e:
            logger.error("保存状态失败: %s", e)

    # ---------- DexScreener ----------
    async def scan_dexscreener(self):
        for url in (
            "https://api.dexscreener.com/token-boosts/latest/v1",
            "https://api.dexscreener.com/token-boosts/top/v1",
        ):
            try:
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        continue
                    tokens = await resp.json()
                    sol_tokens = [t for t in (tokens if isinstance(tokens, list) else []) if t.get("chainId") == "solana"]
                    for token in sol_tokens[:20]:
                        addr = token.get("tokenAddress", "")
                        if addr and addr not in self.coins:
                            await self._check_dex_pair(addr)
                            await asyncio.sleep(0.35)
            except Exception as e:
                logger.error("DexScreener扫描异常 (%s): %s", url.split("/")[-1], e)
        logger.info("DexScreener扫描完成, 监控: %d 个币", len(self.coins))

    async def _check_dex_pair(self, token_address: str):
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                pairs = data.get("pairs") or []
                if not pairs:
                    return
                pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))

                fdv = float(pair.get("fdv", 0) or 0)
                liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                vol = float(pair.get("volume", {}).get("h24", 0) or 0)
                pair_created = pair.get("pairCreatedAt", 0)

                age_hours = 0.0
                created_dt = None
                if pair_created:
                    created_dt = datetime.fromtimestamp(pair_created / 1000, tz=timezone.utc)
                    age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600

                if not (age_hours >= self.config.MIN_AGE_HOURS
                        and fdv >= self.config.MIN_MCAP
                        and liq >= self.config.MIN_LIQUIDITY
                        and vol >= self.config.MIN_VOLUME):
                    return

                bt = pair.get("baseToken", {})
                symbol = bt.get("symbol", "???")
                pc = pair.get("priceChange", {})

                coin = MonitoredCoin(
                    contract_address=token_address, symbol=symbol,
                    name=bt.get("name", symbol),
                    pair_address=pair.get("pairAddress", ""),
                    fdv=fdv, liquidity=liq, volume_24h=vol,
                    price=float(pair.get("priceUsd", 0) or 0),
                    created_at=created_dt.isoformat() if created_dt else "",
                    added_at=datetime.now(timezone.utc).isoformat(),
                    age_hours=age_hours,
                    price_change_5m=float(pc.get("m5", 0) or 0),
                    price_change_1h=float(pc.get("h1", 0) or 0),
                    price_change_6h=float(pc.get("h6", 0) or 0),
                    price_change_24h=float(pc.get("h24", 0) or 0),
                )
                logger.info("候选币: %s  FDV=$%s Liq=$%s Vol=$%s Age=%.1fh",
                            symbol, f"{fdv:,.0f}", f"{liq:,.0f}", f"{vol:,.0f}", age_hours)
                await self._security_check(coin)
        except Exception as e:
            logger.error("检查token %s 失败: %s", token_address[:12], e)

    # ---------- 安全检查 ----------
    async def _security_check(self, coin: MonitoredCoin):
        be_ok = await self._birdeye_security(coin)
        rc_ok = await self._rugcheck(coin)
        if be_ok and rc_ok:
            coin.security_passed = True
            coin.status = "active"
            self.coins[coin.contract_address] = coin
            logger.info("✅ %s 通过安全检查", coin.symbol)
            self._save_state()
        else:
            logger.warning("❌ %s 未通过安全检查 (Birdeye:%s Rugcheck:%s)", coin.symbol, be_ok, rc_ok)

    async def _birdeye_security(self, coin: MonitoredCoin) -> bool:
        if not self.config.BIRDEYE_API_KEY:
            return True
        url = "https://public-api.birdeye.so/defi/token_security"
        headers = {"X-API-KEY": self.config.BIRDEYE_API_KEY, "x-chain": "solana"}
        try:
            async with self.session.get(url, headers=headers,
                                        params={"address": coin.contract_address},
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return True
                sec = (await resp.json()).get("data", {})
                coin.birdeye_security = sec
                if sec.get("freezeAuthority"):
                    logger.warning("%s: 存在冻结权限", coin.symbol)
                    return False
                return True
        except Exception as e:
            logger.error("Birdeye安全检查异常: %s", e)
            return True

    async def _rugcheck(self, coin: MonitoredCoin) -> bool:
        url = f"https://api.rugcheck.xyz/v1/tokens/{coin.contract_address}/report/summary"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return True
                data = await resp.json()
                risks = data.get("risks", [])
                high = [r for r in risks if r.get("level") == "danger"]
                if high:
                    names = [r.get("name", "") for r in high]
                    logger.warning("%s Rugcheck高风险: %s", coin.symbol, names)
                    if any("lp" in n.lower() and "unlock" in n.lower() for n in names):
                        coin.lp_locked = False
                        return False
                coin.lp_locked = True
                coin.rugcheck_status = "Good" if not high else "Warning"
                return True
        except Exception as e:
            logger.error("Rugcheck异常: %s", e)
            return True

    # ---------- Birdeye 轮询 ----------
    async def poll_birdeye_data(self):
        if not self.config.BIRDEYE_API_KEY:
            return
        headers = {"X-API-KEY": self.config.BIRDEYE_API_KEY, "x-chain": "solana"}
        for ca, coin in list(self.coins.items()):
            if coin.status != "active":
                continue
            try:
                async with self.session.get(
                    "https://public-api.birdeye.so/defi/token_overview",
                    headers=headers, params={"address": ca},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        continue
                    td = (await resp.json()).get("data", {})
                    coin.price = float(td.get("price", 0) or 0)
                    coin.fdv = float(td.get("fdv", 0) or td.get("mc", 0) or 0)
                    coin.liquidity = float(td.get("liquidity", 0) or 0)
                    coin.volume_24h = float(td.get("v24hUSD", 0) or 0)
                    coin.holders = int(td.get("holder", 0) or 0)
                    coin.price_change_5m = float(td.get("priceChange5mPercent", 0) or 0)
                    coin.price_change_1h = float(td.get("priceChange1hPercent", 0) or 0)
                    coin.price_change_6h = float(td.get("priceChange6hPercent", 0) or 0)
                    coin.price_change_24h = float(td.get("priceChange24hPercent", 0) or 0)
                    if coin.created_at:
                        created = datetime.fromisoformat(coin.created_at.replace("Z", "+00:00"))
                        coin.age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.error("Birdeye轮询 %s 失败: %s", coin.symbol, e)
        await self._check_exit_conditions()
        self._save_state()

    # ---------- X 热度 ----------
    async def update_x_heat(self):
        if not self.config.X_BEARER_TOKEN:
            logger.debug("无X_BEARER_TOKEN, 生成模拟热度")
            for coin in self.coins.values():
                if coin.status == "active":
                    self._mock_heat(coin)
            self._save_state()
            return

        logger.info("=== X热度更新 (%d 币) ===", len(self.coins))
        for ca, coin in list(self.coins.items()):
            if coin.status != "active":
                continue
            tweets, new_sid = await self._fetch_tweets(coin)
            if new_sid:
                coin.x_since_id = new_sid
            heat = self._calc_heat(tweets, coin)
            coin.heat_history.append(heat.to_dict())
            if len(coin.heat_history) > self.config.MAX_HEAT_HISTORY:
                coin.heat_history.pop(0)

            # 同步记录FDV快照 (与heat_history时间对齐, 方便前端叠加对比)
            coin.fdv_history.append({
                "fdv": coin.fdv,
                "liquidity": coin.liquidity,
                "volume_24h": coin.volume_24h,
                "holders": coin.holders,
                "price": coin.price,
                "timestamp": heat.timestamp,
            })
            if len(coin.fdv_history) > self.config.MAX_HEAT_HISTORY:
                coin.fdv_history.pop(0)

            old = coin.current_heat_score
            coin.current_heat_score = heat.heat_score
            coin.heat_delta_percent = ((heat.heat_score - old) / old * 100) if old > 0 else (100 if heat.heat_score > 0 else 0)

            if coin.heat_delta_percent > self.config.HEAT_SPIKE_PERCENT:
                coin.heat_trend = "spike"
            elif coin.heat_delta_percent > 10:
                coin.heat_trend = "up"
            elif coin.heat_delta_percent < -20:
                coin.heat_trend = "down"
            else:
                coin.heat_trend = "flat"

            coin.low_heat_count = (coin.low_heat_count + 1) if heat.heat_score < self.config.EXIT_LOW_HEAT_THRESHOLD else 0

            logger.info("%s: 热度=%.0f Δ=%+.1f%% 提及=%d KOL=%d 趋势=%s",
                        coin.symbol, heat.heat_score, coin.heat_delta_percent,
                        heat.mentions, heat.kol_count, coin.heat_trend)

            if coin.heat_trend == "spike" and heat.heat_score >= self.config.HEAT_SPIKE_MIN_SCORE:
                await self._discord_alert(coin, heat)
            await asyncio.sleep(1)
        self._save_state()

    async def _fetch_tweets(self, coin: MonitoredCoin):
        headers = {"Authorization": f"Bearer {self.config.X_BEARER_TOKEN}"}
        ca_short = coin.contract_address[:20]
        query = f'(${coin.symbol} OR "{ca_short}") lang:en -is:reply -is:retweet'
        params = {
            "query": query, "max_results": 100,
            "tweet.fields": "public_metrics,created_at,author_id",
            "expansions": "author_id", "user.fields": "public_metrics",
        }
        if coin.x_since_id:
            params["since_id"] = coin.x_since_id
        try:
            async with self.session.get(
                "https://api.x.com/2/tweets/search/recent",
                headers=headers, params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 429:
                    ra = int(resp.headers.get("Retry-After", 60))
                    logger.warning("X API限流, 等 %ds", min(ra, 120))
                    await asyncio.sleep(min(ra, 120))
                    return [], coin.x_since_id
                if resp.status != 200:
                    return [], coin.x_since_id
                data = await resp.json()
                tweets = data.get("data", [])
                meta = data.get("meta", {})
                umap = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
                for t in tweets:
                    aid = t.get("author_id")
                    if aid and aid in umap:
                        t["author"] = umap[aid]
                return tweets, meta.get("newest_id")
        except Exception as e:
            logger.error("X API异常 (%s): %s", coin.symbol, e)
            return [], coin.x_since_id

    def _calc_heat(self, tweets, coin) -> HeatRecord:
        if not tweets:
            return HeatRecord(timestamp=datetime.now(timezone.utc).isoformat())
        n = len(tweets)
        eng = kol = ca_n = 0
        for t in tweets:
            m = t.get("public_metrics", {})
            e = m.get("like_count", 0) + m.get("retweet_count", 0) + m.get("reply_count", 0)
            eng += e
            a = t.get("author", {})
            fc = a.get("public_metrics", {}).get("followers_count", 0)
            if fc >= self.config.KOL_FOLLOWERS_THRESHOLD and e > 20:
                kol += 1
            if coin.contract_address[:16] in t.get("text", ""):
                ca_n += 1
        ae = eng / n
        kr = kol / n
        cr = ca_n / n
        hs = n * ae * (1 + kr * self.config.KOL_WEIGHT) * (1 + cr * 0.3)
        return HeatRecord(
            mentions=n, avg_eng=round(ae, 2), heat_score=round(hs, 1),
            kol_ratio=round(kr, 3), kol_count=kol, total_engagement=eng,
            ca_mention_ratio=round(cr, 3), timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def _mock_heat(self, coin: MonitoredCoin):
        import random
        base = random.randint(100, 2000)
        if coin.heat_history:
            last = coin.heat_history[-1].get("heat_score", base)
            base = max(10, last * (1 + random.uniform(-0.3, 0.4)))
        h = HeatRecord(
            mentions=random.randint(5, 80), avg_eng=round(random.uniform(2, 50), 2),
            heat_score=round(base, 1), kol_ratio=round(random.uniform(0, 0.3), 3),
            kol_count=random.randint(0, 5), total_engagement=random.randint(50, 500),
            ca_mention_ratio=round(random.uniform(0.1, 0.5), 3),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        old = coin.current_heat_score
        coin.heat_history.append(h.to_dict())
        if len(coin.heat_history) > self.config.MAX_HEAT_HISTORY:
            coin.heat_history.pop(0)
        # 同步模拟FDV历史
        mock_fdv = coin.fdv if coin.fdv > 0 else random.randint(200000, 5000000)
        if coin.fdv_history:
            mock_fdv = coin.fdv_history[-1].get("fdv", mock_fdv) * (1 + random.uniform(-0.08, 0.12))
        coin.fdv_history.append({
            "fdv": round(mock_fdv, 0),
            "liquidity": coin.liquidity,
            "volume_24h": coin.volume_24h,
            "holders": coin.holders,
            "price": coin.price,
            "timestamp": h.timestamp,
        })
        if len(coin.fdv_history) > self.config.MAX_HEAT_HISTORY:
            coin.fdv_history.pop(0)
        coin.current_heat_score = h.heat_score
        coin.heat_delta_percent = ((h.heat_score - old) / old * 100) if old > 0 else 0
        if coin.heat_delta_percent > self.config.HEAT_SPIKE_PERCENT:
            coin.heat_trend = "spike"
        elif coin.heat_delta_percent > 10:
            coin.heat_trend = "up"
        elif coin.heat_delta_percent < -20:
            coin.heat_trend = "down"
        else:
            coin.heat_trend = "flat"
        coin.low_heat_count = (coin.low_heat_count + 1) if h.heat_score < self.config.EXIT_LOW_HEAT_THRESHOLD else 0

    # ---------- 退出检查 ----------
    async def _check_exit_conditions(self):
        for ca, coin in list(self.coins.items()):
            if coin.status != "active":
                continue
            reason = ""
            if 0 < coin.fdv < self.config.EXIT_FDV_THRESHOLD:
                reason = f"FDV跌破${self.config.EXIT_FDV_THRESHOLD:,} (${coin.fdv:,.0f})"
            elif coin.age_hours > self.config.EXIT_AGE_HOURS:
                reason = f"年龄>{self.config.EXIT_AGE_HOURS}h ({coin.age_hours:.1f}h)"
            elif coin.low_heat_count >= self.config.EXIT_LOW_HEAT_CONSECUTIVE:
                reason = f"连续{coin.low_heat_count}次低热度"
            if reason:
                coin.status = "exited"
                coin.exit_reason = reason
                logger.info("🔴 %s 退出: %s", coin.symbol, reason)
                await self._discord_msg(
                    f"🔴 **{coin.symbol}** 退出监控\n原因: {reason}\n"
                    f"FDV: ${coin.fdv:,.0f} | 热度: {coin.current_heat_score:.0f}"
                )

    # ---------- Discord ----------
    async def _discord_alert(self, coin, heat):
        now = time.time()
        if now - self._notification_cooldown.get(coin.contract_address, 0) < 300:
            return
        self._notification_cooldown[coin.contract_address] = now
        embed = {
            "title": f"🔥 {coin.symbol} X热度暴涨 +{coin.heat_delta_percent:.0f}%",
            "color": 0xFF4500,
            "fields": [
                {"name": "热度", "value": f"{heat.heat_score:.0f}", "inline": True},
                {"name": "提及", "value": str(heat.mentions), "inline": True},
                {"name": "KOL", "value": str(heat.kol_count), "inline": True},
                {"name": "FDV", "value": f"${coin.fdv:,.0f}", "inline": True},
                {"name": "LP", "value": f"${coin.liquidity:,.0f}", "inline": True},
                {"name": "Vol", "value": f"${coin.volume_24h:,.0f}", "inline": True},
                {"name": "Holders", "value": f"{coin.holders:,}", "inline": True},
                {"name": "Age", "value": f"{coin.age_hours:.1f}h", "inline": True},
                {"name": "CA", "value": f"[{coin.contract_address[:12]}...]({coin.gmgn_url})", "inline": False},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._discord_msg(embed=embed)

    async def _discord_msg(self, content=None, embed=None):
        if not self.config.DISCORD_WEBHOOK_URL:
            return
        payload = {}
        if content:
            payload["content"] = content
        if embed:
            payload["embeds"] = [embed]
        try:
            async with self.session.post(self.config.DISCORD_WEBHOOK_URL, json=payload,
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status not in (200, 204):
                    logger.error("Discord通知失败: %d", resp.status)
        except Exception as e:
            logger.error("Discord异常: %s", e)

    # ---------- 手动操作 ----------
    def remove_coin(self, ca):
        if ca in self.coins:
            self.coins[ca].status = "manual_removed"
            self.coins[ca].exit_reason = "手动删除"
            logger.info("手动移除: %s", self.coins[ca].symbol)
            self._save_state()
            return True
        return False

    def add_coin_manual(self, ca, symbol):
        if ca in self.coins:
            return False
        self.coins[ca] = MonitoredCoin(
            contract_address=ca, symbol=symbol,
            added_at=datetime.now(timezone.utc).isoformat(),
            security_passed=True, status="active",
        )
        self._save_state()
        logger.info("手动添加: %s (%s...)", symbol, ca[:12])
        return True

    # ---------- HTTP API + 静态文件 ----------
    def _build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._cors_mw])
        r = app.router
        r.add_get("/api/coins", self._h_coins)
        r.add_get("/api/coins/{ca}", self._h_coin)
        r.add_delete("/api/coins/{ca}", self._h_del)
        r.add_post("/api/coins", self._h_add)
        r.add_get("/api/status", self._h_status)
        r.add_get("/api/config", self._h_cfg_get)
        r.add_post("/api/config", self._h_cfg_set)
        # 静态文件 + SPA fallback
        if FRONTEND_DIR.exists():
            r.add_static("/static/", FRONTEND_DIR)
        r.add_get("/", self._h_index)
        r.add_get("/{path:.*}", self._h_index)
        return app

    @web.middleware
    async def _cors_mw(self, req, handler):
        if req.method == "OPTIONS":
            resp = web.Response()
        else:
            try:
                resp = await handler(req)
            except web.HTTPException as ex:
                resp = ex
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    async def _h_index(self, req):
        idx = FRONTEND_DIR / "index.html"
        if idx.exists():
            return web.FileResponse(idx)
        return web.Response(text="Dashboard not built. Visit /api/status", content_type="text/plain")

    async def _h_coins(self, req):
        show_all = req.query.get("all", "false").lower() == "true"
        coins = [c.to_dict() for c in self.coins.values() if show_all or c.status == "active"]
        return web.json_response({"coins": coins, "count": len(coins)})

    async def _h_coin(self, req):
        ca = req.match_info["ca"]
        c = self.coins.get(ca)
        return web.json_response(c.to_dict()) if c else web.json_response({"error": "Not found"}, status=404)

    async def _h_del(self, req):
        ca = req.match_info["ca"]
        if self.remove_coin(ca):
            return web.json_response({"success": True})
        return web.json_response({"error": "Not found"}, status=404)

    async def _h_add(self, req):
        try:
            body = await req.json()
            ca = body.get("contract_address", "")
            sym = body.get("symbol", "UNKNOWN")
            if not ca:
                return web.json_response({"error": "need contract_address"}, status=400)
            if self.add_coin_manual(ca, sym):
                asyncio.create_task(self._check_dex_pair(ca))
                return web.json_response({"success": True})
            return web.json_response({"error": "already exists"}, status=409)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def _h_status(self, req):
        active = sum(1 for c in self.coins.values() if c.status == "active")
        return web.json_response({
            "status": "running" if self._running else "stopped",
            "active_coins": active, "total_coins": len(self.coins),
            "uptime_seconds": round(time.time() - self._start_time, 1),
            "last_dex_scan": self._last_dex_scan,
            "last_birdeye_poll": self._last_birdeye_poll,
            "last_x_update": self._last_x_update,
        })

    async def _h_cfg_get(self, req):
        return web.json_response({
            k: getattr(self.config, k) for k in (
                "EXIT_FDV_THRESHOLD", "EXIT_AGE_HOURS", "EXIT_LOW_HEAT_THRESHOLD",
                "EXIT_LOW_HEAT_CONSECUTIVE", "HEAT_SPIKE_PERCENT", "HEAT_SPIKE_MIN_SCORE",
                "MIN_MCAP", "MIN_LIQUIDITY", "MIN_VOLUME",
                "BIRDEYE_POLL_INTERVAL", "X_HEAT_INTERVAL", "DEXSCREENER_SCAN_INTERVAL",
            )
        })

    async def _h_cfg_set(self, req):
        try:
            body = await req.json()
            for k, v in body.items():
                ku = k.upper()
                if hasattr(self.config, ku):
                    setattr(self.config, ku, type(getattr(self.config, ku))(v))
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    # ---------- 主循环 ----------
    async def start(self):
        self._running = True
        self._start_time = time.time()
        self.session = aiohttp.ClientSession()

        logger.info("=" * 60)
        logger.info("SOL链二段交易监控系统启动")
        logger.info("  监控: %d 币  端口: %d", len(self.coins), self.config.API_PORT)
        logger.info("  DexScreener: %ds  Birdeye: %ds  X热度: %ds",
                     self.config.DEXSCREENER_SCAN_INTERVAL,
                     self.config.BIRDEYE_POLL_INTERVAL,
                     self.config.X_HEAT_INTERVAL)
        logger.info("=" * 60)

        # 启动 HTTP
        app = self._build_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.config.API_HOST, self.config.API_PORT)
        await site.start()
        logger.info("HTTP服务: http://%s:%d", self.config.API_HOST, self.config.API_PORT)

        # graceful shutdown
        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        try:
            while not stop_event.is_set():
                now = time.time()
                if now - self._last_dex_scan >= self.config.DEXSCREENER_SCAN_INTERVAL:
                    try:
                        await self.scan_dexscreener()
                    except Exception as e:
                        logger.error("DexScreener异常: %s", e)
                    self._last_dex_scan = now

                if now - self._last_birdeye_poll >= self.config.BIRDEYE_POLL_INTERVAL:
                    try:
                        await self.poll_birdeye_data()
                    except Exception as e:
                        logger.error("Birdeye异常: %s", e)
                    self._last_birdeye_poll = now

                if now - self._last_x_update >= self.config.X_HEAT_INTERVAL:
                    try:
                        await self.update_x_heat()
                    except Exception as e:
                        logger.error("X热度异常: %s", e)
                    self._last_x_update = now

                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._running = False
            self._save_state()
            if self.session:
                await self.session.close()
            await runner.cleanup()
            logger.info("监控系统已停止")


if __name__ == "__main__":
    monitor = SolMonitor()
    asyncio.run(monitor.start())
