from astrbot.api.all import *
import astrbot.api.event.filter as filter
from astrbot.api.message_components import *
from datetime import datetime, timedelta
import random
import json
import aiohttp
import asyncio
import traceback
import time
from pathlib import Path
from urllib.parse import urlparse
from typing import Dict, List, Optional, Set, Tuple

# --------------- 路径配置 ---------------
PLUGIN_DIR = Path(__file__).parent
PAIR_DATA_PATH = PLUGIN_DIR / "pair_data.json"
COOLING_DATA_PATH = PLUGIN_DIR / "cooling_data.json"
BLOCKED_USERS_PATH = PLUGIN_DIR / "blocked_users.json"
BREAKUP_COUNT_PATH = PLUGIN_DIR / "breakup_counts.json"
ADVANCED_ENABLED_PATH = PLUGIN_DIR / "advanced_enabled.json"

# --------------- 数据结构 ---------------
class GroupMember:
    """群成员数据类"""
    def __init__(self, data: dict):
        self.user_id: str = str(data["user_id"])
        self.nickname: str = data["nickname"]
        self.card: str = data["card"]

    @property
    def display_info(self) -> str:
        """带QQ号的显示信息"""
        return f"{self.card or self.nickname}({self.user_id})"

# --------------- 插件主类 ---------------
@register("DailyWife", "jmt059", "每日老婆插件", "v0.7", "https://github.com/jmt059/DailyWife")
class DailyWifePlugin(Star):
    # 用于跟踪等待确认开启进阶功能的用户和会话信息
    ADVANCED_ENABLE_STATES: Dict[str, Dict[str, any]] = {}

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.pair_data = self._load_pair_data()
        self.cooling_data = self._load_cooling_data()
        self.blocked_users = self._load_blocked_users()
        self.advanced_enabled = self._load_data(ADVANCED_ENABLED_PATH, {})
        self._init_napcat_config()
        self._migrate_old_data()
        self._clean_invalid_cooling_records()
        self.breakup_counts = self._load_breakup_counts()

        # 存储进阶功能每日使用计数：{group_id: {user_id: {"wish": int, "rob": int, "lock": int}}}
        self.advanced_usage: Dict[str, Dict[str, Dict[str, int]]] = {}

        # 启动定时任务检查进阶功能开启是否超时
        asyncio.create_task(self._check_advanced_enable_timeout())

    # --------------- 数据迁移 ---------------
    def _migrate_old_data(self):
        try:
            if "block_list" in self.config:
                self.blocked_users = set(map(str, self.config["block_list"]))
                self._save_blocked_users()
                del self.config["block_list"]
            for group_id in list(self.pair_data.keys()):
                pairs = self.pair_data[group_id].get("pairs", {})
                for uid in pairs:
                    if "is_initiator" not in pairs[uid]:
                        pairs[uid]["is_initiator"] = True
                if isinstance(pairs, dict) and all(isinstance(v, str) for v in pairs.values()):
                    new_pairs = {}
                    for user_id, target_id in pairs.items():
                        new_pairs[user_id] = {
                            "user_id": target_id,
                            "display_name": f"未知用户({target_id})"
                        }
                        if target_id in pairs:
                            new_pairs[target_id] = {
                                "user_id": user_id,
                                "display_name": f"未知用户({user_id})"
                            }
                    self.pair_data[group_id]["pairs"] = new_pairs
                    self._save_pair_data()
        except Exception as e:
            print(f"数据迁移失败: {traceback.format_exc()}")

    # --------------- 初始化方法 ---------------
    def _init_napcat_config(self):
        try:
            self.napcat_host = self.config.get("napcat_host") or "127.0.0.1:3000"
            parsed = urlparse(f"http://{self.napcat_host}")
            if not parsed.hostname or not parsed.port:
                raise ValueError("无效的Napcat地址格式")
            self.napcat_hostname = parsed.hostname
            self.napcat_port = parsed.port
            self.timeout = self.config.get("request_timeout") or 10
        except Exception as e:
            raise RuntimeError(f"Napcat配置错误：{e}")

    # --------------- 数据管理 ---------------
    def _load_pair_data(self) -> Dict:
        try:
            if PAIR_DATA_PATH.exists():
                with open(PAIR_DATA_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            return {}
        except Exception as e:
            print(f"配对数据加载失败: {traceback.format_exc()}")
            return {}

    def _load_cooling_data(self) -> Dict:
        try:
            if COOLING_DATA_PATH.exists():
                with open(COOLING_DATA_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return { k: {"users": v["users"], "expire_time": datetime.fromisoformat(v["expire_time"])}
                             for k, v in data.items() }
            return {}
        except Exception as e:
            print(f"冷静期数据加载失败: {traceback.format_exc()}")
            return {}

    def _load_blocked_users(self) -> Set[str]:
        try:
            if BLOCKED_USERS_PATH.exists():
                with open(BLOCKED_USERS_PATH, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            return set()
        except Exception as e:
            print(f"屏蔽列表加载失败: {traceback.format_exc()}")
            return set()

    def _load_data(self, path: str, default=None):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except json.JSONDecodeError:
            print(f"JSON 文件 {path} 解码错误，已返回默认值。")
            return default
        except Exception as e:
            print(f"加载数据文件 {path} 失败: {traceback.format_exc()}")
            return default

    def _save_pair_data(self):
        try:
            if not PAIR_DATA_PATH.parent.exists():
                PAIR_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp_path = PAIR_DATA_PATH.with_suffix(".tmp")
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(self.pair_data, f, ensure_ascii=False, indent=2)
            temp_path.replace(PAIR_DATA_PATH)
        except Exception as e:
            print(f"保存配对数据失败: {traceback.format_exc()}")
            raise

    def _save_cooling_data(self):
        temp_data = { k: {"users": v["users"], "expire_time": v["expire_time"].isoformat()}
                      for k, v in self.cooling_data.items() }
        self._save_data(COOLING_DATA_PATH, temp_data)

    def _save_blocked_users(self):
        self._save_data(BLOCKED_USERS_PATH, list(self.blocked_users))

    def _save_data(self, path: Path, data: dict):
        try:
            temp_path = path.with_suffix(".tmp")
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            temp_path.replace(path)
        except Exception as e:
            print(f"数据保存失败: {traceback.format_exc()}")

    def _load_breakup_counts(self) -> Dict[str, Dict[str, int]]:
        try:
            if BREAKUP_COUNT_PATH.exists():
                with open(BREAKUP_COUNT_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return { date: {k: int(v) for k, v in counts.items()} for date, counts in data.items() }
            return {}
        except Exception as e:
            print(f"分手次数数据加载失败: {traceback.format_exc()}")
            return {}

    def _parse_display_info(self, raw_info: str) -> Tuple[str, str]:
        try:
            if '(' in raw_info and raw_info.endswith(')'):
                name_part, qq_part = raw_info.rsplit('(', 1)
                return name_part.strip(), qq_part[:-1]
            if '(' not in raw_info:
                return raw_info, "未知QQ号"
            parts = raw_info.split('(')
            if len(parts) >= 2:
                return parts[0].strip(), parts[-1].replace(')', '')
            return raw_info, "解析失败"
        except Exception as e:
            print(f"解析display_info失败：{raw_info} | 错误：{str(e)}")
            return raw_info, "解析异常"

    def _format_display_info(self, raw_info: str) -> str:
        nickname, qq = self._parse_display_info(raw_info)
        max_len = self.config.get("display_name_max_length", 10)
        safe_nickname = nickname.replace("\n", "").replace("\r", "").strip()
        formatted_nickname = safe_nickname[:max_len] + "……" if len(safe_nickname) > max_len else safe_nickname
        return f"{formatted_nickname}({qq})"

    # --------------- 命令处理器 ---------------
    @filter.command("重置")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def reset_command_handler(self, event: AstrMessageEvent):
        args = event.message_str.split()[1:]
        if not args:
            help_text = (
                "❌ 参数错误\n"
                "格式：重置 [群号/-选项]\n"
                "可用选项：\n"
                "-a → 全部数据\n"
                "-p → 配对数据\n"
                "-c → 冷静期\n"
                "-b → 屏蔽名单\n"
                "-d → 分手记录\n"
                "-e → 进阶功能（重置后当前群视为未开启进阶）"
            )
            yield event.plain_result(help_text)
            return
        arg = args[0]
        if arg == "-a":
            self.pair_data = {}
            self.cooling_data = {}
            self.blocked_users = set()
            self.breakup_counts = {}
            self.advanced_usage = {}
            self.advanced_enabled = {}
            self._save_all_data()
            yield event.plain_result("✅ 已重置所有数据")
        elif arg == "-e":
            group_id = str(event.message_obj.group_id)
            self.advanced_enabled.pop(group_id, None)
            yield event.plain_result("✅ 已重置本群进阶功能状态")
        elif arg.isdigit():
            group_id = str(arg)
            if group_id in self.pair_data:
                del self.pair_data[group_id]
                self._save_pair_data()
                yield event.plain_result(f"✅ 已重置群组 {group_id} 的配对数据")
            else:
                yield event.plain_result(f"⚠ 未找到群组 {group_id} 的记录")
        else:
            option_map = {
                "-p": ("配对数据", lambda: self._reset_pairs()),
                "-c": ("冷静期数据", lambda: self._reset_cooling()),
                "-b": ("屏蔽名单", lambda: self._reset_blocks()),
                "-d": ("分手记录", lambda: self._reset_breakups())
            }
            if arg not in option_map:
                yield event.plain_result("❌ 无效选项\n使用帮助查看可用选项")
                return
            opt_name, reset_func = option_map[arg]
            reset_func()
            yield event.plain_result(f"✅ 已重置 {opt_name}")

    def _reset_pairs(self):
        self.pair_data = {}
        self._save_pair_data()

    def _reset_cooling(self):
        self.cooling_data = {}
        self._save_cooling_data()

    def _reset_blocks(self):
        self.blocked_users = set()
        self._save_blocked_users()
        self.cooling_data = { k: v for k, v in self.cooling_data.items() if not k.startswith("block_") }
        self._save_cooling_data()

    def _reset_breakups(self):
        self.breakup_counts = {}
        self._save_data(BREAKUP_COUNT_PATH, self.breakup_counts)

    def _save_all_data(self):
        self._save_pair_data()
        self._save_cooling_data()
        self._save_blocked_users()
        self._save_data(BREAKUP_COUNT_PATH, self.breakup_counts)

    @filter.command("屏蔽")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def block_command_handler(self, event: AstrMessageEvent):
        parts = event.message_str.split()
        if len(parts) < 2 or not parts[1].isdigit():
            yield event.plain_result("❌ 参数错误\n格式：屏蔽 [QQ号]")
            return
        qq = parts[1]
        qq_str = str(qq)
        if qq_str in self.blocked_users:
            yield event.plain_result(f"ℹ️ 用户 {qq} 已在屏蔽列表中")
        else:
            self.blocked_users.add(qq_str)
            self._save_blocked_users()
            yield event.plain_result(f"✅ 已屏蔽用户 {qq}")

    @filter.command("冷静期")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cooling_command_handler(self, event: AstrMessageEvent):
        parts = event.message_str.split()
        if len(parts) < 2 or not parts[1].isdigit():
            yield event.plain_result("❌ 参数错误，格式：冷静期 [小时数]")
            return
        hours = int(parts[1])
        if not 1 <= hours <= 720:
            yield event.plain_result("❌ 无效时长（1-720小时）")
            return
        self.config["default_cooling_hours"] = hours
        yield event.plain_result(f"✅ 已设置默认冷静期时间为 {hours} 小时")

    # --------------- 核心功能 ---------------
    async def _get_members(self, group_id: str) -> Optional[List]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://{self.napcat_host}/get_group_member_list",
                    json={"group_id": group_id},
                    timeout=self.timeout
                ) as resp:
                    raw_response = await resp.text()
                    try:
                        data = json.loads(raw_response)
                    except json.JSONDecodeError:
                        print("API返回非JSON格式响应")
                        return None
                    if "data" not in data or not isinstance(data["data"], list):
                        print("API返回数据结构异常")
                        return None
                    valid_members = []
                    for m in data["data"]:
                        if "user_id" not in m:
                            continue
                        valid_members.append(GroupMember(m))
                    return valid_members
        except Exception as e:
            print(f"获取成员异常：{traceback.format_exc()}")
            return None

    def _check_reset(self, group_id: str):
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            if group_id not in self.pair_data or self.pair_data[group_id].get("date") != today:
                self.pair_data[group_id] = {"date": today, "pairs": {}, "used": []}
                self._save_pair_data()
        except Exception as e:
            print(f"重置检查失败: {traceback.format_exc()}")

    # --------------- 用户功能 ---------------
    @filter.regex(r"^今日老婆$")
    async def daily_wife_command(self, event: AstrMessageEvent):
        if not hasattr(event.message_obj, "group_id"):
            yield event.plain_result("此命令仅限群聊中使用。")
            return
        try:
            group_id = str(event.message_obj.group_id)
            user_id = event.get_sender_id()
            bot_id = event.message_obj.self_id
            self._check_reset(group_id)
            group_data = self.pair_data.get(group_id, {"date": datetime.now().strftime("%Y-%m-%d"), "pairs": {}, "used": []})

            # Check if the user is already in a pairing
            if user_id in group_data.get("pairs", {}):
                yield event.plain_result(f"🌸 你今天已经有伴侣啦，用查询命令查看详细哦~")
                return

            members = await self._get_members(int(group_id))
            if not members:
                yield event.plain_result("⚠️ 当前群组状态异常，请联系管理员")
                return
            valid_members = [m for m in members if m.user_id not in {user_id, bot_id}
                                            and m.user_id not in group_data["used"]
                                            and not self._is_in_cooling_period(user_id, m.user_id)]
            target = None
            for _ in range(5):
                if not valid_members:
                    break
                target = random.choice(valid_members)
                # Now we only check if the *target* is already in a pair
                if target.user_id not in group_data.get("pairs", {}):
                    break
                valid_members.remove(target)
                target = None
            if not target:
                yield event.plain_result("😢 暂时找不到合适的人选")
                return

            # Create a bidirectional pairing
            sender_display = self._format_display_info(f"{event.get_sender_name()}({user_id})")
            group_data["pairs"][user_id] = {"user_id": target.user_id, "display_name": target.display_info}
            group_data["pairs"][target.user_id] = {"user_id": user_id, "display_name": sender_display}
            if user_id not in group_data["used"]:
                group_data["used"].append(user_id)
            if target.user_id not in group_data["used"]:
                group_data["used"].append(target.user_id)
            self._save_pair_data()
            avatar_url = f"http://q.qlogo.cn/headimg_dl?dst_uin={target.user_id}&spec=640"
            target_display = self._format_display_info(target.display_info)
            yield event.chain_result([
                Plain(f"恭喜{sender_display}，\n"),
                Plain(f"▻ 成功娶到：{target_display}\n"),
                Plain("▻ 对方头像："),
                Image.fromURL(avatar_url),
                Plain("\n💎 好好对待TA哦，\n"),
                Plain("使用 /查询老婆 查看详细信息")
            ])
        except Exception as e:
            print(f"配对异常: {traceback.format_exc()}")
            yield event.plain_result("❌ 配对过程发生严重异常，请联系开发者")


    @filter.regex(r"^查询老婆$")
    async def query_handler(self, event: AstrMessageEvent):
        try:
            group_id = str(event.message_obj.group_id)
            user_id = event.get_sender_id()
            self._check_reset(group_id)
            group_data = self.pair_data.get(group_id, {})
            if user_id not in group_data.get("pairs", {}):
                yield event.plain_result("🌸 你还没有伴侣哦~")
                return
            partner_info = group_data["pairs"][user_id]
            avatar_url = f"http://q.qlogo.cn/headimg_dl?dst_uin={partner_info['user_id']}&spec=640"
            formatted_info = self._format_display_info(partner_info['display_name'])
            yield event.chain_result([Plain(f"💖 您的今日伴侣：{formatted_info}\n(请好好对待TA)"), Image.fromURL(avatar_url)])
        except Exception as e:
            print(f"查询异常: {traceback.format_exc()}")
            yield event.plain_result("❌ 查询过程发生异常")

    @filter.regex(r"^我要分手$")
    async def divorce_command(self, event: AstrMessageEvent):
        try:
            group_id = str(event.message_obj.group_id)
            user_id = event.get_sender_id()
            if group_id not in self.pair_data or user_id not in self.pair_data[group_id]["pairs"]:
                yield event.plain_result("🌸 您还没有伴侣哦~")
                return
            partner_info = self.pair_data[group_id]["pairs"][user_id]
            partner_id = partner_info["user_id"]
            today = datetime.now().strftime("%Y-%m-%d")
            user_counts = self.breakup_counts.get(today, {})
            current_count = user_counts.get(user_id, 0)
            if current_count >= self.config["max_daily_breakups"]:
                block_hours = self.config["breakup_block_hours"]
                expire_time = datetime.now() + timedelta(hours=block_hours)
                self.blocked_users.add(user_id)
                self.cooling_data[f"block_{user_id}"] = {"users": [user_id], "expire_time": expire_time}
                self._save_blocked_users()
                self._save_cooling_data()
                yield event.chain_result([Plain(f"⚠️ 检测到异常操作：\n▸ 今日已分手 {current_count} 次\n▸ 功能已临时禁用 {block_hours} 小时")])
                return

            # 删除双方的配对记录
            if user_id in self.pair_data[group_id]["pairs"]:
                del self.pair_data[group_id]["pairs"][user_id]
            if partner_id in self.pair_data[group_id]["pairs"] and self.pair_data[group_id]["pairs"][partner_id]["user_id"] == user_id:
                del self.pair_data[group_id]["pairs"][partner_id]

            group_data = self.pair_data[group_id]
            group_data["used"] = [uid for uid in group_data["used"] if uid != user_id and uid != partner_id]
            self._save_pair_data()
            cooling_key = f"{user_id}-{partner_id}"
            cooling_hours = self.config.get("default_cooling_hours", 48)
            self.cooling_data[cooling_key] = {"users": [user_id, partner_id], "expire_time": datetime.now() + timedelta(hours=cooling_hours)}
            self._save_cooling_data()
            yield event.chain_result([Plain(f"💔 您已解除与伴侣的关系\n⏳ {cooling_hours}小时内无法再匹配到一起")])
            user_counts[user_id] = current_count + 1
            self.breakup_counts[today] = user_counts
            self._save_data(BREAKUP_COUNT_PATH, self.breakup_counts)
        except Exception as e:
            print(f"分手异常: {traceback.format_exc()}")
            yield event.plain_result("❌ 分手操作异常")

    # --------------- 进阶功能（进阶功能） ---------------
    @filter.command("开启老婆插件进阶功能")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def enable_advanced_command(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = event.get_sender_id()
        if self.advanced_enabled.get(group_id, False):
            yield event.plain_result("进阶功能已开启。")
            return
        # 记录用户ID和会话信息
        DailyWifePlugin.ADVANCED_ENABLE_STATES[user_id] = {"session": event.session, "timestamp": time.time()}
        yield event.plain_result("请在30秒内发送确认命令：我已知晓进阶功能带来的潜在风险并且执意开启")

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def confirm_enable_advanced(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        group_id = str(event.message_obj.group_id)
        if user_id in DailyWifePlugin.ADVANCED_ENABLE_STATES and event.message_str.strip() == "我已知晓进阶功能带来的潜在风险并且执意开启":
            del DailyWifePlugin.ADVANCED_ENABLE_STATES[user_id]
            self.advanced_enabled[group_id] = True
            self._save_data(ADVANCED_ENABLED_PATH, self.advanced_enabled)
            yield event.plain_result("进阶功能已开启，该群现已启用进阶功能。")

    @filter.command("关闭进阶老婆插件功能")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def disable_advanced_command(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        self.advanced_enabled[group_id] = False
        self._save_data(ADVANCED_ENABLED_PATH, self.advanced_enabled)
        yield event.plain_result("进阶功能已关闭，该群已禁用进阶功能。")

    def _init_advanced_usage(self, group_id: str, user_id: str):
        if group_id not in self.advanced_usage:
            self.advanced_usage[group_id] = {}
        if user_id not in self.advanced_usage[group_id]:
            self.advanced_usage[group_id][user_id] = {"wish": 0, "rob": 0, "lock": 0}

    @filter.command("许愿")
    async def wish_command(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = event.get_sender_id()
        if not self.advanced_enabled.get(group_id, False):
            yield event.plain_result("进阶功能未开启，该群无法使用许愿功能。")
            return
        parts = event.message_str.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 参数错误：请指定许愿对象（仅支持命令+QQ号）。")
            return
        target_input = parts[1]
        target_qq = target_input[1:] if target_input.startswith("@") else target_input

        if user_id == target_qq:
            yield event.plain_result("❌ 无法对自己使用许愿功能。")
            return

        self._init_advanced_usage(group_id, user_id)
        if self.advanced_usage[group_id][user_id]["wish"] >= self.config.get("max_daily_wishes", 1):
            yield event.plain_result("❌ 今日许愿次数已用完。")
            return

        if group_id not in self.pair_data:
            self.pair_data[group_id] = {"date": datetime.now().strftime("%Y-%m-%d"), "pairs": {}, "used": []}
        group_data = self.pair_data[group_id]

        if user_id in group_data["pairs"]:
            yield event.plain_result("你已经有伴侣了……许愿将不可用")
            return

        try:
            async with aiohttp.ClientSession() as session:
                napcat_url = f"http://{self.napcat_host}/get_group_member_info"
                payload = {
                    "group_id": group_id,
                    "user_id": target_qq,
                    "no_cache": False
                }
                async with session.post(napcat_url, json=payload, timeout=self.timeout) as resp:
                    response_data = await resp.json()
                    if response_data.get("status") == "failed" and "用户ID" in response_data.get("message", "") and "不存在" in response_data.get("message", ""):
                        yield event.plain_result("群内似乎没这个人呢……")
                        return
                    elif response_data.get("status") == "ok" and "data" in response_data:
                        target_nickname = response_data["data"].get("nickname", f"未知用户({target_qq})")
                        sender_nickname = event.get_sender_name()
                        group_data["pairs"][user_id] = {"user_id": target_qq, "display_name": f"{target_nickname}({target_qq})"}
                        group_data["pairs"][target_qq] = {"user_id": user_id, "display_name": f"{sender_nickname}({user_id})"}
                        if user_id not in group_data["used"]:
                            group_data["used"].append(user_id)
                        if target_qq not in group_data["used"]:
                            group_data["used"].append(target_qq)
                        self._save_pair_data()
                        self.advanced_usage[group_id][user_id]["wish"] += 1
                        yield event.plain_result("许愿成功，系统已为你指定伴侣。")
                    else:
                        print(f"Napcat API 错误 (许愿): {response_data}")
                        yield event.plain_result("许愿失败：Napcat API 返回异常。")

        except aiohttp.ClientError as e:
            print(f"连接 Napcat API 失败 (许愿): {e}")
            yield event.plain_result("许愿失败：无法连接到 Napcat 服务。")
        except asyncio.TimeoutError:
            yield event.plain_result("许愿失败：连接 Napcat API 超时。")
        except Exception as e:
            print(f"许愿异常: {traceback.format_exc()}")
            yield event.plain_result("❌ 许愿过程发生异常。")

    @filter.command("强娶")
    async def rob_command(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = event.get_sender_id()
        if not self.advanced_enabled.get(group_id, False):
            yield event.plain_result("进阶功能未开启，该群无法使用强娶功能。")
            return
        parts = event.message_str.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 参数错误：请指定强娶对象（仅支持命令+QQ号）。")
            return
        target_input = parts[1]
        target_qq = target_input[1:] if target_input.startswith("@") else target_input

        if user_id == target_qq:
            yield event.plain_result("❌ 无法对自己使用强娶功能。")
            return

        self._init_advanced_usage(group_id, user_id)
        if self.advanced_usage[group_id][user_id]["rob"] >= self.config.get("max_daily_rob_attempts", 2):
            yield event.plain_result("❌ 今日强娶次数已用完。")
            return

        if group_id not in self.pair_data:
            self.pair_data[group_id] = {"date": datetime.now().strftime("%Y-%m-%d"), "pairs": {}, "used": []}
        group_data = self.pair_data[group_id]

        if user_id in group_data["pairs"]:
            yield event.plain_result("你已经有伴侣了……强娶将不可用")
            return

        try:
            async with aiohttp.ClientSession() as session:
                napcat_url = f"http://{self.napcat_host}/get_group_member_info"
                payload = {
                    "group_id": group_id,
                    "user_id": target_qq,
                    "no_cache": False
                }
                async with session.post(napcat_url, json=payload, timeout=self.timeout) as resp:
                    response_data = await resp.json()
                    if response_data.get("status") == "failed" and "用户ID" in response_data.get("message", "") and "不存在" in response_data.get("message", ""):
                        yield event.plain_result("群内似乎没这个人呢……")
                        return
                    elif response_data.get("status") == "ok" and "data" in response_data:
                        target_nickname = response_data["data"].get("nickname", f"未知用户({target_qq})")
                        if target_qq not in group_data["pairs"]:
                            yield event.plain_result("强娶失败：目标当前没有伴侣，请改用许愿命令。")
                            return
                        target_pair = group_data["pairs"][target_qq]
                        if target_pair.get("locked", False):
                            yield event.plain_result("强娶失败：目标伴侣处于锁定状态。")
                            return
                        partner_id = target_pair["user_id"]
                        partner_pair = group_data["pairs"].get(partner_id, {})
                        if partner_pair.get("locked", False):
                            yield event.plain_result("强娶失败：目标伴侣处于锁定状态。")
                            return

                        # 删除被抢夺者及其原配偶的双向记录
                        if target_qq in group_data["pairs"]:
                            original_partner_id = group_data["pairs"][target_qq]["user_id"]
                            del group_data["pairs"][target_qq]
                            if original_partner_id in group_data["pairs"] and group_data["pairs"][original_partner_id]["user_id"] == target_qq:
                                del group_data["pairs"][original_partner_id]

                        sender_nickname = event.get_sender_name()
                        group_data["pairs"][user_id] = {"user_id": target_qq, "display_name": f"{target_nickname}({target_qq})"}
                        group_data["pairs"][target_qq] = {"user_id": user_id, "display_name": f"{sender_nickname}({user_id})"}
                        if user_id not in group_data["used"]:
                            group_data["used"].append(user_id)
                        if target_qq not in group_data["used"]:
                            group_data["used"].append(target_qq)
                        self._save_pair_data()
                        self.advanced_usage[group_id][user_id]["rob"] += 1
                        yield event.plain_result("强娶成功，你已成功抢得伴侣。")
                    else:
                        print(f"Napcat API 错误 (强娶): {response_data}")
                        yield event.plain_result("强娶失败：Napcat API 返回异常。")

        except aiohttp.ClientError as e:
            print(f"连接 Napcat API 失败 (强娶): {e}")
            yield event.plain_result("强娶失败：无法连接到 Napcat 服务。")
        except asyncio.TimeoutError:
            yield event.plain_result("强娶失败：连接 Napcat API 超时。")
        except Exception as e:
            print(f"强娶异常: {traceback.format_exc()}")
            yield event.plain_result("❌ 强娶过程发生异常。")

    @filter.command("锁定")
    async def lock_command(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        if not self.advanced_enabled.get(group_id, False):
            yield event.plain_result("进阶功能未开启，该群无法使用锁定功能。")
            return
        user_id = event.get_sender_id()
        self._init_advanced_usage(group_id, user_id)
        if self.advanced_usage[group_id][user_id]["lock"] >= self.config.get("max_daily_lock", 1):
            yield event.plain_result("❌ 今日锁定次数已用完。")
            return
        group_data = self.pair_data.get(group_id, {"pairs": {}, "used": []})
        if user_id not in group_data["pairs"]:
            yield event.plain_result("锁定失败：你当前没有伴侣。")
            return
        pair_info = group_data["pairs"][user_id]
        if pair_info.get("is_initiator", False):
            yield event.plain_result("锁定失败：只有被抽方才能锁定。")
            return
        partner_id = pair_info["user_id"]
        group_data["pairs"][user_id]["locked"] = True
        if partner_id in group_data["pairs"]:
            group_data["pairs"][partner_id]["locked"] = True
        self.pair_data[group_id] = group_data
        self._save_pair_data()
        self.advanced_usage[group_id][user_id]["lock"] += 1
        yield event.plain_result("锁定成功，你与伴侣已被锁定，强娶将无法进行。")

    # 异步定时任务检查进阶功能开启是否超时
    async def _check_advanced_enable_timeout(self):
        while True:
            await asyncio.sleep(5)  # 每隔5秒检查一次
            now = time.time()
            expired_users = []
            for user_id, state in DailyWifePlugin.ADVANCED_ENABLE_STATES.items():
                if now - state["timestamp"] > 30:
                    expired_users.append(user_id)
                    # 发送超时消息
                    await self.context.send_message(state["session"], MessageChain([Plain("开启进阶功能超时了哦~")]))

            # 移除超时的用户状态
            for user_id in expired_users:
                if user_id in DailyWifePlugin.ADVANCED_ENABLE_STATES:
                    del DailyWifePlugin.ADVANCED_ENABLE_STATES[user_id]

    # --------------- 辅助功能 ---------------
    def _clean_invalid_cooling_records(self):
        try:
            now = datetime.now()
            expired_keys = [ k for k, v in self.cooling_data.items() if v["expire_time"] < now ]
            for k in expired_keys:
                del self.cooling_data[k]
            if expired_keys:
                self._save_cooling_data()
        except Exception as e:
            print(f"清理冷静期数据失败: {traceback.format_exc()}")

    def _is_in_cooling_period(self, user1: str, user2: str) -> bool:
        return any({user1, user2} == set(pair["users"]) and datetime.now() < pair["expire_time"]
                   for pair in self.cooling_data.values())

    # --------------- 动态菜单 ---------------
    @filter.command("老婆菜单")
    async def menu_handler(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        is_admin = event.is_admin()  # 判断管理员身份
        adv_enabled = self.advanced_enabled.get(group_id, False)
        # 基础菜单
        base_menu = (
            "【老婆插件使用说明】\n\n"
            "🌸 基础功能(更新为正则触发)：\n"
            "今日老婆 - 随机配对CP\n"
            "查询老婆 - 查询当前CP\n"
            "我要分手 - 解除当前CP关系\n\n"
        )
        # 当前配置显示
        config_menu = (
            f"当前配置：\n"
            f"▸ 每日最大分手次数：{self.config.get('max_daily_breakups',3)}\n"
            f"▸ 超限屏蔽时长：{self.config.get('breakup_block_hours',24)}小时\n"
            f"▸ 解除关系后需间隔 {self.config.get('default_cooling_hours',48)} 小时才能再次匹配\n"
            f"▸ 每日许愿次数：{self.config.get('max_daily_wishes',1)}\n"
            f"▸ 每日强娶次数：{self.config.get('max_daily_rob_attempts',2)}\n"
            f"▸ 每日锁定次数：{self.config.get('max_daily_lock',1)}"
        )
        # 根据是否启用进阶功能构造菜单：
        if not adv_enabled:
            if is_admin:
                admin_menu = (
                    "⚙️ 管理员命令：\n"
                    "/重置 -a → 全部数据\n"
                    "/重置 [群号] → 指定群配对数据\n"
                    "/重置 -p → 配对数据\n"
                    "/重置 -c → 冷静期数据\n"
                    "/重置 -b → 屏蔽名单及相关冷静期\n"
                    "/重置 -d → 分手记录\n"
                    "/重置 -e → 进阶功能状态重置\n"
                    "/屏蔽 [QQ号] - 屏蔽指定用户\n"
                    "/冷静期 [小时] - 设置冷静期时长\n"
                    "/开启老婆插件进阶功能\n\n"
                )
            else:
                admin_menu = ""
            menu_text = base_menu + admin_menu + config_menu
        else:
            adv_menu = (
                "⚠️ 进阶命令(带唤醒前缀! QQ号前带空格!)：\n"
                "/许愿 [QQ号] - 每日限1次（指定伴侣）\n"
                "/强娶 [QQ号] - 每日限2次（抢夺他人伴侣）\n"
                "/锁定 - 每日限1次（被抽方锁定伴侣，防止强娶）\n\n"
            )
            if is_admin:
                admin_menu = (
                    "⚙️ 管理员命令：\n"
                    "/重置 -a → 全部数据\n"
                    "/重置 [群号] → 指定群配对数据\n"
                    "/重置 -p → 配对数据\n"
                    "/重置 -c → 冷静期数据\n"
                    "/重置 -b → 屏蔽名单及相关冷静期\n"
                    "/重置 -d → 分手记录\n"
                    "/重置 -e → 进阶功能状态重置\n"
                    "/屏蔽 [QQ号] - 屏蔽指定用户\n"
                    "/冷静期 [小时] - 设置冷静期时长\n"
                    "/关闭进阶老婆插件功能\n\n"
                )
                menu_text = base_menu + adv_menu + admin_menu + config_menu
            else:
                menu_text = base_menu + adv_menu + config_menu
        yield event.chain_result([Plain(menu_text.strip())])

    # --------------- 定时任务 ---------------
    async def _daily_reset_task(self):
        while True:
            now = datetime.now()
            next_day = now + timedelta(days=1)
            reset_time = datetime(next_day.year, next_day.month, next_day.day, 0, 0, 5)
            wait_seconds = (reset_time - now).total_seconds()
            await asyncio.sleep(wait_seconds)
            try:
                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                if yesterday in self.breakup_counts:
                    del self.breakup_counts[yesterday]
                    self._save_data(BREAKUP_COUNT_PATH, self.breakup_counts)
                now = datetime.now()
                self.cooling_data = { k: v for k, v in self.cooling_data.items() if not (k.startswith("block_") and v["expire_time"] < now) }
                self._save_cooling_data()
                self._clean_invalid_cooling_records()
                self.advanced_usage = {}
            except Exception as e:
                print(f"定时任务失败: {traceback.format_exc()}")

    # 插件被禁用、重载或关闭时触发
    async def terminate(self):
        """
        此处实现你的对应逻辑, 例如销毁, 释放某些资源, 回滚某些修改。
        """
        pass