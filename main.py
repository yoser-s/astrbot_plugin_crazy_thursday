# -*- coding: utf-8 -*-
"""疯狂星期四：每周定时向指定群发送一条文案。

文案源可插拔：
  - vme  ：https://vme.im/api/random（免鉴权 JSON，国内可直连）
  - rsshub：Telegram 频道经 RSSHub 中转（t.me 国内不可直连，多实例自动降级）
"""

import asyncio
import hashlib
import html
import json
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request

try:
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.platform.message_type import MessageType
except ImportError:  # 兼容旧版本
    MessageSession = None
    MessageType = None

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except ImportError:
    get_astrbot_plugin_data_path = None


PLUGIN_NAME = "astrbot_plugin_crazy_thursday"

# 容器/主机时区固定按东八区判断，不依赖系统 TZ
TZ8 = timezone(timedelta(hours=8))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

SUPPORTED_PROVIDERS = ("vme", "rsshub")

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# AI 审核：让当前使用的 LLM 结合发送日期判断文案是否适合群发
REVIEW_SYSTEM_PROMPT = (
    "你是群聊文案审核员。用户会给你一条“疯狂星期四”文案，"
    "以及今天的日期和计划发送的日期，请判断这条文案是否适合在计划日期发送到普通 QQ 群。\n"
    "出现下列任一情况才判定不通过：\n"
    "1) 涉及政治、领导人、政党、政权、体制、历史政治事件、分裂主张等敏感内容，"
    "包括以影射、隐喻、谐音、代称、戏谑方式出现的政治话题"
    "（例如摄政、朝堂、宫廷政治、政变、政权更替等）；\n"
    "2) 内容与某个特定节日、纪念日或社会事件强绑定，而计划发送日期和该节日对不上"
    "（例如节日已过去很久还发节日文案，或节日还远未到来）；"
    "注意：只是出现节日字样不算问题，只要结合日期看是应景、得体的就应当通过；\n"
    "3) 含色情、暴力、赌博、诈骗、违法引导、辱骂歧视等违规内容。\n"
    "只输出一行 JSON，不要任何解释或代码块：\n"
    '{"pass": true 或 false, "reason": "不通过时用不超过 20 字说明原因，通过时留空"}'
)
REVIEW_MAX_CANDIDATES = 8  # 一次发送最多审核这么多条，避免刷爆 LLM
REVIEW_WORKERS = 4  # 手动审核时的并发数

# vme.im 的文案实际托管在 GitHub issue（random 接口的 id 即 issue node_id）
GITHUB_ISSUES_API = "https://api.github.com/repos/%s/issues"


def _iso_ts(value) -> float:
    """解析 2026-10-08T02:37:14Z 这类时间戳，失败返回 0"""
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (TypeError, ValueError):
        return 0.0


# 抓取失败且无缓存时的内置兜底文案
BUILTIN_TEXTS = [
    "今天是星期四，我本来不想打扰你。但我的手在抖，我的心在痛，我的胃在叫——它想吃肯德基。V我50，让我安静地吃完这顿疯狂星期四。",
    "科学家研究发现，每周四下午三点，人体内的炸鸡需求量会达到峰值。这不是馋，这是生理刚需。V我50，救我于危难。",
    "我不是来要钱的，我是来给你一个投资机会。50元，回报是未来某天你需要人帮忙时，我会想起来今天。",
    "本人已确诊为肯德基依赖症，医生说要终身服药，药名叫吮指原味鸡。家境贫寒，V我50，让我续命一天。",
    "据不可靠消息，本周四肯德基门口的队伍已经排到了隔壁街。而我，还在为50块钱发愁。",
    "刚刚算命先生说我这周有个大劫，需要用一份全家桶来化解。我信了，V我50。",
    "我知道你很想拒绝我，但你先别拒绝。因为今天是星期四，而星期四的规矩你懂的。",
    "群里安静得有点过分了。是不是大家都在等我先开口？那我说了：V我50。",
    "本人自愿放弃本周所有面子，换取一顿疯狂星期四。50块钱，买我一个星期的尊严。",
    "体检报告出来了，医生说我缺一种营养——只有肯德基能提供的那种。V我50，谢谢。",
    "我已经三天没吃饭了。不是因为没钱，是因为只有周四的炸鸡才算饭。V我50。",
    "今天的风很大，吹得我有点冷。但我知道，只要一份热腾腾的疯狂星期四，我就能活过来。V我50。",
]

MAX_TEXT_LEN = 2000  # 过长的文案不参与抽取，避免刷屏/超限


