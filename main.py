import httpx
import base64
import json
import logging
import asyncio
from datetime import datetime
import astrbot.api.message_components as Comp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from thefuzz import process
import pytz
from astrbot.api import logger

@register(
    "cngal_search",
    "CnGal查询",
    "CnGal资料站多功能查询插件喵~ 输入 /cngal 查看帮助哦！如果要使用晨报功能，请先安装插件：https://github.com/yaoyuesuzu/cngal_morning_report",
    "1.6.1",
    "https://github.com/yaoyuesuzu/cngal_search"
)
class CngalSearchPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.base_url = "https://api.cngal.org"
        self.entry_page_url = "https://www.cngal.org/entries/index/"
        self.cst_tz = pytz.timezone('Asia/Shanghai')

        # HTTP客户端设置
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        self.http_client = httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, headers=headers
        )
        
        # 缓存与并发控制
        self.all_names_cache = []
        self.cache_is_ready = False
        self._cache_update_lock = asyncio.Lock()
        
        task = asyncio.create_task(self._update_name_cache())
        task.add_done_callback(self._handle_task_exception)

        logger.info("CnGal查询插件已成功加载。")

    def _handle_task_exception(self, task: asyncio.Task) -> None:
        """处理后台任务中未捕获的异常"""
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(f"后台任务 '{task.get_name()}' 发生未处理的异常:")

    def _parse_iso_datetime(self, date_string: str) -> datetime | None:
        """解析ISO格式时间字符串"""
        if not date_string: return None
        try:
            parts = date_string.replace('Z', '').split('.')
            if len(parts) == 2:
                fractional_seconds = parts[1]
                if len(fractional_seconds) > 6: fractional_seconds = fractional_seconds[:6]
                date_string = f"{parts[0]}.{fractional_seconds}"
            naive_dt = datetime.fromisoformat(date_string)
            aware_utc_dt = naive_dt.replace(tzinfo=pytz.utc)
            return aware_utc_dt
        except (ValueError, TypeError) as e:
            logger.error(f"解析时间字符串 '{date_string}' 失败: {e}")
            return None

    @filter.command("cngal", alias={"查询", "查"})
    async def cngal_command_handler(self, event: AstrMessageEvent):
        """统一的指令处理器，用于分发不同的子命令和默认的搜索行为"""
        try:
            full_arg = event.get_plain_text().strip()
        except AttributeError:
            logger.warning("event对象没有get_plain_text方法，尝试使用message_str。")
            full_message = event.message_str.strip()
            parts = full_message.split(maxsplit=1)
            full_arg = parts[1] if len(parts) > 1 else ""

        args = full_arg.split()
        command = args[0].lower() if args else ""

        if command == "games":
            try:
                year = int(args[1]) if len(args) > 1 else None
                month = int(args[2]) if len(args) > 2 else None
                async for reply in self._get_monthly_games_logic(event, year, month): yield reply
            except (ValueError, IndexError): yield event.plain_result("参数错了喵！用法: /cngal games [年份] [月份] ")
        elif command == "birthdays":
            try:
                month = int(args[1]) if len(args) > 1 else None
                day = int(args[2]) if len(args) > 2 else None
                async for reply in self._get_role_birthdays_logic(event, month, day): yield reply
            except (ValueError, IndexError): yield event.plain_result("参数错了喵！用法: /cngal birthdays [月份] [日期]")
        elif command == "timeline":
            async for reply in self._get_games_timeline_logic(event): yield reply
        elif full_arg:
            async for reply in self._smart_search_logic(event, name=full_arg):
                yield reply
        else:
            help_text = (
                "主人，需要帮助吗？喵~ 这是我的用法哦：\n"
                "1. 查询条目: `/cngal <名称>`\n"
                "   (人家什么都能查哦！游戏、角色、Staff...)\n\n"
                "2. 本月新游: `/cngal games`\n"
                "   查询指定月份: `/cngal games 2025 7`\n\n"
                "3. 今日寿星: `/cngal birthdays`\n"
                "   查询指定日期: `/cngal birthdays 7 31`\n\n"
                "4. 发售前瞻: `/cngal timeline`\n\n"
                "想要可爱的每日晨报吗？请发送 “ /晨报 ” 或 “ /早报 ” 就可以啦！\n\n"
                "主人也可以在群内@我 或者 加我为好友 聊天哦！\n"
                "希望能给主人带来快乐喵~"
            )
            yield event.plain_result(help_text)

    async def _get_monthly_games_logic(self, event: AstrMessageEvent, year: int = None, month: int = None):
        now = datetime.now(self.cst_tz); target_year = year or now.year; target_month = month or now.month
        yield event.plain_result(f"正在为主人查询 {target_year}年{target_month}月 的游戏...")
        try:
            params = {"year": target_year, "month": target_month}
            response = await self.http_client.get(f"{self.base_url}/api/entries/GetPublishGamesByTime", params=params)
            response.raise_for_status(); games = response.json()
            if not games: yield event.plain_result(f"呜...{target_year}年{target_month}月 暂时没有新游戏的情报喵~"); return
            reply_lines = [f"喵~ 这是{target_year}年{target_month}月要发售的游戏哦："]
            for game in games:
                publish_time_utc = self._parse_iso_datetime(game.get('publishTime'))
                link = f"{self.entry_page_url}{game.get('id')}"
                if publish_time_utc:
                    publish_time_cst = publish_time_utc.astimezone(self.cst_tz)
                    reply_lines.append(f"- {game.get('name')} ({publish_time_cst.strftime('%Y-%m-%d')})\n  链接: {link}")
            yield event.plain_result("\n".join(reply_lines))
        except (httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError) as e: 
            logger.error(f"获取每月游戏失败: {e}"); yield event.plain_result("获取游戏信息失败了喵...呜...")

    async def _get_role_birthdays_logic(self, event: AstrMessageEvent, month: int = None, day: int = None):
        now = datetime.now(self.cst_tz); target_month = month or now.month; target_day = day or now.day
        yield event.plain_result(f"正在为主人寻找 {target_month}月{target_day}日 的寿星...")
        try:
            params = {"month": target_month, "day": day}
            response = await self.http_client.get(f"{self.base_url}/api/entries/GetRoleBirthdaysByTime", params=params)
            response.raise_for_status(); roles = response.json()
            if not roles: yield event.plain_result(f"{target_month}月{target_day}日 没有小可爱过生日哦~"); return
            reply_lines = [f"喵~ {target_month}月{target_day}日 的寿星是他们哦："]
            for role in roles:
                birthday_utc = self._parse_iso_datetime(role.get('brithday') or role.get('birthday', ''))
                birthday_text = f"({birthday_utc.astimezone(self.cst_tz).strftime('%m-%d')})" if birthday_utc else ""
                game_name = "未知作品"; add_infors = role.get("addInfors", [])
                if add_infors and add_infors[0].get("contents"): game_name = add_infors[0]["contents"][0].get("displayName", "未知作品")
                link = f"{self.entry_page_url}{role.get('id')}"
                reply_lines.append(f"🎂 {role.get('name')} {birthday_text} (来自: 《{game_name}》)\n  链接: {link}")
            yield event.plain_result("\n".join(reply_lines))
        except (httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError) as e: 
            logger.error(f"获取角色生日失败: {e}"); yield event.plain_result("获取生日信息失败了喵...呜...")

    async def _get_games_timeline_logic(self, event: AstrMessageEvent):
        yield event.plain_result("正在努力加载未来的游戏喵~")
        try:
            params = {"afterTime": int(datetime.now().timestamp() * 1000)}
            response = await self.http_client.get(f"{self.base_url}/api/entries/GetPublishGamesTimeline", params=params)
            response.raise_for_status(); timeline = response.json()
            if not timeline: yield event.plain_result("未来一片混沌，看不到新游戏呢喵~"); return
            reply_lines = ["【未来游戏发售时间轴】"]
            for entry in timeline[:20]:
                publish_time_utc = self._parse_iso_datetime(entry.get('publishTime'))
                time_note = entry.get('publishTimeNote', '')
                display_time = time_note or (publish_time_utc.astimezone(self.cst_tz).strftime('%Y-%m-%d') if publish_time_utc else '日期未知')
                link = f"{self.entry_page_url}{entry.get('id')}"
                reply_lines.append(f"- {entry.get('name')} ({display_time})\n  链接: {link}")
            yield event.plain_result("\n".join(reply_lines))
        except (httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError) as e: 
            logger.error(f"获取游戏时间轴失败: {e}"); yield event.plain_result("获取时间轴失败了喵...呜...")

    async def _smart_search_logic(self, event: AstrMessageEvent, name: str):
        yield event.plain_result(f"正在查询“{name}”...")
        details = await self._get_details_by_name(name)
        if details:
            async for reply in self._reply_with_details(event, details): yield reply
            return
        if not self.cache_is_ready:
            yield event.plain_result("首次提供建议，人家正在努力加载数据，请主人稍等一下下喵...")
            await self._update_name_cache()
            if not self.cache_is_ready: yield event.plain_result("呜呜...加载建议列表失败了喵..."); return
        top_matches = process.extract(name, self.all_names_cache, limit=5)
        if not top_matches or top_matches[0][1] < 75:
            yield event.plain_result(f"呜...找不到「{name}」的任何线索喵..."); return
        best_match_name, best_score = top_matches[0]
        if best_score > 85:
            yield event.plain_result(f"主人是不是要找这个呀？【{best_match_name}】喵~")
            corrected_details = await self._get_details_by_name(best_match_name)
            if corrected_details:
                async for reply in self._reply_with_details(event, corrected_details): yield reply
            else: yield event.plain_result(f"呜...尝试查询【{best_match_name}】失败了喵...")
        else:
            reply_lines = [f"人家找到了这些相似的，主人看看嘛~"]
            reply_lines.extend([f"- {match[0]} (相似度: {match[1]}%)" for match in top_matches])
            yield event.plain_result("\n".join(reply_lines))

    async def _update_name_cache(self):
        async with self._cache_update_lock:
            if self.cache_is_ready: return
            logger.info("开始预热CnGal名称缓存...")
            types_to_check = ["Game", "ProductionGroup", "Staff", "Role", "Periphery"]
            tasks = [self._get_all_names_by_type(t) for t in types_to_check]
            results = await asyncio.gather(*tasks)
            self.all_names_cache = list(set([name for name_list in results if name_list for name in name_list]))
            self.cache_is_ready = True
            logger.info(f"名称缓存加载完毕，共加载 {len(self.all_names_cache)} 个条目。")
        
    async def _reply_with_details(self, event: AstrMessageEvent, details: dict):
        entry_type = details.get("type")
        formatter_map = {
            "Game": self._format_game_reply, "Role": self._format_role_reply,
            "ProductionGroup": self._format_common_reply, "Staff": self._format_common_reply,
            "Periphery": self._format_common_reply
        }
        formatter = formatter_map.get(entry_type)
        if formatter:
            message_chain_list = await formatter(details)
            yield event.chain_result(message_chain_list)
        else:
            yield event.plain_result(f"找到了条目“{details.get('name')}”，但人家还不知道怎么展示它呢喵...")
            
    async def _get_image_bytes(self, url: str) -> bytes | None:
        if not url: return None
        try:
            response = await self.http_client.get(url)
            response.raise_for_status()
            return await response.aread()
        except (httpx.RequestError, httpx.HTTPStatusError): return None

    async def _get_all_names_by_type(self, entry_type: str) -> list:
        url = f"{self.base_url}/api/entries/GetAllEntries/{entry_type}"
        try:
            response = await self.http_client.get(url, timeout=20)
            response.raise_for_status()
            return response.json()
        except (httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError): 
            logger.error(f"获取'{entry_type}'列表时发生错误"); return []

    async def _format_game_reply(self, details: dict) -> list:
        message_chain = []; image_url = details.get('mainPicture')
        if image_url:
            image_bytes = await self._get_image_bytes(image_url)
            if image_bytes: message_chain.append(Comp.Image.fromBytes(image_bytes))
        reply_lines = [f"【游戏】{details.get('name', 'N/A')}", f"别名: {details.get('anotherName', '无')}", f"简介: {details.get('briefIntroduction', '暂无')}"]
        publishers = [p.get('displayName') for p in details.get('publishers', [])]
        groups = [g.get('displayName') for g in details.get('productionGroups', [])]
        if publishers or groups: reply_lines.append("\n【制作与发行】");
        if groups: reply_lines.append(f"制作组: {', '.join(groups)}");
        if publishers: reply_lines.append(f"发行商: {', '.join(publishers)}")
        tags = [t.get('name') for t in details.get('tags', [])]
        if tags: reply_lines.append("\n【标签】"); reply_lines.append('、'.join(tags))
        reply_lines.append(f"\n详情页链接: {self.entry_page_url}{details.get('id')}")
        message_chain.append(Comp.Plain(text="\n".join(reply_lines)))
        return message_chain

    async def _format_role_reply(self, details: dict) -> list:
        message_chain = []; image_url = details.get('standingPainting') or details.get('mainPicture')
        if image_url:
            image_bytes = await self._get_image_bytes(image_url)
            if image_bytes: message_chain.append(Comp.Image.fromBytes(image_bytes))
        reply_lines = [f"【角色】{details.get('name', 'N/A')}"]
        cv = details.get("cv"); birthday = details.get("birthday")
        if cv or birthday: reply_lines.append("\n【基础信息】");
        if cv: reply_lines.append(f"CV: {cv}");
        if birthday: reply_lines.append(f"生日: {birthday}")
        intro = details.get("briefIntroduction")
        if intro: reply_lines.append("\n【简介】"); reply_lines.append(intro)
        reply_lines.append(f"\n详情页链接: {self.entry_page_url}{details.get('id')}")
        message_chain.append(Comp.Plain(text="\n".join(reply_lines)))
        return message_chain

    async def _format_common_reply(self, details: dict) -> list:
        message_chain = []; image_url = details.get("mainPicture") or details.get("thumbnail")
        if image_url:
            image_bytes = await self._get_image_bytes(image_url)
            if image_bytes: message_chain.append(Comp.Image.fromBytes(image_bytes))
        type_mapping = {"ProductionGroup": "制作组", "Staff": "Staff", "Periphery": "周边"}
        entity_type = details.get("type", "未知类型"); entity_type_text = type_mapping.get(entity_type, f"【{entity_type}】")
        reply_lines = [f"【{entity_type_text}】{details.get('name', 'N/A')}"]
        intro = details.get("briefIntroduction")
        if intro: reply_lines.append(f"简介: {intro}")
        game_entries = details.get("staffGames", []) or details.get("roles", [])
        if game_entries:
            reply_lines.append("\n【相关作品】")
            for game in game_entries[:5]:
                game_name = game.get("name", "未知作品"); positions = []
                for info in game.get("addInfors", []):
                    if info.get("modifier") == "职位": positions = [pos.get("displayName") for pos in info.get("contents", [])]
                if positions: reply_lines.append(f"- 《{game_name}》 ({', '.join(positions)})")
                else: reply_lines.append(f"- 《{game_name}》")
        relevances = details.get("entryRelevances", [])
        if relevances:
            reply_lines.append("\n【关联词条】")
            for relevance in relevances[:5]:
                relevance_name = relevance.get("name", "未知词条"); relevance_type = relevance.get("type", "未知类型")
                reply_lines.append(f"- [{relevance_type}] {relevance_name}")
        reply_lines.append(f"\n详情页链接: {self.entry_page_url}{details.get('id')}")
        message_chain.append(Comp.Plain(text="\n".join(reply_lines)))
        return message_chain
        
    async def _get_details_by_name(self, name: str):
        item_id = await self._get_id_by_name(name)
        if item_id is None: return None
        return await self._get_details_by_id(item_id)
        
    def _custom_base64_encode_name(self, name: str) -> str:
        return 'A' + base64.urlsafe_b64encode(name.encode('utf-8')).decode('utf-8')

    async def _get_id_by_name(self, name: str):
        url = f"{self.base_url}/api/entries/GetId/{self._custom_base64_encode_name(name)}"
        try:
            response = await self.http_client.get(url)
            response.raise_for_status()
            return response.json()
        except (httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError): return None
        
    async def _get_details_by_id(self, item_id: int):
        url = f"{self.base_url}/api/entries/GetEntryView/{item_id}"
        try:
            response = await self.http_client.get(url)
            response.raise_for_status()
            return response.json()
        except (httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError): return None

    async def terminate(self):
        """插件终止时关闭HTTP客户端"""
        await self.http_client.aclose()
        logger.info("CnGal查询插件已卸载，HTTP客户端已关闭。")
