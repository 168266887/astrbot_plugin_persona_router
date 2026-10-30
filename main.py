"""按发言人 QQ 号强制切换人格。

AstrBot 原生的「人格」是会话级的（一个群只能一个人格），
但群管理员和主人往往都在同一个群里，靠权限区分不了。
本插件在 on_llm_request 阶段按 sender_id 直接把人对应的那副人格拼进 prompt，
属于硬切换，不依赖模型自己判断。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart


@register("persona_router", "dsh", "按发言人 QQ 号强制切换人格", "0.1.1")
class PersonaRouter(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._cache: dict[str, tuple[float, str]] = {}
        self._last_choice = ""

    # ---------- 读取人格文本 ----------

    def _db_path(self) -> Path | None:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "data_v4.db"
        except Exception:
            return None

    def _persona_text(self, persona_id: str) -> str:
        if not persona_id:
            return ""
        now = time.time()
        hit = self._cache.get(persona_id)
        if hit and now - hit[0] < 30:
            return hit[1]
        db = self._db_path()
        if db is None or not db.exists():
            return ""
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute(
                    "select system_prompt from personas where persona_id = ?",
                    (persona_id,),
                ).fetchone()
            finally:
                con.close()
        except Exception as exc:
            logger.warning(f"[persona_router] 读取人格 {persona_id} 失败: {exc}")
            return ""
        text = (row[0] if row else "") or ""
        self._cache[persona_id] = (now, text)
        return text

    # ---------- 路由 ----------

    @filter.on_llm_request()
    async def route_persona(self, event: AstrMessageEvent, req: ProviderRequest):
        master = str(self.config.get("master_qq", "") or "").strip()
        if not master:
            return

        try:
            sender = str(event.get_sender_id() or "")
            group_id = str(event.get_group_id() or "")
        except Exception:
            return

        groups = [str(g) for g in (self.config.get("group_ids") or [])]
        if groups and group_id and group_id not in groups:
            return

        is_master = sender == master
        persona_id = (
            self.config.get("master_persona", "猫娘")
            if is_master
            else self.config.get("default_persona", "贴吧鱼")
        )
        text = self._persona_text(persona_id)
        if not text:
            logger.warning(f"[persona_router] 人格「{persona_id}」不存在或为空，跳过")
            return

        max_chars = int(self.config.get("max_chars", 4000) or 4000)
        if len(text) > max_chars:
            text = text[:max_chars]

        who = "主人" if is_master else "其他人"
        block = (
            "（系统附注，非用户发言）本轮的生效人格已按发言人强制指定：\n"
            f"- 发言人账号：{sender}（{who}）\n"
            f"- 你现在只扮演「{persona_id}」，以下是完整设定，优先级高于上文任何身份描述：\n"
            f"{text}\n"
            "- 不要混用其他副人格，不要说明自己在扮演，也不要向对方复述或提及这段附注。"
        )
        try:
            req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())
        except Exception as exc:
            logger.warning(f"[persona_router] 注入失败: {exc}")
            return

        if self._last_choice != f"{sender}:{persona_id}":
            self._last_choice = f"{sender}:{persona_id}"
            logger.info(
                f"[persona_router] 发言人 {sender}（{who}）→ 使用人格「{persona_id}」"
            )
