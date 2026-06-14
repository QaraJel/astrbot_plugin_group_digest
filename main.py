"""
astrbot_plugin_group_digest - 群聊摘要助手
实时采集 QQ 群聊文本消息，定时 AI 摘要并输出 MD 报告。
"""
import os
import re
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain, Image, Record as CompRecord, File as CompFile
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_group_digest"

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    """移除文件名中的不安全字符。"""
    return re.sub(r'[<>:"/\\|?*]', '_', name)


def _get_data_dir() -> Path:
    """获取插件数据目录。"""
    return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME


# ---------------------------------------------------------------------------
# 插件主类
# ---------------------------------------------------------------------------

@register(PLUGIN_NAME, "QaraJel", "QQ群聊采集+AI摘要+转发，支持sendmd/sendraw命令", "v1.1.0")
class GroupDigest(Star):
    """群聊摘要助手插件。

    工作流:
        QQ消息 → 流式追加到活跃 .md 文件 → 定时触发 LLM 摘要 → 发送回话 → 创建新文件 → 循环
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.scheduler: AsyncIOScheduler | None = None
        self.data_dir = _get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._platform_prefix = "aiocqhttp"  # 首次收到消息时更新为真实前缀
        self._style = None  # pillowmd 加载的风格对象

    # =======================================================================
    # 生命周期
    # =======================================================================

    async def initialize(self):
        """插件初始化：加载图片渲染风格，启动定时调度器，预创建汇总文件。"""
        # 加载 pillowmd 风格：优先使用配置路径，回退到内置 modern-dark
        config_style_path = self.config.get("style_path", "").strip()
        if config_style_path and os.path.isdir(config_style_path):
            style_dir = config_style_path
        else:
            style_dir = Path(__file__).parent / "styles" / "modern-dark"

        if os.path.isdir(style_dir):
            try:
                import pillowmd
                loop = asyncio.get_running_loop()
                self._style = await loop.run_in_executor(
                    None, lambda: pillowmd.LoadMarkdownStyles(str(style_dir))
                )
                logger.info(f"[{PLUGIN_NAME}] 已加载图片渲染风格: {style_dir}")
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 加载风格失败，使用默认渲染: {e}")
                self._style = None
        else:
            logger.warning(f"[{PLUGIN_NAME}] 风格目录不存在: {style_dir}，使用默认渲染")
            self._style = None

        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._setup_summary_schedule()
        self._setup_cache_clean_schedule()
        self.scheduler.start()
        await self._pre_create_active_files()
        logger.info(f"[{PLUGIN_NAME}] 插件初始化完成，数据目录: {self.data_dir}")

    async def terminate(self):
        """插件卸载时关闭调度器。"""
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info(f"[{PLUGIN_NAME}] 插件已卸载")

    # =======================================================================
    # 调度器设置
    # =======================================================================

    def _setup_summary_schedule(self):
        """解析 summary_schedule 配置并注册 cron 任务。"""
        schedule_str = self.config.get("summary_schedule", "08:00,20:00")
        times = [t.strip() for t in schedule_str.split(",") if t.strip()]
        for t in times:
            parts = t.split(":")
            if len(parts) != 2:
                logger.warning(f"[{PLUGIN_NAME}] 无效的时间格式: '{t}'，已跳过")
                continue
            try:
                hour = int(parts[0].strip())
                minute = int(parts[1].strip())
            except ValueError:
                logger.warning(f"[{PLUGIN_NAME}] 无法解析时间: '{t}'，已跳过")
                continue

            # cron 规范：hour 范围为 0-23，24 视为 0（午夜）
            if hour == 24:
                hour = 0
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                logger.warning(f"[{PLUGIN_NAME}] 时间超出范围: '{t}' (hour 0-23, minute 0-59)，已跳过")
                continue

            self.scheduler.add_job(
                self._scheduled_summary,
                'cron',
                hour=hour,
                minute=minute,
                id=f"summary_{hour:02d}_{minute:02d}",
                replace_existing=True,
            )
            logger.info(f"[{PLUGIN_NAME}] 定时摘要已注册: 每天 {hour:02d}:{minute:02d}")

    def _setup_cache_clean_schedule(self):
        """注册缓存清理定时任务。"""
        interval = self.config.get("cache_clean_interval_hours", 12)
        self.scheduler.add_job(
            self.do_cache_clean,
            'interval',
            hours=interval,
            id="cache_clean",
            replace_existing=True,
        )
        logger.info(f"[{PLUGIN_NAME}] 缓存清理已注册: 每 {interval} 小时")

    async def _pre_create_active_files(self):
        """为所有已配置的群聊预创建空白活跃汇总文件。"""
        for group_id in self.config.get("enable_groups", []):
            session_key = f"group_{group_id}"
            existing = self._get_session_dir(session_key)
            # 仅在目录为空时预创建，已有文件则不覆盖
            if not any(existing.glob("*.md")):
                now = datetime.now()
                filename = now.strftime("%y-%m-%d-%H-%M.md")
                path = existing / filename
                path.write_text("", encoding="utf-8")
                await self.put_kv_data(f"active_file_{session_key}", filename)
                logger.info(f"[{PLUGIN_NAME}] 预创建活跃文件: {path}")
            else:
                logger.info(f"[{PLUGIN_NAME}] {session_key} 已有汇总文件，跳过预创建")

    # =======================================================================
    # 消息处理
    # =======================================================================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群聊消息，追加到活跃汇总文件。"""
        await self._handle_message(event, is_group=True)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_private_message(self, event: AstrMessageEvent):
        """监听私聊消息（测试用），追加到活跃汇总文件。"""
        if not self.config.get("enable_private", True):
            return
        await self._handle_message(event, is_group=False)

    async def _handle_message(self, event: AstrMessageEvent, is_group: bool):
        """统一处理消息采集逻辑。"""
        msg_obj = event.message_obj

        # self_recursion 检查
        if not self.config.get("self_recursion", False):
            sender_id = str(msg_obj.sender.user_id) if msg_obj.sender else ""
            self_id = str(msg_obj.self_id)
            if sender_id == self_id:
                return

        # 确定会话标识
        if is_group:
            group_id = str(msg_obj.group_id)
            if group_id not in [str(g) for g in self.config.get("enable_groups", [])]:
                return
            session_key = f"group_{group_id}"
        else:
            user_id = str(msg_obj.sender.user_id) if msg_obj.sender else "unknown"
            session_key = f"private_{user_id}"

        # 保存 unified_msg_origin 以便定时任务发送消息
        umo = event.unified_msg_origin
        await self.put_kv_data(f"umo_{session_key}", umo)

        # 捕获真实的平台前缀（替代硬编码 "aiocqhttp"）
        self._platform_prefix = umo.split(":")[0]

        # 提取消息文本
        sender_name = self._get_sender_display_name(event)
        text_content = self._extract_message_text(event)

        if not text_content:
            return

        # 追加到活跃文件
        await self._append_message(session_key, sender_name, text_content)

    def _get_sender_display_name(self, event: AstrMessageEvent) -> str:
        """获取发送者显示名称。"""
        name_record = self.config.get("name_record", True)
        if not name_record:
            return ""  # 匿名模式
        msg_obj = event.message_obj
        if msg_obj.sender and msg_obj.sender.nickname:
            return msg_obj.sender.nickname
        return event.get_sender_name()

    def _extract_message_text(self, event: AstrMessageEvent) -> str:
        """从消息链中提取文本内容。"""
        msg_obj = event.message_obj
        if not msg_obj.message:
            return ""

        parts = []
        for comp in msg_obj.message:
            if isinstance(comp, Plain):
                text = comp.text.strip()
                if text:
                    parts.append(text)
            elif isinstance(comp, Image):
                if self.config.get("modality", {}).get("image", False):
                    parts.append("[图片]")
            elif isinstance(comp, CompRecord):
                if self.config.get("modality", {}).get("voice", False):
                    parts.append("[语音]")

        return " ".join(parts)

    # =======================================================================
    # 文件管理
    # =======================================================================

    def _get_session_dir(self, session_key: str) -> Path:
        """获取会话的汇总文件目录，确保存在。"""
        d = self.data_dir / session_key
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def _get_active_file_path(self, session_key: str) -> Path | None:
        """获取当前活跃汇总文件路径。"""
        filename = await self.get_kv_data(f"active_file_{session_key}", "")
        if filename:
            path = self._get_session_dir(session_key) / filename
            if path.exists():
                return path
        return None

    async def _create_new_active_file(self, session_key: str) -> Path:
        """创建新的空白活跃汇总文件，返回路径。"""
        now = datetime.now()
        filename = now.strftime("%y-%m-%d-%H-%M.md")
        path = self._get_session_dir(session_key) / filename
        path.write_text("", encoding="utf-8")
        await self.put_kv_data(f"active_file_{session_key}", filename)
        logger.info(f"[{PLUGIN_NAME}] 新活跃文件: {path}")
        return path

    async def _append_message(self, session_key: str, sender_name: str, content: str):
        """追加一条消息到当前活跃汇总文件。"""
        active_path = await self._get_active_file_path(session_key)
        if active_path is None:
            active_path = await self._create_new_active_file(session_key)

        if sender_name:
            line = f"[{sender_name}] {content}\n"
        else:
            line = f"{content}\n"

        with open(active_path, "a", encoding="utf-8") as f:
            f.write(line)

    async def _get_summary_files(
        self, session_key: str, coupling_count: int
    ) -> list[tuple[Path, bool]]:
        """获取用于摘要的文件列表。

        Returns:
            list of (file_path, is_latest): 文件路径及是否为最新文件
        """
        session_dir = self._get_session_dir(session_key)
        active_path = await self._get_active_file_path(session_key)

        # 收集所有 .md 文件，按修改时间排序（新的在前）
        all_files = sorted(
            session_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        if not all_files:
            return []

        result = []

        # 最新文件（活跃文件）必定包含
        if active_path and active_path in all_files:
            result.append((active_path, True))
            all_files.remove(active_path)
        elif all_files:
            result.append((all_files[0], True))
            all_files = all_files[1:]

        # 耦合的历史文件
        for f in all_files[:coupling_count]:
            result.append((f, False))

        return result

    # =======================================================================
    # 摘要生成
    # =======================================================================

    async def _generate_summary(
        self, session_key: str, coupling_count: int
    ) -> tuple[str | None, list, int]:
        """执行 LLM 摘要生成，返回 (summary_text, files, prompt_chars)。
        若无法生成则返回 (None, [], 0)。
        """
        files = await self._get_summary_files(session_key, coupling_count)
        if not files:
            logger.info(f"[{PLUGIN_NAME}] {session_key} 无汇总文件，跳过摘要")
            return None, [], 0

        active_file = files[0][0]
        try:
            active_content = active_file.read_text(encoding="utf-8").strip()
        except Exception:
            active_content = ""

        if not active_content:
            logger.info(f"[{PLUGIN_NAME}] {session_key} 活跃文件为空，跳过摘要")
            return None, [], 0

        prompt = self._build_summary_prompt(files, active_content)
        prompt_chars = len(prompt)

        try:
            logger.info(f"[{PLUGIN_NAME}] {session_key} 开始调用 LLM 生成摘要...")
            llm_resp = await self.context.llm_generate(
                chat_provider_id=self.config.get("summary_provider_id", "deepseek/deepseek-v4-flash"),
                prompt=prompt,
            )
            summary_text = llm_resp.completion_text
            logger.info(f"[{PLUGIN_NAME}] {session_key} LLM 摘要完成 ({len(summary_text)} 字)")
            return summary_text, files, prompt_chars
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] {session_key} LLM 调用失败: {e}")
            return None, [], 0

    async def do_summary(
        self, session_key: str, umo: str, coupling_count: int | None = None
    ):
        """执行摘要核心逻辑（含发送和自动转发）。

        Args:
            session_key: 会话标识
            umo: unified_msg_origin
            coupling_count: 耦合数，None 表示使用配置默认值
        """
        if coupling_count is None:
            coupling_count = self.config.get("coupling_count", 0)

        summary_text, files, _ = await self._generate_summary(session_key, coupling_count)
        if summary_text is None:
            return

        # 发送到自身会话
        try:
            await self._send_summary_result(umo, summary_text, files)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] {session_key} 发送摘要失败: {e}")

        # 仅 `/generate` 命令使用转发配置，定时摘要不自动转发
        await self._create_new_active_file(session_key)
        logger.info(f"[{PLUGIN_NAME}] {session_key} 摘要完成，已创建新活跃文件")

    def _build_summary_prompt(
        self, files: list[tuple[Path, bool]], active_content: str
    ) -> str:
        """构建发送给 LLM 的完整 prompt。"""
        summary_prompt = self.config.get(
            "summary_prompt",
            "你是一个专业的群聊总结助手。请根据以下群聊消息记录，生成一份结构化的群聊摘要。",
        )

        parts = [summary_prompt, "", "---", ""]

        # 历史文件（带日期标注）
        for file_path, is_latest in files:
            if not is_latest:
                # 从文件名提取创建时间
                mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                date_label = mtime.strftime("%y/%m/%d %H:%M")
                parts.append(f"## 创建时间: {date_label}")
                try:
                    parts.append(file_path.read_text(encoding="utf-8"))
                except Exception:
                    parts.append("(文件读取失败)")
                parts.append("")

        # 最新文件
        if len(files) > 1:
            parts.append("---")
            parts.append("## 最新汇总文件（请重点摘要此部分）")
            parts.append("")
        parts.append(active_content)

        return "\n".join(parts)

    async def _render_local_image(
        self, display_text: str, source_desc: str, gen_time: str
    ) -> MessageChain:
        """用 pillowmd 本地渲染摘要为图片（modern-dark 风格）。"""

        md_text = f"# {source_desc}\n\n*生成时间: {gen_time}*\n\n{display_text}"
        loop = asyncio.get_running_loop()

        if self._style is not None:
            result = await loop.run_in_executor(None, lambda: self._style.Render(md_text))
            img = getattr(result, "image", result)  # Render 可能返回 PIL Image 或 MdRenderResult
        else:
            import pillowmd
            result = await pillowmd.MdToImage(md_text)
            img = result.image

        tmp_path = self.data_dir / f"_tmp_img_{datetime.now().strftime('%m%d%H%M%S')}.png"
        await loop.run_in_executor(None, lambda: img.save(str(tmp_path)))
        chain = MessageChain()
        from astrbot.api.message_components import Image as CompImage
        chain.chain.append(CompImage.fromFileSystem(str(tmp_path)))
        asyncio.create_task(self._delayed_unlink(tmp_path, 300))
        return chain

    @staticmethod
    async def _delayed_unlink(path: Path, delay: int):
        await asyncio.sleep(delay)
        try:
            path.unlink()
        except Exception:
            pass

    async def _send_summary_result(
        self, umo: str, summary_text: str, files: list,
        source_info: str | None = None,
    ):
        """发送 AI 摘要结果到指定会话。

        Args:
            umo: 目标 unified_msg_origin
            summary_text: LLM 生成的摘要文本
            files: 源文件列表（未使用，保留兼容）
            source_info: public 模式下来源标注，None 则不显示
        """
        output_mode = self.config.get("output_mode", "image")

        if source_info:
            display_text = f"{source_info}\n\n{summary_text}"
        else:
            display_text = summary_text

        if output_mode == "file":
            md_content = f"# 群聊摘要\n\n生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n{display_text}"
            tmp_path = self.data_dir / f"_tmp_summary_{umo.replace(':', '_')}.md"
            tmp_path.write_text(md_content, encoding="utf-8")
            chain = MessageChain()
            chain.chain.append(CompFile(file=str(tmp_path), name=f"摘要_{datetime.now().strftime('%m%d_%H%M')}.md"))
            await self.context.send_message(umo, chain)
            try:
                tmp_path.unlink()
            except Exception:
                pass
        else:
            gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            source_desc = source_info or "群聊摘要"
            try:
                chain = await self._render_local_image(display_text, source_desc, gen_time)
                await self.context.send_message(umo, chain)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 本地渲染失败，回退到文本: {e}")
                await self.context.send_message(
                    umo,
                    MessageChain().message(f"📋 群聊摘要\n\n{display_text}"),
                )

    async def _send_raw_file(
        self, umo: str, raw_content: str,
        source_info: str | None = None,
    ):
        """发送原始汇总日志为 .md 文件附件。

        Args:
            umo: 目标 unified_msg_origin
            raw_content: 原始汇总内容
            source_info: public 模式下来源标注
        """
        if source_info:
            display_content = f"{source_info}\n\n{raw_content}"
        else:
            display_content = raw_content

        tmp_path = self.data_dir / f"_tmp_raw_{umo.replace(':', '_')}.md"
        tmp_path.write_text(display_content, encoding="utf-8")
        chain = MessageChain()
        chain.chain.append(CompFile(file=str(tmp_path), name=f"原始日志_{datetime.now().strftime('%m%d_%H%M')}.md"))
        try:
            await self.context.send_message(umo, chain)
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass

    # =======================================================================
    # 定时任务
    # =======================================================================

    async def _scheduled_summary(self):
        """定时触发：对所有已跟踪的会话执行摘要。"""
        logger.info(f"[{PLUGIN_NAME}] 定时摘要触发")
        coupling_count = self.config.get("coupling_count", 0)

        # 遍历数据目录下的所有会话
        if not self.data_dir.exists():
            return

        for session_dir in self.data_dir.iterdir():
            if not session_dir.is_dir():
                continue
            session_key = session_dir.name

            # 对于群聊，检查是否在 enable_groups 中
            if session_key.startswith("group_"):
                group_id = session_key[6:]
                if group_id not in [str(g) for g in self.config.get("enable_groups", [])]:
                    continue
                # 检查是否在定时摘要排除名单中（黑名单）
                exclude_groups = self.config.get("scheduled_summary_groups", "").strip()
                if exclude_groups:
                    excluded = [g.strip() for g in exclude_groups.split(",") if g.strip()]
                    if group_id in excluded:
                        continue
            elif session_key.startswith("private_"):
                if not self.config.get("enable_private", True):
                    continue
            else:
                continue

            umo = await self.get_kv_data(f"umo_{session_key}", "")
            if not umo:
                logger.warning(f"[{PLUGIN_NAME}] {session_key} 无 umo 记录，跳过")
                continue

            await self.do_summary(session_key, umo, coupling_count)

    # =======================================================================
    # 缓存清理
    # =======================================================================

    async def do_cache_clean(self):
        """清理超过保留天数的汇总文件。"""
        days = self.config.get("cache_clean_days", 7)
        if days <= 0:
            return

        cutoff = datetime.now() - timedelta(days=days)
        cleaned = 0

        if not self.data_dir.exists():
            return

        for session_dir in self.data_dir.iterdir():
            if not session_dir.is_dir():
                continue
            for md_file in session_dir.glob("*.md"):
                mtime = datetime.fromtimestamp(md_file.stat().st_mtime)
                if mtime < cutoff:
                    # 不删除当前活跃文件
                    session_key = session_dir.name
                    active_filename = await self.get_kv_data(
                        f"active_file_{session_key}", ""
                    )
                    if md_file.name == active_filename:
                        continue
                    try:
                        md_file.unlink()
                        cleaned += 1
                    except Exception as e:
                        logger.warning(f"[{PLUGIN_NAME}] 删除文件失败 {md_file}: {e}")

        if cleaned > 0:
            logger.info(f"[{PLUGIN_NAME}] 缓存清理: 删除 {cleaned} 个过期文件")

    # =======================================================================
    # 用户命令
    # =======================================================================

    # -----------------------------------------------------------------------
    # 转发与命令辅助方法
    # -----------------------------------------------------------------------

    def _build_target_umo(self, target_id: str, target_type: str) -> str:
        """构造目标会话的 unified_msg_origin。

        Args:
            target_id: 群号或 QQ 号
            target_type: "group" 或 "private"
        """
        msg_type = "GroupMessage" if target_type == "group" else "FriendMessage"
        return f"{self._platform_prefix}:{msg_type}:{target_id}"

    def _get_source_info(self, event: AstrMessageEvent) -> str:
        """从事件中提取来源信息（public 模式下附加到目标消息）。"""
        msg_obj = event.message_obj
        if msg_obj.group_id:
            return f"来源: 群 {msg_obj.group_id}"
        else:
            sender_name = event.get_sender_name()
            sender_id = msg_obj.sender.user_id if msg_obj.sender else "unknown"
            return f"来源: 私聊 {sender_name}({sender_id})"

    def _resolve_session_key(self, event: AstrMessageEvent) -> str:
        """从事件解析会话标识。"""
        msg_obj = event.message_obj
        if msg_obj.group_id:
            return f"group_{msg_obj.group_id}"
        else:
            user_id = str(msg_obj.sender.user_id) if msg_obj.sender else "unknown"
            return f"private_{user_id}"

    def _should_notify(self, mode: str) -> bool:
        """检查是否应显示进度通知。

        当 suppress_notifications 开启时，永远返回 False。
        """
        if self.config.get("suppress_notifications", False):
            return False
        return mode == "public"

    async def _forward_to_targets(
        self, summary_text: str, files: list
    ):
        """静默转发 AI 摘要到所有配置的转发目标。"""
        for group_id in self.config.get("forward_groups", []):
            target_umo = self._build_target_umo(str(group_id), "group")
            try:
                await self._send_summary_result(target_umo, summary_text, files)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 转发摘要到群 {group_id} 失败: {e}")

        for user_id in self.config.get("forward_private", []):
            target_umo = self._build_target_umo(str(user_id), "private")
            try:
                await self._send_summary_result(target_umo, summary_text, files)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 转发摘要到私聊 {user_id} 失败: {e}")

    async def _forward_raw_to_targets(self, raw_content: str):
        """静默转发原始日志到所有配置的转发目标。"""
        for group_id in self.config.get("forward_groups", []):
            target_umo = self._build_target_umo(str(group_id), "group")
            try:
                await self._send_raw_file(target_umo, raw_content)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 转发原始日志到群 {group_id} 失败: {e}")

        for user_id in self.config.get("forward_private", []):
            target_umo = self._build_target_umo(str(user_id), "private")
            try:
                await self._send_raw_file(target_umo, raw_content)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 转发原始日志到私聊 {user_id} 失败: {e}")

    # -----------------------------------------------------------------------
    # /sendmd — AI 摘要
    # -----------------------------------------------------------------------

    @filter.command("sendmd")
    async def cmd_sendmd(
        self, event: AstrMessageEvent,
        target_id: str = "0",
        mode: str = "public",
        coupling_count: int = 1,
    ):
        """生成一次 AI 摘要并发送到指定目标。

        target_id: 目标群号/QQ号，0=当前会话
        mode: public=显示进度通知，private=完全静默
        coupling_count: 耦合历史文件数，默认 1（当前+前1个日志）
        """
        session_key = self._resolve_session_key(event)
        umo = event.unified_msg_origin
        await self.put_kv_data(f"umo_{session_key}", umo)

        # 确定目标
        if target_id == "0":
            target_umo = umo
        else:
            all_known_groups = [str(g) for g in self.config.get("enable_groups", [])] + [str(g) for g in self.config.get("forward_groups", [])]
            target_type = "group" if target_id in all_known_groups else "private"
            target_umo = self._build_target_umo(target_id, target_type)

        notify = self._should_notify(mode)
        if notify:
            yield event.plain_result("🔄 正在生成摘要，请稍候...")

        # 生成摘要
        summary_text, files, prompt_chars = await self._generate_summary(session_key, coupling_count=coupling_count)
        if summary_text is None:
            if notify:
                yield event.plain_result("⚠️ 无内容可摘要")
            event.stop_event()
            return

        # 发送到目标
        source_info = self._get_source_info(event) if notify else None
        try:
            await self._send_summary_result(target_umo, summary_text, files, source_info=source_info)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 发送摘要到 {target_id} 失败: {e}")
            if notify:
                yield event.plain_result(f"⚠️ 发送失败: {e}")
            event.stop_event()
            return

        # 仅当发送给自己时才关闭源文件（数据被本群消费）
        if target_id == "0":
            await self._create_new_active_file(session_key)

        if notify:
            est_tokens = prompt_chars // 2
            yield event.plain_result(f"✅ 转发成功（约 {est_tokens:,} tokens）")
        event.stop_event()

    # -----------------------------------------------------------------------
    # /sendraw — 原始日志
    # -----------------------------------------------------------------------

    @filter.command("sendraw")
    async def cmd_sendraw(
        self, event: AstrMessageEvent,
        target_id: str = "0",
        mode: str = "public",
        coupling_count: int = 1,
    ):
        """发送原始汇总日志（.md 文件），不经过 AI 摘要。

        target_id: 目标群号/QQ号，0=当前会话
        mode: public=显示进度通知，private=完全静默
        coupling_count: 耦合历史文件数，默认 1（当前+前1个日志）
        """
        session_key = self._resolve_session_key(event)
        umo = event.unified_msg_origin
        await self.put_kv_data(f"umo_{session_key}", umo)

        # 确定目标
        if target_id == "0":
            target_umo = umo
        else:
            all_known_groups = [str(g) for g in self.config.get("enable_groups", [])] + [str(g) for g in self.config.get("forward_groups", [])]
            target_type = "group" if target_id in all_known_groups else "private"
            target_umo = self._build_target_umo(target_id, target_type)

        notify = self._should_notify(mode)
        if notify:
            yield event.plain_result("🔄 正在准备原始日志...")

        # 获取文件
        files = await self._get_summary_files(session_key, coupling_count)
        if not files:
            if notify:
                yield event.plain_result("⚠️ 无汇总文件可发送")
            event.stop_event()
            return

        # 组装原始内容
        raw_parts = []
        for file_path, is_latest in files:
            try:
                content = file_path.read_text(encoding="utf-8").strip()
            except Exception:
                content = f"(文件读取失败: {file_path.name})"
            if not content:
                continue
            if is_latest:
                raw_parts.append(f"## 最新汇总\n\n{content}")
            else:
                mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                raw_parts.append(f"## 创建时间: {mtime.strftime('%y/%m/%d %H:%M')}\n\n{content}")

        if not raw_parts:
            if notify:
                yield event.plain_result("⚠️ 所有汇总文件为空")
            event.stop_event()
            return

        raw_content = "\n\n---\n\n".join(raw_parts)
        source_info = self._get_source_info(event) if notify else None

        try:
            await self._send_raw_file(target_umo, raw_content, source_info=source_info)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 发送原始日志到 {target_id} 失败: {e}")
            if notify:
                yield event.plain_result(f"⚠️ 发送失败: {e}")
            event.stop_event()
            return

        if notify:
            yield event.plain_result("✅ 原始日志发送完成")
        event.stop_event()

    # -----------------------------------------------------------------------
    # /receivemd — 反向 AI 摘要
    # -----------------------------------------------------------------------

    @filter.command("receivemd")
    async def cmd_receivemd(
        self, event: AstrMessageEvent,
        source_id: str,
        mode: str = "public",
        coupling_count: int = 1,
    ):
        """从源会话生成 AI 摘要，发送到当前会话（与 sendmd 方向相反）。

        source_id: 源群号/QQ号
        mode: public=显示进度通知+来源标注，private=完全静默
        coupling_count: 耦合历史文件数，默认 1（当前+前1个日志）
        """
        session_key = self._resolve_session_key(event)
        current_umo = event.unified_msg_origin

        # 确定源会话类型
        all_known_groups = [str(g) for g in self.config.get("enable_groups", [])] + [str(g) for g in self.config.get("forward_groups", [])]
        source_type = "group" if source_id in all_known_groups else "private"
        source_session = f"{source_type}_{source_id}"
        source_umo = self._build_target_umo(source_id, source_type)

        notify = self._should_notify(mode)
        if notify:
            yield event.plain_result("🔄 正在生成摘要，请稍候...")

        # 生成摘要（从源会话的文件）
        summary_text, files, prompt_chars = await self._generate_summary(source_session, coupling_count=coupling_count)
        if summary_text is None:
            if notify:
                yield event.plain_result("⚠️ 源会话无内容可摘要")
            event.stop_event()
            return

        source_info = f"来源: 群 {source_id}" if source_type == "group" else f"来源: 私聊 {source_id}" if notify else None

        try:
            await self._send_summary_result(current_umo, summary_text, files, source_info=source_info)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 发送摘要失败: {e}")
            if notify:
                yield event.plain_result(f"⚠️ 发送失败: {e}")
            event.stop_event()
            return

        # 源会话创建新活跃文件
        await self._create_new_active_file(source_session)

        if notify:
            est_tokens = prompt_chars // 2
            yield event.plain_result(f"✅ 摘要已发送（约 {est_tokens:,} tokens）")
        event.stop_event()

    # -----------------------------------------------------------------------
    # /receiveraw — 反向原始日志
    # -----------------------------------------------------------------------

    @filter.command("receiveraw")
    async def cmd_receiveraw(
        self, event: AstrMessageEvent,
        source_id: str,
        mode: str = "public",
        coupling_count: int = 1,
    ):
        """从源会话获取原始日志，发送到当前会话（与 sendraw 方向相反）。

        source_id: 源群号/QQ号
        mode: public=显示进度通知+来源标注，private=完全静默
        coupling_count: 耦合历史文件数，默认 1（当前+前1个日志）
        """
        current_umo = event.unified_msg_origin

        # 确定源会话类型
        all_known_groups = [str(g) for g in self.config.get("enable_groups", [])] + [str(g) for g in self.config.get("forward_groups", [])]
        source_type = "group" if source_id in all_known_groups else "private"
        source_session = f"{source_type}_{source_id}"

        notify = self._should_notify(mode)
        if notify:
            yield event.plain_result("🔄 正在准备原始日志...")

        files = await self._get_summary_files(source_session, coupling_count)
        if not files:
            if notify:
                yield event.plain_result("⚠️ 源会话无汇总文件可发送")
            event.stop_event()
            return

        raw_parts = []
        for file_path, is_latest in files:
            try:
                content = file_path.read_text(encoding="utf-8").strip()
            except Exception:
                content = f"(文件读取失败: {file_path.name})"
            if not content:
                continue
            if is_latest:
                raw_parts.append(f"## 最新汇总\n\n{content}")
            else:
                mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                raw_parts.append(f"## 创建时间: {mtime.strftime('%y/%m/%d %H:%M')}\n\n{content}")

        if not raw_parts:
            if notify:
                yield event.plain_result("⚠️ 源会话所有汇总文件为空")
            event.stop_event()
            return

        raw_content = "\n\n---\n\n".join(raw_parts)
        source_info = f"来源: 群 {source_id}" if source_type == "group" else f"来源: 私聊 {source_id}" if notify else None

        try:
            await self._send_raw_file(current_umo, raw_content, source_info=source_info)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 发送原始日志失败: {e}")
            if notify:
                yield event.plain_result(f"⚠️ 发送失败: {e}")
            event.stop_event()
            return

        if notify:
            yield event.plain_result("✅ 原始日志已发送")
        event.stop_event()

    # -----------------------------------------------------------------------
    # /generate — 批量生成并静默转发
    # -----------------------------------------------------------------------

    @filter.command("generate")
    async def cmd_generate(self, event: AstrMessageEvent, output_type: str = "md"):
        """对所有启用群聊生成摘要，静默转发到配置的转发目标。

        output_type: "md"=AI摘要, "raw"=原始日志
        """
        forward_groups = [str(g) for g in self.config.get("forward_groups", [])]
        forward_private = [str(u) for u in self.config.get("forward_private", [])]

        if not forward_groups and not forward_private:
            yield event.plain_result("⚠️ 未配置转发目标群号/QQ号，请先在插件配置中设置 forward_groups 或 forward_private")
            event.stop_event()
            return

        enable_groups = [str(g) for g in self.config.get("enable_groups", [])]
        if not enable_groups:
            yield event.plain_result("⚠️ 未配置启用群聊，请先在插件配置中设置 enable_groups")
            event.stop_event()
            return

        success_count = 0
        for group_id in enable_groups:
            session_key = f"group_{group_id}"

            if output_type == "raw":
                files = await self._get_summary_files(session_key, coupling_count=1)
                if not files:
                    logger.info(f"[{PLUGIN_NAME}] /generate raw: {session_key} 无文件，跳过")
                    continue
                raw_parts = []
                for file_path, is_latest in files:
                    try:
                        content = file_path.read_text(encoding="utf-8").strip()
                    except Exception:
                        content = f"(文件读取失败: {file_path.name})"
                    if not content:
                        continue
                    if is_latest:
                        raw_parts.append(f"## 最新汇总\n\n{content}")
                    else:
                        mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                        raw_parts.append(f"## 创建时间: {mtime.strftime('%y/%m/%d %H:%M')}\n\n{content}")
                if not raw_parts:
                    continue
                raw_content = "\n\n---\n\n".join(raw_parts)
                await self._forward_raw_to_targets(raw_content)
                await self._create_new_active_file(session_key)
                success_count += 1
            else:
                summary_text, files, _ = await self._generate_summary(session_key, coupling_count=1)
                if summary_text is None:
                    logger.info(f"[{PLUGIN_NAME}] /generate: {session_key} 无内容，跳过")
                    continue
                await self._forward_to_targets(summary_text, files)
                await self._create_new_active_file(session_key)
                success_count += 1

        type_label = "原始日志" if output_type == "raw" else "摘要"
        logger.info(f"[{PLUGIN_NAME}] /generate: 已为 {success_count}/{len(enable_groups)} 个群生成{type_label}并转发")
        event.stop_event()

    # -----------------------------------------------------------------------
    # /checksize — 查看汇总文件大小和行数
    # -----------------------------------------------------------------------

    @filter.command("checksize")
    async def cmd_checksize(
        self, event: AstrMessageEvent,
        target_id: str = "0",
        coupling_count: int = 0,
    ):
        """查看目标会话的汇总文件统计信息。

        target_id: 目标群号/QQ号，0=当前会话
        coupling_count: 耦合历史文件数，默认 0（仅当前活跃文件）
        """
        if target_id == "0":
            session_key = self._resolve_session_key(event)
        else:
            all_known_groups = [str(g) for g in self.config.get("enable_groups", [])] + [str(g) for g in self.config.get("forward_groups", [])]
            target_type = "group" if target_id in all_known_groups else "private"
            session_key = f"{target_type}_{target_id}"

        files = await self._get_summary_files(session_key, coupling_count)
        if not files:
            yield event.plain_result(f"📊 会话 {target_id} 无汇总文件")
            event.stop_event()
            return

        total_size = 0
        total_lines = 0
        file_count = len(files)

        for file_path, is_latest in files:
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                content = ""
            total_size += file_path.stat().st_size
            total_lines += len([l for l in content.split("\n") if l.strip()])

        size_kb = total_size / 1024
        if size_kb >= 1024:
            size_str = f"{size_kb / 1024:.1f} MB"
        else:
            size_str = f"{size_kb:.1f} KB"

        msg = (
            f"📊 会话 {target_id} 汇总统计\n"
            f"文件数: {file_count}\n"
            f"总大小: {size_str}\n"
            f"总行数: {total_lines:,} 行"
        )
        yield event.plain_result(msg)
        event.stop_event()

    # -----------------------------------------------------------------------
    # /forcecut — 强制分割汇总文件
    # -----------------------------------------------------------------------

    @filter.command("forcecut")
    async def cmd_forcecut(
        self, event: AstrMessageEvent,
        target_id: str = "0",
    ):
        """强制结束当前活跃汇总文件，创建新的空白文件。

        target_id: 目标群号/QQ号，0=当前会话
        """
        if target_id == "0":
            session_key = self._resolve_session_key(event)
        else:
            all_known_groups = [str(g) for g in self.config.get("enable_groups", [])] + [str(g) for g in self.config.get("forward_groups", [])]
            target_type = "group" if target_id in all_known_groups else "private"
            session_key = f"{target_type}_{target_id}"

        old_path = await self._get_active_file_path(session_key)
        await self._create_new_active_file(session_key)

        if old_path:
            yield event.plain_result(f"✅ 已分割，旧文件: {old_path.name}")
        else:
            yield event.plain_result("✅ 已创建新汇总文件（此前无活跃文件）")
        event.stop_event()

    # -----------------------------------------------------------------------
    # 已有的查询/管理命令
    # -----------------------------------------------------------------------

    @filter.command("digest_status")
    async def cmd_digest_status(self, event: AstrMessageEvent):
        """查看当前群/私聊的采集状态。"""
        msg_obj = event.message_obj
        is_group = bool(msg_obj.group_id)

        if is_group:
            session_key = f"group_{msg_obj.group_id}"
        else:
            user_id = str(msg_obj.sender.user_id) if msg_obj.sender else "unknown"
            session_key = f"private_{user_id}"

        active_path = await self._get_active_file_path(session_key)
        if active_path is None:
            yield event.plain_result("📊 当前无活跃汇总文件，发送第一条消息后自动创建。")
            event.stop_event()
            return

        try:
            content = active_path.read_text(encoding="utf-8")
            line_count = len([l for l in content.split("\n") if l.strip()])
            char_count = len(content)
            file_name = active_path.name
        except Exception:
            yield event.plain_result("📊 无法读取汇总文件。")
            event.stop_event()
            return

        # 统计历史文件数
        session_dir = self._get_session_dir(session_key)
        all_files = list(session_dir.glob("*.md"))
        history_count = len(all_files) - 1

        msg = (
            f"📊 采集状态\n"
            f"活跃文件: {file_name}\n"
            f"消息条数: {line_count}\n"
            f"总字符数: {char_count}\n"
            f"历史文件: {history_count} 个"
        )
        yield event.plain_result(msg)
        event.stop_event()

    @filter.command("digest_clear")
    async def cmd_digest_clear(self, event: AstrMessageEvent):
        """清空当前群/私聊的所有汇总文件。"""
        msg_obj = event.message_obj
        is_group = bool(msg_obj.group_id)

        if is_group:
            session_key = f"group_{msg_obj.group_id}"
        else:
            user_id = str(msg_obj.sender.user_id) if msg_obj.sender else "unknown"
            session_key = f"private_{user_id}"

        session_dir = self._get_session_dir(session_key)
        deleted = 0
        for md_file in session_dir.glob("*.md"):
            try:
                md_file.unlink()
                deleted += 1
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 删除文件失败 {md_file}: {e}")

        await self.put_kv_data(f"active_file_{session_key}", "")
        yield event.plain_result(f"🗑️ 已清空 {deleted} 个汇总文件。")
        event.stop_event()

    @filter.command("digest_config")
    async def cmd_digest_config(self, event: AstrMessageEvent):
        """查看当前插件配置。"""
        cfg = self.config
        msg = (
            f"⚙️ 插件配置\n"
            f"启用群聊: {cfg.get('enable_groups', [])}\n"
            f"私聊采集: {cfg.get('enable_private', True)}\n"
            f"转发群: {cfg.get('forward_groups', [])}\n"
            f"转发私聊: {cfg.get('forward_private', [])}\n"
            f"定时摘要: {cfg.get('summary_schedule', '08:00,20:00')}\n"
            f"耦合文件数: {cfg.get('coupling_count', 0)}\n"
            f"采集自己: {cfg.get('self_recursion', False)}\n"
            f"记录昵称: {cfg.get('name_record', True)}\n"
            f"缓存保留: {cfg.get('cache_clean_days', 7)} 天\n"
            f"输出方式: {cfg.get('output_mode', 'image')}\n"
            f"文本权重: {cfg.get('modality', {}).get('text', 100)}"
        )
        yield event.plain_result(msg)
        event.stop_event()