def _data_dir() -> Path:
    """运行时数据目录（放 data/plugin_data 下，避免插件更新被覆盖）"""
    if get_astrbot_plugin_data_path:
        base = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
    else:
        # main.py 位于 data/plugins/<name>/main.py → parents[2] 即 data
        base = Path(os.path.abspath(__file__)).parents[2] / PLUGIN_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def _clean_html(raw: str) -> str:
    """把 RSS 里转义过的 HTML 正文还原成纯文本"""
    s = html.unescape(raw or "")
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p\s*>", "\n", s)
    s = re.sub(r"(?is)<img[^>]*>", "", s)
    s = re.sub(r"<[^>]+>", "", s)  # 剥掉 <span class="emoji"> 之类的外壳，保留文字
    s = html.unescape(s)
    s = re.sub(r"[ \t\u3000]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _strip_markdown_images(text: str) -> str:
    """去掉 ![alt](url) 形式的图片，留下真正的文字"""
    return re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text or "").strip()


def parse_rss(xml_text: str) -> list:
    """解析 RSS，返回 [{guid, text, link}]"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("[%s] RSS 解析失败：%r" % (PLUGIN_NAME, exc))
        return []

    posts = []
    for item in root.findall("./channel/item"):
        guid = (item.findtext("guid") or item.findtext("link") or "").strip()
        body = _clean_html(item.findtext("description"))
        if not guid or not body:
            continue
        if len(body) < 8 or body.strip() in ("🖼", "🖼️"):
            continue
        posts.append(
            {
                "guid": guid,
                "text": body,
                "link": (item.findtext("link") or "").strip(),
            }
        )
    return posts


@register(PLUGIN_NAME, "yosers", "每周四定时发送疯狂星期四文案", "v1.0.0")
class CrazyThursdayPlugin(Star):
    CONFIG_GROUPS = ("source", "schedule", "send", "filter")

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self._data = _data_dir()
        self._posts_file = self._data / "posts.json"
        self._state_file = self._data / "state.json"
        self._review_file = self._data / "review.json"
        self._closed = False
        self._task = None

        self._register_web_apis()
        try:
            self._task = asyncio.create_task(self._loop())
        except RuntimeError as exc:
            logger.error("[%s] 定时任务启动失败：%r" % (PLUGIN_NAME, exc))

    async def terminate(self):
        self._closed = True
        if self._task:
            self._task.cancel()
        logger.info("[%s] 已停止" % PLUGIN_NAME)

    # ------------------------------------------------------------------ #
    # 配置读取（_conf_schema.json 是分组结构，落盘后是嵌套的）
    # ------------------------------------------------------------------ #
    def _c(self, key, default):
        value = self.config.get(key)
        if value is None:
            for group in self.CONFIG_GROUPS:
                sub = self.config.get(group)
                if isinstance(sub, dict) and sub.get(key) is not None:
                    value = sub.get(key)
                    break
        return default if value is None else value

    def _set_conf(self, group: str, key: str, value):
        sub = self.config.get(group)
        if not isinstance(sub, dict):
            sub = {}
            self.config[group] = sub
        sub[key] = value

    @staticmethod
    def _as_lines(raw) -> list:
        if isinstance(raw, str):
            return [x.strip() for x in raw.splitlines() if x.strip()]
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return []

    def _provider_order(self) -> list:
        raw = self._c("provider_order", list(SUPPORTED_PROVIDERS))
        chunks = raw.splitlines() if isinstance(raw, str) else list(raw or [])
        names = []
        for chunk in chunks:
            names.extend(
                [x.strip().lower() for x in re.split(r"[,\s]+", str(chunk)) if x.strip()]
            )
        order = [n for n in names if n in SUPPORTED_PROVIDERS]
        return order or list(SUPPORTED_PROVIDERS)

    def _vme_api(self) -> str:
        url = str(self._c("vme_api", "https://vme.im/api/random")).strip()
        return url if url.startswith("http") else "https://vme.im/api/random"

    def _rsshub_instances(self) -> list:
        # 兼容旧配置键 instances
        raw = self._c("rsshub_instances", None)
        if raw is None:
            raw = self._c("instances", [])
        out = []
        for x in self._as_lines(raw):
            if x.startswith("http") and x.rstrip("/") not in out:
                out.append(x.rstrip("/"))
        return out

    def _channel(self) -> str:
        return str(self._c("channel", "crazythu")).strip().lstrip("@") or "crazythu"

    def _timeout(self) -> float:
        try:
            return max(3.0, min(60.0, float(self._c("timeout", 12))))
        except (TypeError, ValueError):
            return 12.0

    def _groups(self) -> list:
        out = []
        for x in self._as_lines(self._c("groups", [])):
            if x.isdigit() and x not in out:
                out.append(x)
        return out

    # ------------------------------------------------------------------ #
    # 文案审核：交给 AstrBot 当前使用的 LLM 判断，结果按正文指纹缓存
    # ------------------------------------------------------------------ #
    def _filter_enabled(self) -> bool:
        raw = self._c("enabled", True)
        if isinstance(raw, str):
            return raw.strip().lower() not in ("", "0", "false", "no", "off")
        return bool(raw)

    @staticmethod
    def _text_digest(text: str) -> str:
        return hashlib.md5(str(text).encode("utf-8")).hexdigest()

    def _review_cache(self) -> dict:
        data = self._load_json(self._review_file, {})
        return data if isinstance(data, dict) else {}

    def _review_day(self) -> str:
        """审核参照的发送日期；没算出下次时间就退回今天"""
        cand = self._next_run_dt()
        if not cand:
            cand = datetime.now(TZ8)
        return cand.strftime("%Y-%m-%d")

    def _review_prompt(self, text: str) -> str:
        today = datetime.now(TZ8)
        day = self._review_day()
        try:
            send_day = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            send_day = today
        return (
            "今天：%s（%s）\n计划发送日期：%s（%s）\n待审核文案：\n%s"
            % (
                today.strftime("%Y-%m-%d"),
                WEEKDAY_NAMES[today.isoweekday() - 1],
                day,
                WEEKDAY_NAMES[send_day.isoweekday() - 1],
                text[:1200],
            )
        )

    def _cached_verdict(self, text: str):
        """返回 (ok, reason)；没有可用缓存（含换了发送日期）时返回 None"""
        item = self._review_cache().get(self._text_digest(text))
        if not isinstance(item, dict) or "ok" not in item:
            return None
        if item.get("day") != self._review_day():
            return None
        return bool(item["ok"]), str(item.get("reason") or "")

    def _save_verdict(self, text: str, ok: bool, reason: str):
        cache = self._review_cache()
        cache[self._text_digest(text)] = {
            "ok": bool(ok),
            "reason": reason,
            "day": self._review_day(),
            "ts": int(datetime.now(TZ8).timestamp()),
        }
        if len(cache) > 800:  # 简单截断，保留最近的一批
            keep = sorted(cache.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:600]
            cache = dict(keep)
        self._save_json(self._review_file, cache)

    async def _ai_review(self, text: str, force: bool = False) -> tuple:
        """返回 (是否通过, 原因)；审核关闭或 LLM 不可用时一律放行"""
        if not text:
            return False, "文案为空"
        if not self._filter_enabled():
            return True, ""
        if not force:
            cached = self._cached_verdict(text)
            if cached is not None:
                return cached

        provider = None
        try:
            provider = self.context.get_using_provider()
        except Exception as exc:
            logger.warning("[%s] 获取 LLM 失败：%r" % (PLUGIN_NAME, exc))
        if not provider:
            logger.warning("[%s] 未配置可用的 LLM，AI 审核已跳过本次" % PLUGIN_NAME)
            return True, ""

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=self._review_prompt(text), system_prompt=REVIEW_SYSTEM_PROMPT
                ),
                timeout=45,
            )
            answer = str(getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.warning("[%s] AI 审核调用失败，放行该条：%r" % (PLUGIN_NAME, exc))
            return True, ""

        ok, reason = True, ""
        match = re.search(r"\{.*\}", answer, re.S)
        if match:
            try:
                data = json.loads(match.group(0))
                ok = bool(data.get("pass", True))
                reason = str(data.get("reason") or "").strip()[:60]
            except ValueError:
                logger.warning("[%s] AI 审核返回无法解析：%s" % (PLUGIN_NAME, answer[:120]))
        else:
            logger.warning("[%s] AI 审核返回无法解析：%s" % (PLUGIN_NAME, answer[:120]))

        self._save_verdict(text, ok, reason)
        return ok, reason

    def _blocked_count(self, posts: list) -> int:
        """缓存中已被 AI 判定为不通过的条数"""
        n = 0
        for p in posts:
            if not isinstance(p, dict) or not p.get("text"):
                continue
            verdict = self._cached_verdict(p["text"])
            if verdict is not None and not verdict[0]:
                n += 1
        return n

    @staticmethod
    def _parse_hhmm(value) -> tuple:
        m = re.match(r"^(\d{1,2})\s*[:：]\s*(\d{1,2})$", str(value or "").strip())
        if not m:
            return 9, 0
        h, mi = int(m.group(1)), int(m.group(2))
        if not (0 <= h < 24 and 0 <= mi < 60):
            return 9, 0
        return h, mi

    def _weekday(self) -> int:
        try:
            wd = int(self._c("weekday", 4))
        except (TypeError, ValueError):
            return 4
        return wd if 1 <= wd <= 7 else 4

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #
    @staticmethod
    def _load_json(path: Path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    @staticmethod
    def _save_json(path: Path, data):
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))

    def _state(self) -> dict:
        state = self._load_json(self._state_file, {})
        if not isinstance(state, dict):
            state = {}
        state.setdefault("last_run", "")
        state.setdefault("sent", [])
        state.setdefault("last_guid", "")
        state.setdefault("recent_texts", [])
        state.setdefault("last_fetch", "")
        state.setdefault("last_source", "")
        return state

    def _mark_sent(self, guid: str, text: str = ""):
        state = self._state()
        if guid:
            sent = [g for g in state.get("sent", []) if g != guid]
            sent.append(guid)
            state["sent"] = sent[-500:]
            state["last_guid"] = guid
        # 记录正文指纹：即使 guid 不同（如转载/重复投稿）也能识别同一条文案
        if text:
            digest = hashlib.md5(text.encode("utf-8")).hexdigest()
            recent = [x for x in state.get("recent_texts", []) if x != digest]
            recent.append(digest)
            state["recent_texts"] = recent[-3:]
        self._save_json(self._state_file, state)

    def _touch_state(self, **kwargs):
        state = self._state()
        state.update(kwargs)
        state["last_fetch"] = datetime.now(TZ8).strftime("%Y-%m-%d %H:%M:%S")
        self._save_json(self._state_file, state)

    # ------------------------------------------------------------------ #
    # 抓取：多源可插拔
    # ------------------------------------------------------------------ #
    def _vme_mode(self) -> str:
        mode = str(self._c("vme_mode", "latest")).strip().lower()
        return mode if mode in ("latest", "random") else "latest"

    def _vme_repo(self) -> str:
        repo = str(self._c("vme_repo", "vme-im/vme-content")).strip().strip("/")
        return repo or "vme-im/vme-content"

    async def _fetch_vme_latest(self) -> list:
        """按发布时间倒序取最新文案（GitHub issue 列表，比 random 接口可控）"""
        url = GITHUB_ISSUES_API % self._vme_repo()
        params = {
            "state": "all",
            "sort": "created",
            "direction": "desc",
            "per_page": 50,
        }
        headers = {
            "User-Agent": UA,
            "Accept": "application/vnd.github+json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout(), follow_redirects=True
            ) as cli:
                resp = await cli.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                logger.warning(
                    "[%s] vme latest → HTTP %s（剩余配额 %s）"
                    % (
                        PLUGIN_NAME,
                        resp.status_code,
                        resp.headers.get("x-ratelimit-remaining", "?"),
                    )
                )
                return []
            data = resp.json()
        except Exception as exc:
            logger.warning("[%s] vme latest 抓取失败：%r" % (PLUGIN_NAME, exc))
            return []

        if not isinstance(data, list):
            return []

        posts = {}
        for item in data:
            if not isinstance(item, dict) or item.get("pull_request"):
                continue
            body = _strip_markdown_images(item.get("body") or "")
            if len(body) < 8:
                body = (item.get("title") or "").strip()
            if len(body) < 8:
                continue
            key = str(item.get("node_id") or item.get("id") or item.get("number"))
            guid = "vme:%s" % key
            if guid in posts:
                continue
            posts[guid] = {
                "guid": guid,
                "text": body,
                "link": item.get("html_url") or "",
                "created": _iso_ts(item.get("created_at")),
            }

        ordered = sorted(posts.values(), key=lambda x: x.get("created", 0), reverse=True)
        if ordered:
            logger.info("[%s] vme latest 命中 %d 条" % (PLUGIN_NAME, len(ordered)))
        return ordered

    async def _fetch_vme_random(self) -> list:
        """vme.im 随机接口，每次返回一条；多取几次便于去重"""
        url = self._vme_api()
        timeout = self._timeout()
        posts = {}

        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True
            ) as cli:
                for _ in range(3):
                    resp = await cli.get(url, headers={"User-Agent": UA})
                    if resp.status_code != 200:
                        logger.warning(
                            "[%s] vme HTTP %s" % (PLUGIN_NAME, resp.status_code)
                        )
                        break
                    try:
                        data = resp.json()
                    except ValueError:
                        logger.warning("[%s] vme 返回非 JSON" % PLUGIN_NAME)
                        break
                    item = data if isinstance(data, dict) else {}
                    body = _strip_markdown_images(item.get("body") or "")
                    if len(body) < 8:
                        continue  # 纯图片帖
                    key = str(item.get("id") or item.get("url") or "")
                    if not key:
                        key = hashlib.md5(body.encode("utf-8")).hexdigest()
                    posts["vme:%s" % key] = {
                        "guid": "vme:%s" % key,
                        "text": body,
                        "link": item.get("url") or "",
                        "created": _iso_ts(item.get("createdAt")),
                    }
        except Exception as exc:
            logger.warning("[%s] vme 抓取失败：%r" % (PLUGIN_NAME, exc))

        if posts:
            logger.info("[%s] vme random 命中 %d 条" % (PLUGIN_NAME, len(posts)))
        return list(posts.values())

    async def _fetch_vme(self) -> list:
        if self._vme_mode() == "random":
            return await self._fetch_vme_random()
        posts = await self._fetch_vme_latest()
        if not posts:
            logger.info("[%s] 最新列表不可用，回退 vme random" % PLUGIN_NAME)
            return await self._fetch_vme_random()
        return posts

    async def _fetch_rsshub(self) -> list:
        """按实例顺序抓取 RSSHub，失败自动切换"""
        instances = self._rsshub_instances()
        if not instances:
            logger.warning("[%s] 未配置 RSSHub 实例" % PLUGIN_NAME)
            return []

        channel = self._channel()
        timeout = self._timeout()

        for base in instances:
            url = "%s/telegram/channel/%s" % (base, channel)
            try:
                async with httpx.AsyncClient(
                    timeout=timeout, follow_redirects=True
                ) as cli:
                    resp = await cli.get(url, headers={"User-Agent": UA})
                if resp.status_code != 200:
                    logger.warning(
                        "[%s] %s → HTTP %s" % (PLUGIN_NAME, base, resp.status_code)
                    )
                    continue
                posts = parse_rss(resp.text)
                if posts:
                    logger.info(
                        "[%s] RSSHub 命中 %d 条（%s）"
                        % (PLUGIN_NAME, len(posts), base)
                    )
                    self._touch_state(last_source="rsshub:%s" % base)
                    return posts
            except Exception as exc:
                logger.warning("[%s] %s 抓取失败：%r" % (PLUGIN_NAME, base, exc))

        return []

    async def _load_posts(self) -> list:
        """按配置的源顺序依次尝试，全部失败则返回空"""
        if httpx is None:
            logger.error("[%s] 缺少 httpx 依赖" % PLUGIN_NAME)
            return []

        for name in self._provider_order():
            try:
                if name == "vme":
                    posts = await self._fetch_vme()
                elif name == "rsshub":
                    posts = await self._fetch_rsshub()
                else:
                    continue
            except Exception as exc:
                logger.warning("[%s] 源 %s 异常：%r" % (PLUGIN_NAME, name, exc))
                continue
            if posts:
                if name == "vme":
                    self._touch_state(last_source="vme")
                return posts

        logger.error("[%s] 所有文案源均无结果" % PLUGIN_NAME)
        return []

    def _merge_cache(self, fresh: list) -> list:
        cache = self._load_json(self._posts_file, [])
        if not isinstance(cache, list):
            cache = []

        now_ts = int(datetime.now(TZ8).timestamp())
        by_guid = {}
        for p in cache:
            if isinstance(p, dict) and p.get("guid"):
                by_guid[p["guid"]] = p

        for p in fresh:
            old = by_guid.get(p["guid"])
            if old:
                old["text"] = p["text"]
                old["link"] = p.get("link", "")
                if p.get("created"):
                    old["created"] = p["created"]
            else:
                by_guid[p["guid"]] = {
                    "guid": p["guid"],
                    "text": p["text"],
                    "link": p.get("link", ""),
                    "created": p.get("created", 0),
                    "first_seen": now_ts,
                }

        merged = sorted(
            by_guid.values(), key=lambda x: x.get("first_seen", 0), reverse=True
        )[:300]
        self._save_json(self._posts_file, merged)
        return merged

    def _usable(self, posts: list) -> list:
        return [
            p
            for p in posts
            if isinstance(p, dict) and p.get("text") and len(p["text"]) <= MAX_TEXT_LEN
        ]

    def _candidates(self, posts: list, state: dict) -> list:
        """按发布时间倒序给出候选序列，四重降级保证相邻不重复：

        1. 没发过 + 正文没发过
        2. 池子耗尽则重置，但仍排除上一条的 guid 与最近几条正文
        3. 再退一步只排除上一条 guid
        4. 只有池子里仅剩一条时才允许重复
        """
        usable = self._usable(posts)
        if not usable:
            return []

        sent = set(state.get("sent", []))
        recent = set(state.get("recent_texts", []))
        last_guid = state.get("last_guid", "")

        def digest(p):
            return hashlib.md5(p["text"].encode("utf-8")).hexdigest()

        # 排序基准：有真实发布时间的按时间，没有的退回首次入库时间
        def order_key(p):
            return (p.get("created") or 0, p.get("first_seen") or 0)

        pool = [p for p in usable if p["guid"] not in sent and digest(p) not in recent]
        if not pool:
            pool = [
                p
                for p in usable
                if p["guid"] != last_guid and digest(p) not in recent
            ]
        if not pool:
            pool = [p for p in usable if p["guid"] != last_guid]
        if not pool:
            pool = usable

        pool.sort(key=order_key, reverse=True)
        return pool

    def _pick(self, posts: list, state: dict) -> dict:
        pool = self._candidates(posts, state)
        return pool[0] if pool else {}

    def _remaining(self, posts: list, state: dict) -> int:
        """还没发过的条数，用于在面板上展示余量"""
        sent = set(state.get("sent", []))
        return len([p for p in self._usable(posts) if p["guid"] not in sent])

    async def _compose(self) -> tuple:
        """返回 (文案正文, guid, 来源)；guid 为空表示用的是内置兜底文案"""
        state = self._state()
        fresh = await self._load_posts()
        if fresh:
            posts = self._merge_cache(fresh)
            source = self._state().get("last_source", "")
        else:
            posts = self._load_json(self._posts_file, [])
            if not isinstance(posts, list):
                posts = []
            source = "缓存"

        # 从最新的开始逐条送 AI 审核，第一条通过的才发送
        for item in self._candidates(posts, state)[:REVIEW_MAX_CANDIDATES]:
            ok, reason = await self._ai_review(item.get("text", ""))
            if ok:
                return item.get("text", ""), item.get("guid", ""), source
            logger.info(
                "[%s] 审核不通过，跳过 %s：%s"
                % (PLUGIN_NAME, item.get("guid", "")[:18], reason or "无原因")
            )

        logger.info("[%s] 本轮没有通过审核的文案，改用内置文案" % PLUGIN_NAME)
        return random.choice(BUILTIN_TEXTS), "", "内置"

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    def _platform(self):
        insts = list(getattr(self.context.platform_manager, "platform_insts", None) or [])
        for inst in insts:
            try:
                name = getattr(inst.meta(), "name", "")
            except Exception:
                name = ""
            # PlatformMetadata 没有 type 字段，按适配器名/类名识别
            if name == "aiocqhttp" or type(inst).__name__ == "AiocqhttpAdapter":
                return inst
        for inst in insts:
            if hasattr(inst, "get_client"):
                return inst
        return None

    async def _send_group(self, group_id: str, text: str) -> bool:
        if not text:
            return False
        inst = self._platform()
        if inst is None:
            logger.error("[%s] 没有可用的平台实例，发送失败" % PLUGIN_NAME)
            return False

        try:
            meta = inst.meta()
        except Exception:
            meta = None
        platform_id = getattr(meta, "id", "") or "aiocqhttp"

        # 1) 适配器标准发送（可持久化会话）
        if MessageSession and MessageType:
            try:
                session = MessageSession(
                    platform_id, MessageType.GROUP_MESSAGE, str(group_id)
                )
                await inst.send_by_session(session, MessageChain().message(text))
                return True
            except Exception as exc:
                logger.warning(
                    "[%s] send_by_session 失败，改用 OneBot API：%r"
                    % (PLUGIN_NAME, exc)
                )

        # 2) 直接调用 OneBot v11 API
        client = None
        try:
            client = inst.get_client()
        except Exception as exc:
            logger.error("[%s] 获取 OneBot 客户端失败：%r" % (PLUGIN_NAME, exc))
            return False

        try:
            await client.send_group_msg(group_id=int(group_id), message=text)
            return True
        except Exception as exc:
            logger.debug("[%s] send_group_msg 失败：%r" % (PLUGIN_NAME, exc))

        try:
            await client.api.call_action(
                "send_group_msg", group_id=int(group_id), message=text
            )
            return True
        except Exception as exc:
            logger.error("[%s] 群 %s 发送失败：%r" % (PLUGIN_NAME, group_id, exc))
            return False

    async def _broadcast(self, groups: list) -> dict:
        groups = [str(g).strip() for g in (groups or []) if str(g).strip().isdigit()]
        if not groups:
            return {"ok": False, "text": "", "success": 0, "total": 0, "source": ""}

        text, guid, source = await self._compose()
        success = 0
        for gid in groups:
            if await self._send_group(gid, text):
                success += 1
            await asyncio.sleep(0.8)  # 稍作间隔，降低风控概率

        if success:
            # 内置兜底文案没有 guid，但也要记录正文指纹，防止连续两次发到同一条
            self._mark_sent(guid, text)

        logger.info(
            "[%s] 发送完成：%d/%d 个群成功" % (PLUGIN_NAME, success, len(groups))
        )
        return {
            "ok": success > 0,
            "text": text,
            "success": success,
            "total": len(groups),
            "source": source,
        }

    # ------------------------------------------------------------------ #
    # 定时
    # ------------------------------------------------------------------ #
    async def _loop(self):
        await asyncio.sleep(5)  # 等 AstrBot 初始化完成
        while not self._closed:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[%s] 定时任务异常：%r" % (PLUGIN_NAME, exc))
            await asyncio.sleep(20)

    async def _tick(self):
        now = datetime.now(TZ8)
        if now.isoweekday() != self._weekday():
            return

        hour, minute = self._parse_hhmm(self._c("time", "09:00"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = (now - target).total_seconds()
        # 只在目标时间后 1 小时内补发（容器重启也能接上），超过则当天放弃
        if not (0 <= delta <= 3600):
            return

        today = now.strftime("%Y-%m-%d")
        if self._state().get("last_run") == today:
            return

        groups = self._groups()
        if not groups:
            return

        result = await self._broadcast(groups)
        if result["ok"]:
            state = self._state()
            state["last_run"] = today
            self._save_json(self._state_file, state)

    def _next_run_dt(self):
        """下一次计划发送的时间；找不到返回 None"""
        now = datetime.now(TZ8)
        hour, minute = self._parse_hhmm(self._c("time", "09:00"))
        for i in range(0, 8):
            cand = (now + timedelta(days=i)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if cand.isoweekday() == self._weekday() and cand > now:
                return cand
        return None

    def _next_run(self):
        cand = self._next_run_dt()
        if not cand:
            return ""
        return "%s (%s)" % (
            cand.strftime("%Y-%m-%d %H:%M"),
            WEEKDAY_NAMES[cand.isoweekday() - 1],
        )

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #
    @filter.command("疯狂星期四")
    async def cmd_crazy_thursday(self, event: AstrMessageEvent):
        """在当前群手动发一条疯狂星期四文案"""
        text, guid, _ = await self._compose()
        await event.send(event.plain_result(text))
        self._mark_sent(guid, text)
        event.stop_event()

    # ------------------------------------------------------------------ #
    # Web API
    # ------------------------------------------------------------------ #
    def _register_web_apis(self):
        p = "/%s" % PLUGIN_NAME
        self.context.register_web_api(p + "/status", self.api_status, ["GET"], "运行状态")
        self.context.register_web_api(
            p + "/save_config", self.api_save_config, ["POST"], "保存配置"
        )
        self.context.register_web_api(p + "/fetch", self.api_fetch, ["POST"], "刷新缓存")
        self.context.register_web_api(
            p + "/preview", self.api_preview, ["POST"], "预览文案"
        )
        self.context.register_web_api(
            p + "/review", self.api_review, ["POST"], "手动审核缓存文案"
        )
        self.context.register_web_api(p + "/send", self.api_send, ["POST"], "立即发送")

    async def api_status(self):
        state = self._state()
        posts = self._load_json(self._posts_file, [])
        return json_response(
            {
                "groups": self._groups(),
                "weekday": self._weekday(),
                "time": str(self._c("time", "09:00")),
                "provider_order": self._provider_order(),
                "vme_api": self._vme_api(),
                "vme_mode": self._vme_mode(),
                "vme_repo": self._vme_repo(),
                "rsshub_instances": self._rsshub_instances(),
                "channel": self._channel(),
                "timeout": int(self._timeout()),
                "next_run": self._next_run(),
                "last_run": state.get("last_run", ""),
                "last_fetch": state.get("last_fetch", ""),
                "last_source": state.get("last_source", ""),
                "cache_count": len(posts) if isinstance(posts, list) else 0,
                "remaining": self._remaining(
                    posts if isinstance(posts, list) else [], state
                ),
                "sent_count": len(state.get("sent", [])),
                "blocked_count": self._blocked_count(
                    posts if isinstance(posts, list) else []
                ),
                "filter_enabled": self._filter_enabled(),
                "supported_providers": list(SUPPORTED_PROVIDERS),
            }
        )

    async def api_save_config(self):
        body = await request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体格式错误", status_code=400)

        if "groups" in body:
            groups = self._as_lines(body.get("groups"))
            bad = [g for g in groups if not g.isdigit()]
            if bad:
                return error_response("群号必须是纯数字：%s" % ", ".join(bad), 400)
            self._set_conf("send", "groups", groups)

        if "provider_order" in body:
            names = [x.lower() for x in self._as_lines(body.get("provider_order"))]
            bad = [n for n in names if n not in SUPPORTED_PROVIDERS]
            if bad:
                return error_response(
                    "未知的文案源：%s（支持 %s）"
                    % (", ".join(bad), ", ".join(SUPPORTED_PROVIDERS)),
                    400,
                )
            if not names:
                return error_response("至少保留一个文案源", 400)
            self._set_conf("source", "provider_order", names)

        if "vme_api" in body:
            url = str(body.get("vme_api") or "").strip()
            if not url.startswith("http"):
                return error_response("vme 接口地址需以 http 开头", 400)
            self._set_conf("source", "vme_api", url)

        if "vme_mode" in body:
            mode = str(body.get("vme_mode") or "").strip().lower()
            if mode not in ("latest", "random"):
                return error_response("vme 模式只能是 latest 或 random", 400)
            self._set_conf("source", "vme_mode", mode)

        if "vme_repo" in body:
            repo = str(body.get("vme_repo") or "").strip().strip("/")
            if repo.count("/") != 1:
                return error_response("仓库格式应为 owner/repo", 400)
            self._set_conf("source", "vme_repo", repo)

        if "time" in body:
            hour, minute = self._parse_hhmm(body.get("time"))
            self._set_conf("schedule", "time", "%02d:%02d" % (hour, minute))

        if "weekday" in body:
            try:
                wd = int(body.get("weekday"))
            except (TypeError, ValueError):
                return error_response("星期设置错误", 400)
            if not 1 <= wd <= 7:
                return error_response("星期需在 1-7 之间", 400)
            self._set_conf("schedule", "weekday", wd)

        if "rsshub_instances" in body:
            instances = []
            for x in self._as_lines(body.get("rsshub_instances")):
                x = x.rstrip("/")
                if not x.startswith("http"):
                    return error_response("实例地址需以 http 开头：%s" % x, 400)
                if x not in instances:
                    instances.append(x)
            self._set_conf("source", "rsshub_instances", instances)

        if "channel" in body:
            channel = str(body.get("channel") or "").strip().lstrip("@")
            if not channel:
                return error_response("频道名不能为空", 400)
            self._set_conf("source", "channel", channel)

        if "timeout" in body:
            try:
                timeout = int(body.get("timeout"))
            except (TypeError, ValueError):
                return error_response("超时需为整数秒", 400)
            if not 3 <= timeout <= 60:
                return error_response("超时需在 3-60 秒之间", 400)
            self._set_conf("source", "timeout", timeout)

        if "filter_enabled" in body:
            raw = body.get("filter_enabled")
            if isinstance(raw, str):
                enabled = raw.strip().lower() not in ("", "0", "false", "no", "off")
            else:
                enabled = bool(raw)
            self._set_conf("filter", "enabled", enabled)

        await self.config.save_config_async()
        logger.info("[%s] 配置已保存" % PLUGIN_NAME)
        return json_response({"ok": True, "next_run": self._next_run()})

    async def api_fetch(self):
        fresh = await self._load_posts()
        if not fresh:
            return error_response("所有文案源均抓取失败，已保留原缓存", status_code=502)
        merged = self._merge_cache(fresh)
        return json_response(
            {
                "ok": True,
                "source": self._state().get("last_source", ""),
                "fetched": len(fresh),
                "cache_count": len(merged),
            }
        )

    def _preview_data(self) -> dict:
        """缓存文案 + 当前审核结果，供预览/手动审核复用"""
        state = self._state()
        posts = self._load_json(self._posts_file, [])
        if not isinstance(posts, list):
            posts = []
        sent = set(state.get("sent", []))

        items = []
        for p in posts:
            if not isinstance(p, dict) or not p.get("text"):
                continue
            verdict = self._cached_verdict(p["text"])
            if verdict is None:
                review, reason, blocked = "pending", "", False
            elif verdict[0]:
                review, reason, blocked = "pass", "", False
            else:
                review, reason, blocked = "block", verdict[1], True
            created = p.get("created") or 0
            items.append(
                {
                    "guid": p.get("guid", ""),
                    "text": p["text"],
                    "created": created,
                    "date": (
                        datetime.fromtimestamp(created, TZ8).strftime("%Y-%m-%d")
                        if created
                        else ""
                    ),
                    "first_seen": p.get("first_seen") or 0,
                    "sent": p.get("guid", "") in sent,
                    "review": review,
                    "blocked": blocked,
                    "reason": reason,
                }
            )

        items.sort(key=lambda x: (x["created"] or 0, x["first_seen"] or 0), reverse=True)
        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "usable": len([i for i in items if not i["blocked"]]),
            "blocked": len([i for i in items if i["blocked"]]),
            "pending": len([i for i in items if i["review"] == "pending"]),
            "source": state.get("last_source", "缓存"),
            "filter_enabled": self._filter_enabled(),
            "review_day": self._review_day(),
        }

    async def api_preview(self):
        """列出缓存里的全部文案（含审核结果），不改动发送状态"""
        return json_response(self._preview_data())

    async def api_review(self):
        """手动提前审核缓存文案；all=true 时忽略旧结果全部重审"""
        body = await request.json(default={})
        force = isinstance(body, dict) and bool(body.get("all"))
        if not self._filter_enabled():
            return error_response("AI 审核未启用，请先保存配置开启", 400)

        posts = self._load_json(self._posts_file, [])
        if not isinstance(posts, list):
            posts = []

        todo, seen = [], set()
        for p in posts:
            if not isinstance(p, dict) or not p.get("text"):
                continue
            digest = self._text_digest(p["text"])
            if digest in seen:
                continue
            seen.add(digest)
            if force or self._cached_verdict(p["text"]) is None:
                todo.append(p["text"])

        sem = asyncio.Semaphore(REVIEW_WORKERS)

        async def run(text):
            async with sem:
                await self._ai_review(text, force=force)

        if todo:
            await asyncio.gather(*[run(t) for t in todo])
            logger.info("[%s] 手动审核完成，共送审 %d 条" % (PLUGIN_NAME, len(todo)))

        data = self._preview_data()
        data["reviewed"] = len(todo)
        return json_response(data)

    async def api_send(self):
        body = await request.json(default={})
        groups = body.get("groups") if isinstance(body, dict) else None
        if not groups:
            groups = self._groups()
        if not groups:
            return error_response("未配置目标群", 400)

        result = await self._broadcast(groups)
        if not result["ok"]:
            return error_response("全部群发送失败，请查看日志", status_code=502)
        return json_response(result)
