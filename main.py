import json
import os
import time
from typing import Dict, Optional, Tuple

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

DATA_FILE = os.path.join("data", "plugin_data", "astrbot_plugin_role_at", "role_at_data.json")
PENDING_TIMEOUT = 300  # 广播确认等待超时：5分钟


@register("astrbot_plugin_role_at", "Author", "QQ群身份组管理与AT插件", "1.0.0")
class RoleAtPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data: Dict = {}
        # 待确认广播接收的用户：{(group_id, user_id): {"role_name": str, "time": float}}
        self.pending_broadcast: Dict[Tuple[str, str], Dict] = {}
        self._load_data()
        self._sync_freeat_from_config()
        self._update_config_preview()

    def _load_data(self):
        try:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
        except Exception as e:
            logger.error(f"[RoleAt] 加载数据失败: {e}")
            self.data = {}

    def _save_data(self):
        try:
            os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            self._update_config_preview()
        except Exception as e:
            logger.error(f"[RoleAt] 保存数据失败: {e}")

    def _sync_freeat_from_config(self):
        """将插件配置中的 freeat_settings 同步到运行时数据（配置优先）"""
        freeat_settings: dict = self.config.get("freeat_settings", {})
        changed = False
        for group_id, val in freeat_settings.items():
            if val not in (0, 1):
                continue
            group_data = self._get_group_data(group_id)
            if group_data.get("freeat") != int(val):
                group_data["freeat"] = int(val)
                changed = True
        if changed:
            try:
                os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
                with open(DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"[RoleAt] 同步 freeat 配置时写入失败: {e}")

    def _update_config_preview(self):
        """将各群身份组数据写入插件配置的预览字段，供 WebUI 查看"""
        try:
            preview: Dict = {}
            for group_id, group_data in self.data.items():
                roles_info: Dict = {}
                for role_name, role_info in group_data.get("roles", {}).items():
                    roles_info[role_name] = {
                        "max_members": role_info.get("max_members", 0),
                        "broadcast": role_info.get("broadcast", 0),
                        "visible": role_info.get("visible", 1),
                        "members": role_info.get("members", []),
                        "broadcast_accept": role_info.get("broadcast_accept", []),
                    }
                preview[group_id] = {
                    "freeat": group_data.get("freeat", 1),
                    "roles": roles_info,
                }
            self.config["group_data_preview"] = json.dumps(
                preview, ensure_ascii=False, indent=2
            )
            self.config.save_config()
        except Exception as e:
            logger.warning(f"[RoleAt] 更新配置预览失败: {e}")

    def _get_group_data(self, group_id: str) -> dict:
        if group_id not in self.data:
            self.data[group_id] = {"freeat": 1, "roles": {}}
        return self.data[group_id]

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查是否是 AstrBot 管理员或 QQ 群管理员/群主"""
        if event.is_admin():
            return True
        sender = getattr(event.message_obj, "sender", None)
        if sender:
            role = getattr(sender, "role", "member")
            if role in ("admin", "owner"):
                return True
        return False

    def _get_visible_roles_indexed(self, group_id: str) -> list:
        """返回可见身份列表，格式：[(序号, 名称, 信息), ...]"""
        group_data = self._get_group_data(group_id)
        return [
            (i + 1, name, info)
            for i, (name, info) in enumerate(group_data["roles"].items())
            if info.get("visible", 1) == 1
        ]

    def _clean_pending(self):
        """清理已超时的待确认广播状态"""
        now = time.time()
        stale = [k for k, v in self.pending_broadcast.items() if now - v.get("time", 0) > PENDING_TIMEOUT]
        for k in stale:
            del self.pending_broadcast[k]

    def _try_add_member(self, group_id: str, user_id: str, role_name: str, role_info: dict, is_admin_op: bool = False) -> Optional[str]:
        """
        尝试将用户加入身份组。成功返回 None，失败返回错误消息字符串。
        """
        subject = "该用户" if is_admin_op else "您"
        if user_id in role_info.get("members", []):
            return f"{subject}已经在身份组 [{role_name}] 中了。"
        max_members = role_info.get("max_members", 0)
        if max_members > 0 and len(role_info.get("members", [])) >= max_members:
            return f"身份组 [{role_name}] 已达人数上限（{max_members} 人）。"
        role_info.setdefault("members", []).append(user_id)
        self._save_data()
        return None

    # ─────────────────────────────────────────
    #   主指令入口
    # ─────────────────────────────────────────

    @filter.command("role")
    async def role_command(self, event: AstrMessageEvent):
        """身份组管理指令 /role <子命令> [参数]"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群组中使用。")
            return

        parts = event.message_str.strip().split()
        # parts[0] == "/role"，parts[1] 是子命令，parts[2:] 是参数
        if len(parts) < 2:
            yield event.plain_result(self._help_text())
            return

        subcommand = parts[1].lower()
        args = parts[2:]

        dispatch = {
            "addpreset": self._cmd_addpreset,
            "delpreset": self._cmd_delpreset,
            "list": self._cmd_list,
            "add": self._cmd_add,
            "remove": self._cmd_remove,
            "adminadd": self._cmd_adminadd,
            "adminremove": self._cmd_adminremove,
            "freeat": self._cmd_freeat,
            "at": self._cmd_at,
            "help": self._cmd_help,
        }

        handler = dispatch.get(subcommand)
        if handler is None:
            yield event.plain_result(f"❌ 未知子命令：{subcommand}\n\n{self._help_text()}")
            return

        async for result in handler(event, group_id, args):
            yield result

    # ─────────────────────────────────────────
    #   管理员指令
    # ─────────────────────────────────────────

    async def _cmd_addpreset(self, event, group_id, args):
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员才能创建预设身份。")
            return

        # 参数格式：<名称(可含空格)> <上限> <广播0/1> <可见0/1>
        # 最后三个参数固定为数字，前面的都是名称
        if len(args) < 4:
            yield event.plain_result("用法：/role addpreset <身份名> <上限人数(0=无上限)> <广播0/1> <可见0/1>\n示例：/role addpreset 足球爱好者 0 1 1")
            return

        try:
            visible = int(args[-1])
            broadcast = int(args[-2])
            max_members = int(args[-3])
            name = " ".join(args[:-3])
            if not name:
                raise ValueError("名称不能为空")
            if name.isdigit():
                raise ValueError("身份组名称不能为纯数字，以免与序号混淆")
            if broadcast not in (0, 1) or visible not in (0, 1):
                raise ValueError("广播/可见性参数必须为 0 或 1")
            if max_members < 0:
                raise ValueError("上限人数不能为负数")
        except (ValueError, IndexError) as e:
            yield event.plain_result(f"❌ 参数错误：{e}\n用法：/role addpreset <身份名> <上限人数> <广播0/1> <可见0/1>")
            return

        group_data = self._get_group_data(group_id)
        if name in group_data["roles"]:
            yield event.plain_result(f"❌ 身份 [{name}] 已存在，请先删除再重建，或使用其他名称。")
            return

        group_data["roles"][name] = {
            "max_members": max_members,
            "broadcast": broadcast,
            "visible": visible,
            "members": [],
            "broadcast_accept": [],
        }
        self._save_data()

        yield event.plain_result(
            f"✅ 已创建预设身份：{name}\n"
            f"├ 人数上限：{'无限制' if max_members == 0 else f'{max_members} 人'}\n"
            f"├ 广播功能：{'开启 📢' if broadcast else '关闭'}\n"
            f"└ 可见性：{'可见' if visible else '隐藏'}"
        )

    async def _cmd_delpreset(self, event, group_id, args):
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员才能删除预设身份。")
            return

        if not args:
            yield event.plain_result("用法：/role delpreset <身份名>")
            return

        name = " ".join(args)
        group_data = self._get_group_data(group_id)
        if name not in group_data["roles"]:
            yield event.plain_result(f"❌ 未找到身份组 [{name}]。")
            return

        del group_data["roles"][name]
        # 清理该身份组相关的待确认状态
        to_remove = [k for k, v in self.pending_broadcast.items() if v.get("role_name") == name and k[0] == group_id]
        for k in to_remove:
            del self.pending_broadcast[k]
        self._save_data()

        yield event.plain_result(f"✅ 已删除身份组 [{name}]。")

    async def _cmd_adminadd(self, event, group_id, args):
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员才能执行此操作。")
            return

        if len(args) < 2:
            yield event.plain_result("用法：/role adminadd <用户QQ号> <序号>")
            return

        target_qq = args[0].lstrip("@")
        try:
            index = int(args[1])
        except ValueError:
            yield event.plain_result("❌ 序号必须为整数。")
            return

        visible_roles = self._get_visible_roles_indexed(group_id)
        found = next(((name, info) for idx, name, info in visible_roles if idx == index), None)
        if not found:
            yield event.plain_result(f"❌ 未找到序号 {index} 的身份，请用 /role list 查看列表。")
            return

        role_name, role_info = found
        error = self._try_add_member(group_id, target_qq, role_name, role_info, is_admin_op=True)
        if error:
            yield event.plain_result(f"❌ {error}")
            return

        yield event.chain_result([
            Comp.At(qq=target_qq),
            Comp.Plain(f" 管理员已将您加入身份组 [{role_name}]！"),
        ])

        if role_info.get("broadcast") == 1:
            self._clean_pending()
            self.pending_broadcast[(group_id, target_qq)] = {
                "role_name": role_name,
                "time": time.time(),
            }
            yield event.chain_result([
                Comp.At(qq=target_qq),
                Comp.Plain(
                    f"\n该身份组开启了广播功能，是否接收来自 [{role_name}] 的广播消息？\n"
                    "✅ 回复「同意」接收广播\n"
                    "❌ 回复「拒绝」不接收广播\n"
                    "（5 分钟内有效）"
                ),
            ])

    async def _cmd_adminremove(self, event, group_id, args):
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员才能执行此操作。")
            return

        if len(args) < 2:
            yield event.plain_result("用法：/role adminremove <用户QQ号> <序号>")
            return

        target_qq = args[0].lstrip("@")
        try:
            index = int(args[1])
        except ValueError:
            yield event.plain_result("❌ 序号必须为整数。")
            return

        visible_roles = self._get_visible_roles_indexed(group_id)
        found = next(((name, info) for idx, name, info in visible_roles if idx == index), None)
        if not found:
            yield event.plain_result(f"❌ 未找到序号 {index} 的身份。")
            return

        role_name, role_info = found
        members = role_info.setdefault("members", [])
        if target_qq not in members:
            yield event.plain_result(f"❌ 用户 {target_qq} 不在身份组 [{role_name}] 中。")
            return

        members.remove(target_qq)
        broadcast_accept = role_info.setdefault("broadcast_accept", [])
        if target_qq in broadcast_accept:
            broadcast_accept.remove(target_qq)
        self.pending_broadcast.pop((group_id, target_qq), None)
        self._save_data()

        yield event.chain_result([
            Comp.At(qq=target_qq),
            Comp.Plain(f" 管理员已将您从身份组 [{role_name}] 中移除。"),
        ])

    async def _cmd_freeat(self, event, group_id, args):
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员才能设置此选项。")
            return

        if not args or args[0] not in ("0", "1"):
            yield event.plain_result("用法：/role freeat <0/1>\n0 = 关闭（仅管理员可 AT）\n1 = 开启（所有人可 AT）")
            return

        value = int(args[0])
        group_data = self._get_group_data(group_id)
        group_data["freeat"] = value
        self._save_data()

        # 同步到插件配置
        freeat_settings: dict = self.config.setdefault("freeat_settings", {})
        freeat_settings[group_id] = value
        self.config.save_config()

        status = "开启（所有人可使用 /role at）" if value else "关闭（仅管理员可使用 /role at）"
        yield event.plain_result(f"✅ 身份组 AT 功能已{status}。")

    # ─────────────────────────────────────────
    #   通用指令
    # ─────────────────────────────────────────

    async def _cmd_list(self, event, group_id, args):
        visible_roles = self._get_visible_roles_indexed(group_id)
        if not visible_roles:
            yield event.plain_result("当前没有可见的预设身份组。")
            return

        lines = ["📋 当前群预设身份列表："]
        for idx, name, info in visible_roles:
            member_count = len(info.get("members", []))
            max_members = info.get("max_members", 0)
            max_str = "无上限" if max_members == 0 else f"上限 {max_members} 人"
            broadcast_tag = " 📢" if info.get("broadcast") else ""
            lines.append(f"{idx}. {name}（{member_count} 人 / {max_str}）{broadcast_tag}")

        yield event.plain_result("\n".join(lines))

    async def _cmd_add(self, event, group_id, args):
        if not args:
            yield event.plain_result("用法：/role add <序号|身份名>\n先用 /role list 查看身份列表。")
            return

        visible_roles = self._get_visible_roles_indexed(group_id)
        query = " ".join(args)

        if query.isdigit():
            # 按序号查找
            index = int(query)
            found = next(((name, info) for idx, name, info in visible_roles if idx == index), None)
            hint = f"序号 {index}"
        else:
            # 按身份名查找（精确匹配）
            found = next(((name, info) for _, name, info in visible_roles if name == query), None)
            hint = f"身份名「{query}」"

        if not found:
            yield event.plain_result(f"❌ 未找到{hint}对应的身份，请用 /role list 查看可用身份。")
            return

        role_name, role_info = found
        user_id = event.get_sender_id()

        error = self._try_add_member(group_id, user_id, role_name, role_info)
        if error:
            yield event.plain_result(f"❌ {error}")
            return

        yield event.chain_result([
            Comp.At(qq=user_id),
            Comp.Plain(f" 您已成功加入身份组 [{role_name}]！"),
        ])

        if role_info.get("broadcast") == 1:
            self._clean_pending()
            self.pending_broadcast[(group_id, user_id)] = {
                "role_name": role_name,
                "time": time.time(),
            }
            yield event.chain_result([
                Comp.At(qq=user_id),
                Comp.Plain(
                    f"\n该身份组开启了广播功能，是否接收来自 [{role_name}] 的广播消息？\n"
                    "✅ 回复「同意」接收广播\n"
                    "❌ 回复「拒绝」不接收广播\n"
                    "（5 分钟内有效）"
                ),
            ])

    async def _cmd_remove(self, event, group_id, args):
        if not args:
            yield event.plain_result("用法：/role remove <序号>\n先用 /role list 查看身份列表。")
            return

        try:
            index = int(args[0])
        except ValueError:
            yield event.plain_result("❌ 序号必须为整数。")
            return

        visible_roles = self._get_visible_roles_indexed(group_id)
        found = next(((name, info) for idx, name, info in visible_roles if idx == index), None)
        if not found:
            yield event.plain_result(f"❌ 未找到序号 {index} 的身份。")
            return

        role_name, role_info = found
        user_id = event.get_sender_id()
        members = role_info.setdefault("members", [])

        if user_id not in members:
            yield event.plain_result(f"❌ 您不在身份组 [{role_name}] 中。")
            return

        members.remove(user_id)
        broadcast_accept = role_info.setdefault("broadcast_accept", [])
        if user_id in broadcast_accept:
            broadcast_accept.remove(user_id)
        self.pending_broadcast.pop((group_id, user_id), None)
        self._save_data()

        yield event.chain_result([
            Comp.At(qq=user_id),
            Comp.Plain(f" 您已退出身份组 [{role_name}]。"),
        ])

    async def _cmd_at(self, event, group_id, args):
        if not args:
            yield event.plain_result("用法：/role at <身份名> [附加消息]")
            return

        group_data = self._get_group_data(group_id)

        if not self._is_admin(event) and group_data.get("freeat", 1) == 0:
            yield event.plain_result("❌ 管理员已关闭普通用户的身份组 AT 功能。")
            return

        # 从最长前缀开始匹配身份名，其余部分作为附加消息
        roles = group_data["roles"]
        role_name = None
        extra_msg = ""
        for i in range(len(args), 0, -1):
            candidate = " ".join(args[:i])
            if candidate in roles:
                role_name = candidate
                extra_msg = " ".join(args[i:])
                break

        if role_name is None:
            yield event.plain_result(f"❌ 未找到身份组「{' '.join(args)}」，请检查名称是否正确。")
            return

        role_info = roles[role_name]
        members = role_info.get("members", [])
        broadcast_accept = role_info.get("broadcast_accept", [])

        if role_info.get("broadcast") == 1:
            # 广播身份组：只 AT 已接受广播的成员
            at_targets = [m for m in members if m in broadcast_accept]
            if not at_targets:
                yield event.plain_result(f"❌ 身份组 [{role_name}] 中没有接受广播的成员。")
                return
        else:
            # 非广播身份组：AT 所有成员
            at_targets = members
            if not at_targets:
                yield event.plain_result(f"❌ 身份组 [{role_name}] 中没有成员。")
                return

        chain = []
        for qq in at_targets:
            chain.append(Comp.At(qq=qq))
            chain.append(Comp.Plain(" "))
        notice = f"\n📣 来自身份组 [{role_name}] 的呼叫"
        if extra_msg:
            notice += f"\n{extra_msg}"
        chain.append(Comp.Plain(notice))

        yield event.chain_result(chain)

    async def _cmd_help(self, event, group_id, args):
        yield event.plain_result(self._help_text())

    def _help_text(self) -> str:
        return (
            "📖 身份组管理指令帮助\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "【通用指令】\n"
            "/role list — 查看所有可见身份\n"
            "/role add <序号|身份名> — 加入身份\n"
            "/role remove <序号> — 退出身份\n"
            "/role at <身份名> [附加消息] — AT 该身份组成员\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "【管理员专属】\n"
            "/role addpreset <名称> <上限> <广播0/1> <可见0/1>\n"
            "    创建预设身份（上限0=无上限）\n"
            "/role delpreset <名称> — 删除预设身份\n"
            "/role adminadd <QQ> <序号> — 为用户添加身份\n"
            "/role adminremove <QQ> <序号> — 移除用户的身份\n"
            "/role freeat <0/1> — 开关普通用户的 AT 功能"
        )

    # ─────────────────────────────────────────
    #   广播接收确认监听
    # ─────────────────────────────────────────

    @filter.regex(r"^(同意|拒绝|ok|OK|no|NO)$")
    async def broadcast_confirm(self, event: AstrMessageEvent):
        """拦截群内「同意」/「拒绝」/「ok」/「no」消息，处理广播接收确认"""
        group_id = event.get_group_id()
        if not group_id:
            return

        user_id = event.get_sender_id()
        self._clean_pending()

        key = (group_id, user_id)
        if key not in self.pending_broadcast:
            # 无待确认状态，不处理
            return

        pending = self.pending_broadcast.pop(key)
        role_name = pending["role_name"]
        msg = event.message_str.strip().lower()

        group_data = self._get_group_data(group_id)
        if role_name not in group_data["roles"]:
            return

        role_info = group_data["roles"][role_name]
        broadcast_accept = role_info.setdefault("broadcast_accept", [])

        if msg in ("同意", "ok"):
            if user_id not in broadcast_accept:
                broadcast_accept.append(user_id)
            self._save_data()
            yield event.chain_result([
                Comp.At(qq=user_id),
                Comp.Plain(f" 已开启接收来自 [{role_name}] 的广播消息 ✅"),
            ])
        else:
            if user_id in broadcast_accept:
                broadcast_accept.remove(user_id)
            self._save_data()
            yield event.chain_result([
                Comp.At(qq=user_id),
                Comp.Plain(f" 已关闭接收来自 [{role_name}] 的广播消息。"),
            ])

        event.stop_event()

    # ─────────────────────────────────────────
    #   生命周期
    # ─────────────────────────────────────────

    async def terminate(self):
        self._save_data()
        self._save_data()
