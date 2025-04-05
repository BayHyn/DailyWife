from astrbot.api.all import *
import astrbot.api.event.filter as filter
from datetime import datetime, timedelta
import random
import json
import aiohttp
import asyncio
import logging
import traceback
from pathlib import Path
from urllib.parse import urlparse
from typing import Dict, List, Optional, Set, Tuple

# --------------- 路径配置 ---------------
PLUGIN_DIR = Path(__file__).parent
PAIR_DATA_PATH = PLUGIN_DIR / "pair_data.json"
COOLING_DATA_PATH = PLUGIN_DIR / "cooling_data.json"
BLOCKED_USERS_PATH = PLUGIN_DIR / "blocked_users.json"
BREAKUP_COUNT_PATH = PLUGIN_DIR / "breakup_counts.json"

# --------------- 日志配置 ---------------
logger = logging.getLogger("DailyWife")

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
@register("DailyWife", "jmt059", "每日老婆插件", "v0.4", "https://github.com/jmt059/DailyWife")
class DailyWifePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.pair_data = self._load_pair_data()
        self.cooling_data = self._load_cooling_data()
        self.blocked_users = self._load_blocked_users()
        self._init_napcat_config()
        self._migrate_old_data()
        self._clean_invalid_cooling_records()
        self.breakup_counts = self._load_breakup_counts()
    

    # --------------- 数据迁移 ---------------
    def _migrate_old_data(self):
        """数据格式迁移"""
        try:
            # 迁移旧版屏蔽数据（v3.0.x -> v3.1.x）
            if "block_list" in self.config:
                self.blocked_users = set(map(str, self.config["block_list"]))
                self._save_blocked_users()
                del self.config["block_list"]
            
            # 迁移配对数据格式（v2.x -> v3.x）
            for group_id in list(self.pair_data.keys()):
                pairs = self.pair_data[group_id].get("pairs", {})
            for uid in pairs:
                if "is_initiator" not in pairs[uid]:
                    pairs[uid]["is_initiator"] = (uid == user_id)  # 旧数据默认发起者为抽方
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
            logger.error(f"数据迁移失败: {traceback.format_exc()}")

    # --------------- 初始化方法 ---------------
    def _init_napcat_config(self):
        """初始化Napcat连接配置"""
        try:
            self.napcat_host = self.config.get("napcat_host") or "127.0.0.1:3000"
            parsed = urlparse(f"http://{self.napcat_host}")
            if not parsed.hostname or not parsed.port:
                raise ValueError("无效的Napcat地址格式")
            self.napcat_hostname = parsed.hostname
            self.napcat_port = parsed.port
            self.timeout = self.config.get("request_timeout") or 10
        except Exception as e:
            logger.error(f"Napcat配置错误: {traceback.format_exc()}")
            raise RuntimeError("Napcat配置初始化失败")

    # --------------- 数据管理 ---------------
    def _load_pair_data(self) -> Dict:
        """加载配对数据"""
        try:
            if PAIR_DATA_PATH.exists():
                with open(PAIR_DATA_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"配对数据加载失败: {traceback.format_exc()}")
            return {}

    def _load_cooling_data(self) -> Dict:
        """加载冷静期数据"""
        try:
            if COOLING_DATA_PATH.exists():
                with open(COOLING_DATA_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return {
                        k: {
                            "users": v["users"],
                            "expire_time": datetime.fromisoformat(v["expire_time"])
                        } for k, v in data.items()
                    }
            return {}
        except Exception as e:
            logger.error(f"冷静期数据加载失败: {traceback.format_exc()}")
            return {}

    def _load_blocked_users(self) -> Set[str]:
        """加载屏蔽用户列表"""
        try:
            if BLOCKED_USERS_PATH.exists():
                with open(BLOCKED_USERS_PATH, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            return set()
        except Exception as e:
            logger.error(f"屏蔽列表加载失败: {traceback.format_exc()}")
            return set()

    def _save_pair_data(self):
        """安全保存配对数据"""
        self._save_data(PAIR_DATA_PATH, self.pair_data)

    def _save_cooling_data(self):
        """安全保存冷静期数据"""
        temp_data = {
            k: {
                "users": v["users"],
                "expire_time": v["expire_time"].isoformat()
            } for k, v in self.cooling_data.items()
        }
        self._save_data(COOLING_DATA_PATH, temp_data)

    def _save_blocked_users(self):
        """保存屏蔽用户列表"""
        self._save_data(BLOCKED_USERS_PATH, list(self.blocked_users))

    def _save_data(self, path: Path, data: dict):
        """通用保存方法"""
        try:
            temp_path = path.with_suffix(".tmp")
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            temp_path.replace(path)
        except Exception as e:
            logger.error(f"数据保存失败: {traceback.format_exc()}")
        
    def _load_breakup_counts(self) -> Dict[str, Dict[str, int]]:
        """加载分手次数数据"""
        try:
            if BREAKUP_COUNT_PATH.exists():
                with open(BREAKUP_COUNT_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return {
                        date: {k: int(v) for k, v in counts.items()}
                        for date, counts in data.items()
                    }
            return {}
        except Exception as e:
            logger.error(f"分手次数数据加载失败: {traceback.format_exc()}")
            return {}        

    # --------------- 管理员验证 ---------------
    def _is_admin(self, user_id: str) -> bool:
        """验证管理员权限"""
        admin_list = self.config.get("admin_list", [])
        return str(user_id) in map(str, admin_list)

    # --------------- 命令处理器 ---------------
    @filter.command("重置")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def reset_command_handler(self, event: AstrMessageEvent):
        args = event.message_str.split()[1:]
        if not args:
            yield event.chain_result([
                Plain("❌ 参数错误\n"),
                Plain("格式：重置 [群号/-选项]\n"),
                Plain("可用选项：\n"),
                Plain("-a → 全部数据，\n"),
                Plain("-p → 配对数据，\n"),
                Plain("-c → 冷静期，\n"),
                Plain("-b → 屏蔽名单，\n"),
                Plain("-d → 分手记录")
            ])
            return

        arg = args[0]
        
        # 全部重置
        if arg == "-a":
            self.pair_data = {}
            self.cooling_data = {}
            self.blocked_users = set()
            self.breakup_counts = {}
            self._save_all_data()
            yield event.plain_result("✅ 已重置所有数据（配对/冷静期/屏蔽/分手记录）")

        # 按群号重置
        elif arg.isdigit():
            group_id = str(arg)
            if group_id in self.pair_data:
                del self.pair_data[group_id]
                self._save_pair_data()
                yield event.plain_result(f"✅ 已重置群组 {group_id} 的配对数据")
            else:
                yield event.plain_result(f"⚠ 未找到群组 {group_id} 的记录")

        # 选项重置
        else:
            option_map = {
                "-p": ("pairs", "配对", lambda: self._reset_pairs()),
                "-c": ("cooling", "冷静期", lambda: self._reset_cooling()),
                "-b": ("blocks", "屏蔽名单", lambda: self._reset_blocks()),
                "-d": ("breakups", "分手记录", lambda: self._reset_breakups())
            }
            
            if arg not in option_map:
                yield event.plain_result("❌ 无效选项\n使用帮助查看可用选项")
                return

            opt_name, reset_func = option_map[arg]

            reset_func()
            yield event.plain_result(f"✅ 已重置 {opt_name} 数据")

    def _reset_pairs(self):
        self.pair_data = {}
        self._save_pair_data()

    def _reset_cooling(self):
        self.cooling_data = {}
        self._save_cooling_data()

    def _reset_blocks(self):
        self.blocked_users = set()
        self._save_blocked_users()
        # 同时清理相关冷静期记录
        self.cooling_data = {
            k:v for k,v in self.cooling_data.items() 
            if not k.startswith("block_")
        }
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
        """完整的屏蔽命令处理器"""

        qq = event.message_str.split()[1] if len(event.message_str.split()) > 1 else None
        if not qq or not qq.isdigit():
            yield event.plain_result("❌ 参数错误\n格式：屏蔽 [QQ号]")
            return

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
        """完整的冷静期命令处理器"""

        args = event.message_str.split()
        if len(args) < 2 or not args[1].isdigit():
            yield event.plain_result("❌ 参数错误\n格式：冷静期 [小时数]")
            return

        hours = int(args[1])
        if not 1 <= hours <= 720:
            yield event.plain_result("❌ 无效时长（1-720小时）")
            return

        self.config["default_cooling_hours"] = hours
        yield event.plain_result(f"✅ 已设置默认冷静期时间为 {hours} 小时")

    # --------------- 核心功能 ---------------
    async def _get_members(self, group_id: int) -> Optional[List[GroupMember]]:
        """获取有效群成员"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://{self.napcat_host}/get_group_member_list",
                    json={"group_id": group_id},
                    timeout=self.timeout
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"HTTP状态码异常: {resp.status}")
                        return None
                    
                    data = await resp.json()
                    if data["status"] != "ok":
                        logger.error(f"API返回状态异常: {data}")
                        return None
                    
                    return [
                        GroupMember(m) for m in data["data"]
                        if str(m["user_id"]) not in self.blocked_users
                    ]
        except Exception as e:
            logger.error(f"获取群成员失败: {traceback.format_exc()}")
            return None

    def _check_reset(self, group_id: str):
        """每日重置检查"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            if group_id not in self.pair_data or self.pair_data[group_id].get("date") != today:
                self.pair_data[group_id] = {
                    "date": today,
                    "pairs": {},
                    "used": []
                }
                self._save_pair_data()
        except Exception as e:
            logger.error(f"重置检查失败: {traceback.format_exc()}")

    # --------------- 用户功能 ---------------
    @filter.command("今日老婆")
    async def pair_handler(self, event: AstrMessageEvent):
        """配对功能"""
        try:
            if not hasattr(event.message_obj, "group_id"):
                return

            group_id = str(event.message_obj.group_id)
            user_id = event.get_sender_id()
            bot_id = event.message_obj.self_id
            
            self._check_reset(group_id)
            group_data = self.pair_data[group_id]

            if user_id in group_data["pairs"]:
                # 获取角色信息
                is_initiator = group_data["pairs"][user_id].get("is_initiator", False)

                if is_initiator:
                    # 抽方专属回复
                    reply = [
                        Plain("👑【喜报】\n"),
                        Plain(f"▸ 已迎娶老婆：{group_data['pairs'][user_id]['display_name']}\n"),
                        Plain(f"▸ 特权有效期：至今日24点"),
                    ]
                else:
                    # 被抽方专属回复
                    reply = [
                        Plain("🎁【恭喜】\n"),
                        Plain(f"✦ 您被 {group_data['pairs'][user_id]['display_name']} 选为老婆\n"),
                        Plain(f"✦ 有效期：至今日24点"),
                    ]
                
                yield event.chain_result(reply)
                return


            members = await self._get_members(int(group_id))
            if not members:
                yield event.plain_result("🔧 服务暂时不可用，请稍后再试")
                return

            valid_members = [
                m for m in members
                if m.user_id not in {user_id, bot_id}
                and m.user_id not in group_data["used"]
                and not self._is_in_cooling_period(user_id, m.user_id)
            ]
            
            target = None
            for _ in range(5):
                if not valid_members:
                    break
                target = random.choice(valid_members)
                if target.user_id not in group_data["pairs"]:
                    break
                valid_members.remove(target)
                target = None
            
            if not target:
                yield event.plain_result("😢 暂时找不到合适的人选")
                return

            group_data["pairs"][user_id] = {
                "user_id": target.user_id,
                "display_name": target.display_info,
                "is_initiator": True  # 标记抽方
            }
            group_data["pairs"][target.user_id] = {
                "user_id": user_id,
                "display_name": f"{event.get_sender_name()}({user_id})",
                "is_initiator": False  # 标记被抽方
            }
            group_data["used"].extend([user_id, target.user_id])
            self._save_pair_data()

            avatar_url = f"http://q.qlogo.cn/headimg_dl?dst_uin={target.user_id}&spec=640"
            # 给抽方的提示（在未配对时首次发送命令的人） (is_initiator=True)
            yield event.chain_result([
                Plain(f"【恭喜{event.get_sender_name()}({user_id})🎯娶老婆成功】\n"),
                Plain(f"▻ 成功娶到：{target.display_info}\n"),
                Plain(f"▻ 对方头像："),
                Image.fromURL(avatar_url),
                Plain(f"\n💎 好好对待TA哦"),
                Plain(f"\n使用 查询老婆 查看详细信息")
            ])

        except Exception as e:
            logger.error(f"配对失败: {traceback.format_exc()}")
            yield event.plain_result("❌ 配对过程发生异常")

    # ================== 修复后的查询老婆命令 ==================
    @filter.command("查询老婆")
    async def query_handler(self, event: AstrMessageEvent):
        """查询伴侣"""
        try:
            group_id = str(event.message_obj.group_id)
            user_id = event.get_sender_id()
            
            self._check_reset(group_id)
            group_data = self.pair_data.get(group_id, {})

            # 先检查是否存在CP关系
            if user_id not in group_data.get("pairs", {}):
                yield event.plain_result("🌸 你还没有伴侣哦~")
                return

            target_info = group_data["pairs"][user_id]
            avatar_url = f"http://q.qlogo.cn/headimg_dl?dst_uin={target_info['user_id']}&spec=640"

            # 角色判断逻辑
            if target_info.get("is_initiator", False):
                role_desc = "👑 您的今日老婆"
                footer = "\n(请好好对待TA)"
            else:
                role_desc = "💖 您的今日老公"
                footer = "\n(请好好对待TA)"
                
            yield event.chain_result([
                Plain(f"{role_desc}：{target_info['display_name']}{footer}"),
                At(qq=target_info["user_id"]),
                Image.fromURL(avatar_url)
            ])

        except Exception as e:
            logger.error(f"查询失败: {traceback.format_exc()}")
            yield event.plain_result("❌ 查询过程发生异常")

    # ================== 修复后的分手命令 ==================  
    @filter.command("我要分手")
    async def breakup_handler(self, event: AstrMessageEvent):
        """解除伴侣关系"""
        try:
            group_id = str(event.message_obj.group_id)
            user_id = event.get_sender_id()
            
            if group_id not in self.pair_data or user_id not in self.pair_data[group_id]["pairs"]:
                yield event.plain_result("🌸 您还没有伴侣哦~")
                return
                
            target_info = self.pair_data[group_id]["pairs"][user_id]
            target_id = target_info["user_id"]
            is_initiator = target_info.get("is_initiator", False)  # 先获取身份信息
            today = datetime.now().strftime("%Y-%m-%d")
            user_counts = self.breakup_counts.get(today, {})
            current_count = user_counts.get(user_id, 0)

            if current_count >= self.config["max_daily_breakups"]:
                # 自动屏蔽逻辑
                block_hours = self.config["breakup_block_hours"]
                expire_time = datetime.now() + timedelta(hours=block_hours)
                
                self.blocked_users.add(user_id)
                self.cooling_data[f"block_{user_id}"] = {
                    "users": [user_id],
                    "expire_time": expire_time
                }
                
                self._save_blocked_users()
                self._save_cooling_data()
                
                yield event.chain_result([
                    Plain("⚠️ 检测到异常操作：\n"),
                    Plain(f"▸ 今日已分手 {current_count} 次\n"),
                    Plain(f"▸ 功能已临时禁用 {block_hours} 小时")
                ])
                return

            # 删除配对数据
            del self.pair_data[group_id]["pairs"][user_id]
            del self.pair_data[group_id]["pairs"][target_id]
            self.pair_data[group_id]["used"] = [uid for uid in self.pair_data[group_id]["used"] if uid not in {user_id, target_id}]
            self._save_pair_data()
            
            # 设置冷静期
            cooling_key = f"{user_id}-{target_id}"
            cooling_hours = self.config.get("default_cooling_hours", 48)
            self.cooling_data[cooling_key] = {
                "users": [user_id, target_id],
                "expire_time": datetime.now() + timedelta(hours=cooling_hours)
            }
            self._save_cooling_data()

            # 根据身份生成不同提示
            if is_initiator:    # 抽方
                action = "主动解除与老婆的关系"
                penalty = "将失去老公身份"
            else:   # 被抽方
                action = "主动解除与老公的关系"
                penalty = "将失去老婆身份"
                
            yield event.chain_result([
                Plain(f"💔 您{action}\n⚠️ {penalty}"),
                Plain(f"\n⏳ {cooling_hours}小时内无法再匹配到一起")
            ])
            user_counts[user_id] = current_count + 1
            self.breakup_counts[today] = user_counts
            self._save_data(BREAKUP_COUNT_PATH, self.breakup_counts)     
                   
        except Exception as e:
            logger.error(f"分手操作失败: {traceback.format_exc()}")
            yield event.plain_result("❌ 分手操作异常")

    # --------------- 辅助功能 ---------------
    def _clean_invalid_cooling_records(self):
        """每日清理过期的冷静期记录"""
        try:
            now = datetime.now()
            expired_keys = [
                k for k, v in self.cooling_data.items()
                if v["expire_time"] < now
            ]
            for k in expired_keys:
                del self.cooling_data[k]
            if expired_keys:
                self._save_cooling_data()
                logger.info(f"已清理 {len(expired_keys)} 条过期冷静期记录")
        except Exception as e:
            logger.error(f"清理冷静期数据失败: {traceback.format_exc()}")

    def _is_in_cooling_period(self, user1: str, user2: str) -> bool:
        """检查是否在冷静期"""
        cooling_hours = self.config.get("default_cooling_hours", 48)
        return any(
            {user1, user2} == set(pair["users"]) and 
            datetime.now() < pair["expire_time"]
            for pair in self.cooling_data.values()
        )

    # --------------- 帮助信息 ---------------
    @filter.command("老婆帮帮我")  # 改为更直观的中文命令
    async def help_handler(self, event: AstrMessageEvent):
        """帮助信息"""
        help_msg = f"""
        【老婆插件使用说明】
        🌸 基础功能：
        /今日老婆 - 随机配对CP
        /查询老婆 - 查询当前CP
        /我要分手 - 解除当前CP关系
        
        ⚙️ 管理员命令：
        /重置 -a → 全部数据（配对/冷静期/屏蔽/分手记录）
        /重置 [群号] → 指定群配对数据
        /重置 -p → 所有群配对数据
        /重置 -c → 冷静期数据
        /重置 -b → 屏蔽名单及相关冷静期
        /重置 -d → 分手次数记录
        /屏蔽 [QQ号]  - 屏蔽指定用户
        /冷静期 [小时] - 设置冷静期时长
        
        当前配置：
        ▸ 每日最大分手次数：{self.config['max_daily_breakups']}
        ▸ 超限屏蔽时长：{self.config['breakup_block_hours']}小时
        ▸ 解除关系后需间隔 {self.config.get('default_cooling_hours', 48)} 小时才能再次匹配
        """
        yield event.chain_result([Plain(help_msg.strip())])

    # --------------- 定时任务 ---------------
    async def _daily_reset_task(self):
        """每日定时任务"""
        while True:
            now = datetime.now()
            next_day = now + timedelta(days=1)
            reset_time = datetime(next_day.year, next_day.month, next_day.day, 0, 0, 5)
            wait_seconds = (reset_time - now).total_seconds()
            
            await asyncio.sleep(wait_seconds)
            try:
                # 清除昨日数据
                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                if yesterday in self.breakup_counts:
                    del self.breakup_counts[yesterday]
                    self._save_data(BREAKUP_COUNT_PATH, self.breakup_counts)
                    
                # 清理过期屏蔽
                now = datetime.now()
                self.cooling_data = {
                    k:v for k,v in self.cooling_data.items()
                    if not (k.startswith("block_") and v["expire_time"] < now)
                }
                self._save_cooling_data()

                self._clean_invalid_cooling_records()
                logger.info("每日自动清理任务完成")
            except Exception as e:
                logger.error(f"定时任务失败: {traceback.format_exc()}")

    def __del__(self):
        """析构时启动定时任务"""
        asyncio.create_task(self._daily_reset_task())