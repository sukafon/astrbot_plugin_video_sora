import asyncio
import os
import random
import re

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Video
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult

from .database import Database
from .sora_api import SoraAPI
from .utils import get_image, get_screen_mode

# 获取视频下载地址
MAX_WAIT = 30  # 最大等待时间（秒）
INTERVAL = 3  # 每次轮询间隔（秒）


class VideoSora(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config  # 读取配置文件

        # 水印配置
        watermark_config = self.config.get("watermark_config", {})
        self.not_watermark = watermark_config.get("not_watermark", False)
        self.get_not_watermark_url = watermark_config.get("get_not_watermark_url", "")

        # Sora基本参数
        sora_base_url = self.config.get("sora_base_url", "https://sora.chatgpt.com")
        chatgpt_base_url = self.config.get("chatgpt_base_url", "https://chatgpt.com")
        self.proxy = self.config.get("proxy", "")
        model_config = self.config.get("model_config", {})
        self.speed_down_url_type = self.config.get("speed_down_url_type", "")
        self.speed_down_url = self.config.get("speed_down_url", "")
        self.save_video_enabled = self.config.get("save_video_enabled", False)
        self.video_data_dir = os.path.join(
            StarTools.get_data_dir("astrbot_plugin_video_sora"), "videos"
        )
        # 实例化SoraAPI
        self.SoraAPI = SoraAPI(
            sora_base_url,
            chatgpt_base_url,
            self.proxy,
            model_config,
            self.video_data_dir,
            self.get_not_watermark_url,
        )

        # 动态参数
        self.screen_mode = self.config.get("screen_mode", "自动")
        self.def_prompt = self.config.get("default_prompt", "生成一个多镜头视频")

        # 鉴权信息
        token_config = self.config.get("token_config", {})
        self.token_type = token_config.get("token_type", "SessionToken")
        self.token_list = token_config.get("token_list", [])

        # 并发限制
        self.polling_task = set()
        self.task_limit = int(self.config.get("task_limit", 3))

        # 群白名单
        self.group_whitelist_enabled = self.config.get("group_whitelist_enabled", False)
        self.group_whitelist = self.config.get("group_whitelist", [])

        # 单线程锁
        self.lock = asyncio.Lock()

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        # 创建视频缓存文件路径
        os.makedirs(self.video_data_dir, exist_ok=True)
        # 数据库文件路径
        video_db_path = (
            StarTools.get_data_dir("astrbot_plugin_video_sora") / "video_data.db"
        )
        # 实例化数据库类
        self.database = Database(video_db_path)

        # 构建一个以用户所填Token的后16位为key的字典，记录AccessToken和使用统计等信息
        self.token_dict: dict[str, dict] = {}
        # 初始化这个字典
        for token in self.token_list:
            token_key = token[-16:]
            self.token_dict[token_key] = {
                "session_token": token if self.token_type == "SessionToken" else None,
                "access_token": token if self.token_type == "AccessToken" else None,
                "used_count_today": 0,
                "concurrency_count": 0,
                "rate_limit_reached": False,
                "token_state": 1,
            }

        # 从数据库中加载持久化数据
        tokens = [k[-16:] for k in self.token_list]
        rows = self.database.load_token_data(tokens)
        for token_key, access_token, used_count_today, rate_limit_reached in rows:
            # 如果数据库中有未配置或已被删除的 token，则跳过并记录日志，防止 KeyError
            if token_key not in self.token_dict:
                logger.warning(f"[sora插件初始化] {token_key} 未在配置中存在，已跳过")
                continue
            # 将数据库里的 access_token 映射填入字典
            if self.token_type == "SessionToken":
                self.token_dict[token_key]["access_token"] = access_token or None
            self.token_dict[token_key]["used_count_today"] = used_count_today or 0
            self.token_dict[token_key]["rate_limit_reached"] = bool(rate_limit_reached)

        # 创建一个token_key列表，用于优化遍历性能
        self.token_key_list = list(self.token_dict.keys())

        # 检查配置是否已经关闭函数工具
        if not self.config.get("llm_tool_enabled", False):
            StarTools.unregister_llm_tool("sora_video_generation")
            logger.info("已删除函数调用工具: sora_video_generation")

    async def concurrence_lock(self, token_key: str, is_add: bool):
        """一个确保计数安全的小锁"""
        async with self.lock:
            if is_add:
                self.token_dict[token_key]["concurrency_count"] += 1
            else:
                self.token_dict[token_key]["concurrency_count"] -= 1

    async def queue_task(
        self,
        event: AstrMessageEvent,
        task_id: str,
        authorization: str,
        is_check=False,
    ) -> tuple[str | None, str | None]:
        """完成视频生成并返回视频链接或者错误信息"""

        # 检查是否已经有相同的任务在处理
        if task_id in self.polling_task:
            status, _, progress = await self.SoraAPI.pending_video(
                task_id, authorization
            )
            return (
                None,
                f"⏳ 任务还在队列中，请稍后再看~\n状态：{status} 进度: {progress * 100:.2f}%",
            )
        # 优化人机交互
        if is_check:
            status, err, progress = await self.SoraAPI.pending_video(
                task_id, authorization
            )
            if err:
                return None, err
            if status != "Done":
                await event.send(
                    MessageChain(
                        [
                            Comp.Reply(id=event.message_obj.message_id),
                            Comp.Plain(
                                f"⏳ 任务仍在队列中，请稍后再看~\n状态：{status} 进度: {progress * 100:.2f}%"
                            ),
                        ]
                    )
                )
            else:
                logger.debug("队列状态完成，正在查询视频直链...")

        # 记录正在处理的任务
        try:
            self.polling_task.add(task_id)

            # 等待视频生成
            result, err = await self.SoraAPI.poll_pending_video(task_id, authorization)

            # 更新任务进度
            self.database.update_poll_finished_data(task_id, result, err)

            if result != "Done" or err:
                return None, err

            elapsed = 0
            status = "Done"
            video_url = ""
            generation_id = None
            err = None
            # 获取视频下载地址
            while elapsed < MAX_WAIT:
                # 通过web端点获取视频链接或者失败原因
                (
                    status,
                    video_url,
                    generation_id,
                    err,
                ) = await self.SoraAPI.get_video_by_web(task_id, authorization)
                if video_url or status in {"Failed", "EXCEPTION"}:
                    break
                await asyncio.sleep(INTERVAL)
                elapsed += INTERVAL

            # 获取无水印视频链接
            if (
                not err
                and self.not_watermark
                and generation_id
                and self.get_not_watermark_url
            ):
                not_watermark_url, err = await self.SoraAPI.get_not_watermark(
                    authorization, generation_id
                )
                if not_watermark_url:
                    video_url = not_watermark_url

            # 更新视频链接数据
            self.database.update_video_url_data(
                task_id, status, video_url, generation_id, err
            )

            # 把错误信息返回给调用者
            if not video_url:
                return None, err or "生成视频超时"

            return video_url, None
        finally:
            if is_check:
                self.polling_task.remove(task_id)

    async def create_video(
        self,
        event: AstrMessageEvent,
        image_url: str | None,
        image_bytes: bytes | None,
        prompt: str,
        screen_mode: str,
        authorization: str,
        token_key: str,
    ) -> tuple[str | None, str | None]:
        """创建视频生成任务流程"""
        # 如果消息中携带图片，上传图片到OpenAI端点
        images_id = ""
        if image_bytes:
            images_id, err = await self.SoraAPI.upload_images(
                authorization, image_bytes
            )
            if not images_id or err:
                return None, err

        # 生成视频
        task_id, err = await self.SoraAPI.create_video(
            prompt, screen_mode, images_id, authorization
        )
        if not task_id or err:
            return None, err

        # 记录任务数据
        self.database.insert_video_data(
            task_id,
            event.message_obj.sender.user_id,
            event.message_obj.sender.nickname,
            prompt,
            image_url,
            event.message_obj.message_id,
            token_key,
        )
        # 返回结果
        return task_id, None

    async def handle_video_chain(
        self, event: AstrMessageEvent, task_id: str, video_url: str
    ) -> tuple[MessageEventResult | None, str | None]:
        """处理视频组件消息"""

        # 处理反向代理
        if self.speed_down_url_type == "拼接":
            video_url = self.speed_down_url + video_url
        else:
            video_url = re.sub(r"^(https?://[^/]+)", self.speed_down_url, video_url)

        # 下载视频到本地
        if self.proxy or self.save_video_enabled:
            video_path = os.path.join(self.video_data_dir, f"{task_id}.mp4")
            # 先检查文件路径是否有视频文件
            if not os.path.exists(video_path):
                video_path, err_msg = await self.SoraAPI.download_video(
                    video_url, task_id
                )
            # 如果设置了正向代理，则上报本地文件路径
            if self.proxy:
                if err_msg:
                    return None, err_msg
                return event.chain_result([Video.fromFileSystem(video_path)]), None
        return event.chain_result([Video.fromURL(video_url)]), None

    async def check_permission(self, event: AstrMessageEvent) -> bool:
        """检查插件使用权限"""
        # 检查群是否在白名单中
        if (
            self.group_whitelist_enabled
            and event.unified_msg_origin not in self.group_whitelist
        ):
            logger.warning("当前群不在白名单中，无法使用sora视频生成功能")
            return False

        # 检查Token是否存在
        if not self.token_list:
            await event.send(
                MessageChain(
                    [
                        Comp.Reply(id=event.message_obj.message_id),
                        Comp.Plain("❌ 请先在插件配置中添加 Token"),
                    ]
                )
            )
            return False
        return True

    async def video_schedule(
        self,
        event: AstrMessageEvent,
        image_url: str | None,
        image_bytes: bytes | None,
        prompt: str,
        screen_mode: str,
    ):
        """生成视频调度流程，负责账号轮询和Token管理"""
        # 过滤出可用Token
        valid_token_key = [
            k
            for k, v in self.token_dict.items()
            if not v["rate_limit_reached"]
            and v["token_state"] == 1
            and v["concurrency_count"] < self.task_limit
        ]

        if not valid_token_key:
            yield self.build_plain_result(event, "❌ 当前无可用Token，请稍后再试~")
            return

        task_id = ""
        token_key = ""
        authorization = ""
        err = ""

        # 打乱顺序，避免请求过于集中
        random.shuffle(valid_token_key)
        # 尝试循环使用所有可用token
        for token_key in valid_token_key:
            access_token = await self.get_access_token(token_key)
            # 若无token，则已经在获取AccessToken时发生错误，跳过
            if not access_token:
                err = "鉴权Token无效或已过期，请检查后重新配置~"
                continue
            authorization = "Bearer " + access_token
            # 调用创建视频的函数
            task_id, err = await self.create_video(
                event,
                image_url,
                image_bytes,
                prompt,
                screen_mode,
                authorization,
                token_key,
            )
            # 仅在第一次使用 AccessToken 的时候处理 AccessToken 无效的问题
            if self.token_type == "session_token" and err == "token_expired":
                access_token = await self.refresh_auth_token(token_key)
                if not access_token:
                    err = "鉴权无效或已过期，请检查后重新配置~"
                    continue
                authorization = "Bearer " + access_token
                # 重新调用一次
                task_id, err = await self.create_video(
                    event,
                    image_url,
                    image_bytes,
                    prompt,
                    screen_mode,
                    authorization,
                    token_key,
                )
            # 如果成功拿到 task_id，则跳出循环
            if task_id:
                # 回复用户
                yield self.build_plain_result(
                    event, f"🎬 正在生成视频，请稍候~\nID: {task_id}"
                )
                break

        # 尝试完所有 token 仍然请求失败
        if not task_id:
            yield self.build_plain_result(
                event, err or "❌ 创建视频任务失败，请稍后再试~"
            )
            return

        try:
            # 记录并发
            await self.concurrence_lock(token_key, is_add=True)
            # 交给queue_task处理，直到返回视频链接或者错误信息
            video_url, err_msg = await self.queue_task(event, task_id, authorization)
            if not video_url:
                yield self.build_plain_result(
                    event, err_msg or "❌ 查询视频生成状态失败"
                )
                return

            # 视频组件
            video_chain, err_msg = await self.handle_video_chain(
                event, task_id, video_url
            )
            if err_msg:
                yield self.build_plain_result(event, err_msg or "❌ 处理视频消息失败")
                return

            # 发送视频
            yield video_chain
            # 删除视频文件，如果没有开启保存视频功能，那么只有在开启self.proxy以后才有可能下载视频
            if not self.save_video_enabled and self.proxy:
                self.SoraAPI.delete_video(task_id)
        finally:
            await self.concurrence_lock(token_key, is_add=False)
            self.polling_task.remove(task_id)

    @filter.command("sora", alias={"生成视频"})
    async def video_sora(self, event: AstrMessageEvent):
        """使用Sora生成视频消息入口，处理用户消息"""
        # 检查权限
        if not await self.check_permission(event):
            return

        # 尝试获取图片
        image_url = get_image(event)
        image_bytes = None
        if image_url:
            image_bytes, err = await self.SoraAPI.download_image(image_url)
            if err:
                yield self.build_plain_result(event, err)
                return

        # 解析提示词和横竖屏设置
        prompt, screen_mode = get_screen_mode(
            event.message_str,
            self.def_prompt,
            self.screen_mode,
            image_bytes,
        )

        # 进入生成视频调度流程
        async for result in self.video_schedule(
            event, image_url, image_bytes, prompt, screen_mode
        ):
            yield result

    @filter.command("sora查询", alias={"sora强制查询"})
    async def check_video_task(self, event: AstrMessageEvent, task_id: str):
        """
        重放过去生成的视频，或者查询视频生成状态以及重试未完成的生成任务。
        强制查询将绕过数据库缓存，调用接口重新查询任务情况
        """
        # 检查群是否在白名单中
        if not await self.check_permission(event):
            return
        # 从数据库中获取任务信息
        row = self.database.load_video_data(task_id)
        if not row:
            yield event.chain_result(
                [
                    Comp.Reply(id=event.message_obj.message_id),
                    Comp.Plain("❌ 未找到对应的视频任务"),
                ]
            )
            return
        status, video_url, error_msg, auth_xor = row
        is_force_check = event.message_str.startswith("sora强制查询")
        if not is_force_check:
            # 先处理错误
            if status == "Failed":
                yield event.chain_result(
                    [
                        Comp.Reply(id=event.message_obj.message_id),
                        Comp.Plain(error_msg or "❌ 视频生成失败"),
                    ]
                )
                return
            # 有视频，直接发送视频
            if video_url:
                video_comp, err_msg = await self.handle_video_chain(
                    event, task_id, video_url
                )
                if err_msg:
                    yield self.build_plain_result(event, err_msg)
                    return
                yield video_comp
                # 删除视频文件
                if not self.save_video_enabled and self.proxy:
                    self.SoraAPI.delete_video(task_id)
                return
        # 再次尝试完成视频生成
        # 尝试匹配auth_token
        token_key = None
        for key in self.token_key_list:
            if key == auth_xor:
                token_key = key
                break
        if not token_key:
            yield self.build_plain_result(event, "❌ Token不存在，无法查询视频生成状态")
            return
        # 交给queue_task处理，直到返回视频链接或者错误信息
        access_token = await self.get_access_token(token_key)
        # 若无token，则已经在获取access token时处理过错误，跳过
        if not access_token:
            yield self.build_plain_result(
                event, "❌ 鉴权无效或已过期，请检查后重新配置~"
            )
            return
        authorization = "Bearer " + access_token
        video_url, msg = await self.queue_task(
            event, task_id, authorization, is_check=True
        )
        if not video_url:
            yield self.build_plain_result(event, msg or "❌ 查询视频生成状态失败")
            return

        # 视频组件
        video_chain, err_msg = await self.handle_video_chain(event, task_id, video_url)
        if err_msg:
            yield self.build_plain_result(event, err_msg)
            return

        # 发送处理后的视频
        yield video_chain
        # 删除视频文件
        if not self.save_video_enabled and self.proxy:
            self.SoraAPI.delete_video(task_id)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("sora鉴权检测")
    async def check_validity_check(self, event: AstrMessageEvent):
        """测试鉴权有效性"""
        yield event.chain_result(
            [
                Comp.Reply(id=event.message_obj.message_id),
                Comp.Plain("⏳ 正在测试鉴权有效性，请稍候~"),
            ]
        )
        result = "✅ 有效  ❌ 无效  ⌛ 超时  ❓ 错误\n"
        for token_key in self.token_key_list:
            access_token = await self.get_access_token(token_key)
            if not access_token:
                result += f"❌ {token_key}\n"
                continue
            authorization = "Bearer " + access_token
            is_valid = await self.SoraAPI.check_token_validity(authorization)
            if is_valid == "Success":
                result += f"✅ {token_key}\n"
            elif is_valid == "Invalid":
                result += f"❌ {token_key}\n"
            elif is_valid == "Timeout":
                result += f"⌛ {token_key}\n"
            elif is_valid == "EXCEPTION":
                result += f"❓ {token_key}\n"
        yield self.build_plain_result(event, result)

    async def refresh_auth_token(self, token_key: str) -> str | None:
        """刷新鉴权Token的可用状态"""
        if self.token_type != "SessionToken":
            return None

        # 获取完整的SessionToken
        session_token = self.token_dict.get(token_key, {}).get("session_token", None)
        if not session_token:
            logger.error(f"{token_key} 无法刷新 AccessToken，缺少 SessionToken")
            return None
        (
            new_access_token,
            session_token_expire,
            err,
        ) = await self.SoraAPI.refresh_access_token(session_token)
        if err:
            self.database.update_session_token_state(token_key, self.token_type)
            logger.error(f"{token_key} 的 AccessToken 刷新失败")
        if new_access_token and session_token_expire:
            # 更新内存中的AccessToken
            self.token_dict[token_key]["access_token"] = new_access_token
            # 更新数据库中的AccessToken
            self.database.update_access_token_data(
                token_key, self.token_type, new_access_token, session_token_expire
            )
            logger.info(f"{token_key} 的 AccessToken 已刷新")

        return new_access_token

    async def get_access_token(self, token_key: str) -> str | None:
        """获取对应SessionToken的AccessToken"""
        access_token = self.token_dict.get(token_key, {}).get("access_token", None)
        if access_token:
            return access_token
        if self.token_type == "SessionToken":
            return await self.refresh_auth_token(token_key)

    def build_plain_result(
        self, event: AstrMessageEvent, message: str
    ) -> MessageEventResult:
        return event.chain_result(
            [
                Comp.Reply(id=event.message_obj.message_id),
                Comp.Plain(message),
            ]
        )

    @filter.llm_tool(name="sora_video_generation")
    async def sora_tool(self, event: AstrMessageEvent, prompt: str, screen: str):
        """
        A video generation tool, supporting both text-to-video and image-to-video functionalities.
        If the user requests image-to-video generation, you must first verify that the user's
        current message explicitly contains an actual image. References like 'this one' or 'the
        above image' that point to an image in text form are not acceptable. Proceed only if a
        real image is present.

        Args:
            prompt(string): The video generation prompt. Refine the video generation prompt to
                ensure it is clear, detailed, and accurately aligned with the user's intent.
            screen(string): The screen orientation for the video. Must be one of "landscape" or
                "portrait". You may choose a suitable orientation if the user does not specify.
        """

        # 检查权限
        if not await self.check_permission(event):
            return

        # 尝试获取图片
        image_url = get_image(event)
        image_bytes = None
        if image_url:
            image_bytes, err = await self.SoraAPI.download_image(image_url)
            if err:
                return self.build_plain_result(event, err)

        # 使用提供的参数或默认参数
        if not prompt:
            prompt = self.def_prompt
        if not screen:
            screen = self.screen_mode

        # 调用视频生成调度流程
        async for result in self.video_schedule(
            event, image_url, image_bytes, prompt, screen
        ):
            if result:
                await event.send(result)

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        try:
            await self.SoraAPI.close()
            self.database.close()
        except Exception as e:
            logger.error(f"插件卸载时发生错误: {e}")
