import asyncio
import base64
import hashlib
import json
import os
import time
from uuid import uuid4

from curl_cffi import AsyncSession, CurlMime, ProxySpec, requests
from curl_cffi.requests.exceptions import Timeout

from astrbot.api import logger

from .utils import handle_image

# 轮询参数
MAX_INTERVAL = 60  # 最大间隔
MIN_INTERVAL = 5  # 最小间隔
TOTAL_WAIT = 600  # 最多等待10分钟


class SoraAPI:
    def __init__(
        self,
        sora_base_url: str,
        chatgpt_base_url: str,
        proxy: str,
        model_config: dict,
        video_data_dir: str,
        get_not_watermark_url: str,
    ):
        self.sora_base_url = sora_base_url
        self.chatgpt_base_url = chatgpt_base_url
        self.proxies: ProxySpec | None = (
            {"http": proxy, "https": proxy} if proxy else None
        )
        self.session = AsyncSession(impersonate="chrome136")
        self.model_config = model_config
        self.video_data_dir = video_data_dir
        self.get_not_watermark_url = get_not_watermark_url

    async def download_image(self, url: str) -> tuple[bytes | None, str | None]:
        try:
            response = await self.session.get(url)
            content = handle_image(response.content)
            return content, None
        except (
            requests.exceptions.SSLError,
            requests.exceptions.CertificateVerifyError,
        ):
            # 关闭SSL验证
            response = await self.session.get(url, verify=False)
            content = handle_image(response.content)
            return content, None
        except Timeout as e:
            logger.error(f"网络请求超时: {e}")
            return None, "下载图片失败：网络请求超时，请检查网络连通性"
        except Exception as e:
            logger.error(f"下载图片失败: {e}")
            return None, "下载图片失败"

    async def upload_images(
        self, authorization: str, image_bytes: bytes
    ) -> tuple[str | None, str | None]:
        try:
            mp = CurlMime()
            mp.addpart(
                name="file",
                filename=f"{int(time.time() * 1000)}.png",
                content_type="image/png",
                data=image_bytes,
            )
            response = await self.session.post(
                self.sora_base_url + "/backend/uploads",
                multipart=mp,
                headers={"Authorization": authorization},
                proxies=self.proxies,
            )
            result = response.json()
            if response.status_code == 200:
                return result.get("id"), None
            else:
                err_str = f"上传图片失败: {result.get('error', {}).get('message')}"
                err_code = result.get("error", {}).get("code")
                logger.error(err_str)
                if err_code == "token_expired":
                    return None, "token_expired"
                return None, err_str
        except Timeout as e:
            logger.error(f"网络请求超时: {e}")
            return None, "上传图片失败：网络请求超时，请检查网络连通性"
        except Exception as e:
            logger.error(f"上传图片失败: {e}")
            return None, "上传图片失败"
        finally:
            mp.close()

    async def get_sentinel(self) -> tuple[str | None, str | None]:
        # 随便生成一个哈希值作为PoW证明，反正服务器也不验证，留空都可以
        uuid = str(uuid4())
        random_bytes = uuid.encode()
        stoken = base64.b64encode(hashlib.sha256(random_bytes).digest()).decode()
        flow = "sora_2_create_task"
        payload = {"flow": flow, "id": uuid, "p": stoken}
        try:
            response = await self.session.post(
                self.chatgpt_base_url + "/backend-api/sentinel/req",
                json=payload,
                proxies=self.proxies,
            )
            if response.status_code == 200:
                result = response.json()
                # 组装Sentinel token
                sentinel_token = {
                    "p": stoken,
                    "t": result.get("turnstile", {}).get("dx", ""),
                    "c": result.get("token", ""),
                    "id": uuid,
                    "flow": flow,
                }
                return json.dumps(sentinel_token), None
            else:
                logger.error(f"获取Sentinel tokens失败: {response.text[:100]}")
                return None, "获取Sentinel tokens失败"
        except Timeout as e:
            logger.error(f"网络请求超时: {e}")
            return None, "获取Sentinel tokens失败：网络请求超时，请检查网络连通性"
        except Exception as e:
            logger.error(f"获取Sentinel tokens失败: {e}")
            return None, "获取Sentinel tokens失败"

    async def create_video(
        self, prompt: str, screen_mode: str, image_id: str, authorization: str
    ) -> tuple[str | None, str | None]:
        sentinel_token, err = await self.get_sentinel()
        if err:
            return None, err
        inpaint_items = [{"kind": "upload", "upload_id": image_id}] if image_id else []
        payload = {
            "kind": "video",
            "prompt": prompt,
            "title": None,
            "orientation": screen_mode,
            "size": self.model_config.get("size", "small"),
            "n_frames": self.model_config.get("n_frames", 300),
            "inpaint_items": inpaint_items,
            "remix_target_id": None,
            "cameo_ids": None,
            "cameo_replacements": None,
            "model": self.model_config.get("model", "sy_8"),
            "style_id": None,
            "audio_caption": None,
            "audio_transcript": None,
            "video_caption": None,
            "storyboard_id": None,
        }
        try:
            response = await self.session.post(
                self.sora_base_url + "/backend/nf/create",
                json=payload,
                headers={
                    "Authorization": authorization,
                    "openai-sentinel-token": sentinel_token,
                },
                proxies=self.proxies,
            )
            result = response.json()
            if response.status_code == 200:
                return result.get("id"), None
            else:
                err_str = f"提交任务失败: {result.get('error', {}).get('message')}"
                err_code = result.get("error", {}).get("code")
                logger.error(f"{err_str}，Token: {authorization[-16:]}")
                if err_code == "token_expired":
                    return None, "token_expired"
                return None, err_str
        except Timeout as e:
            logger.error(f"网络请求超时: {e}")
            return None, "提交任务失败：网络请求超时，请检查网络连通性"
        except Exception as e:
            logger.error(f"提交任务失败: {e}")
            return None, "提交任务失败"

    async def pending_video(
        self, task_id: str, authorization: str
    ) -> tuple[str | None, str | None, float]:
        try:
            response = await self.session.get(
                self.sora_base_url + "/backend/nf/pending",
                headers={"Authorization": authorization},
                proxies=self.proxies,
            )
            if response.status_code == 200:
                result = response.json()
                for item in result:
                    if item.get("id") == task_id:
                        return item.get("status"), None, item.get("progress_pct") or 0
                return (
                    "Done",
                    None,
                    0,
                )  # "Done"表示任务队列状态结束，至于任务是否成功，不知道
            else:
                result = response.json()
                err_str = f"视频状态查询失败: {result.get('error', {}).get('message')}"
                logger.error(err_str)
                return "Failed", result.get("error", {}).get("message"), 0
        except Timeout as e:
            logger.error(f"网络请求超时: {e}")
            return "Timeout", "视频状态查询失败：网络请求超时，请检查网络连通性", 0
        except Exception as e:
            logger.error(f"视频状态查询失败: {e}")
            return "EXCEPTION", "视频状态查询失败", 0

    async def poll_pending_video(
        self, task_id: str, authorization: str
    ) -> tuple[str, str | None]:
        """轮询等待视频生成完成"""
        interval = MAX_INTERVAL
        elapsed = 0  # 已等待时间
        timeout_num = 0  # 超时次数
        failed_num = 0  # 失败次数
        # 对于首次查询，直接等待30秒，避免生成被截断，干等过长时间
        await asyncio.sleep(30)
        elapsed += 30
        while elapsed < TOTAL_WAIT:
            status, err, progress = await self.pending_video(task_id, authorization)
            if status == "Done":
                # "Done"表示任务队列状态结束，至于任务是否成功，不知道
                return "Done", None
            elif status == "Failed":
                # 这个错误通常不是审查截断引起的，可能是服务器问题，重试几次
                failed_num += 1
                if failed_num > 3:
                    return (
                        "Failed",
                        f"视频状态查询失败，ID: {task_id}，进度: {progress * 100:.2f}%，错误: {err}",
                    )
            elif status == "Timeout":
                # 前面都过了，这里不太可能超时，但是处理一下吧
                timeout_num += 1
                if timeout_num > 3:
                    return (
                        "Timeout",
                        f"视频状态查询失败，ID: {task_id}，进度: {progress * 100:.2f}%，网络连接超时",
                    )
            elif status == "EXCEPTION":
                # 程序错误，直接返回
                return (
                    "EXCEPTION",
                    f"视频状态查询程序错误，ID: {task_id}，请前往控制台查看",
                )
            # 等待当前轮询间隔
            wait_time = min(interval, TOTAL_WAIT - elapsed)
            await asyncio.sleep(wait_time)
            elapsed += wait_time
            # 反向指数退避：间隔逐步减小
            interval = max(MIN_INTERVAL, interval // 2)
            logger.debug(
                f"视频处理中，{interval}s 后再次请求... 进度: {progress * 100:.2f}%"
            )
        logger.error("视频状态查询超时")
        return (
            "Timeout",
            f"视频状态查询超时，ID: {task_id}，生成进度: {progress * 100:.2f}%",
        )

    async def get_video_by_web(
        self, task_id: str, authorization: str
    ) -> tuple[str, str | None, str | None, str | None]:
        try:
            response = await self.session.get(
                self.sora_base_url + "/backend/project_y/profile/drafts?limit=15",
                headers={"Authorization": authorization},
                proxies=self.proxies,
            )
            if response.status_code == 200:
                result = response.json()
                for item in result.get("items", []):
                    if item.get("task_id") == task_id:
                        downloadable_url = item.get("downloadable_url")
                        if not downloadable_url:
                            err_str = (
                                item.get("reason_str")
                                or item.get("error_reason")
                                or "未知错误"
                            )
                            logger.error(
                                f"生成视频失败, task_id: {task_id}, sora_reason: {err_str}"
                            )
                            return (
                                "Failed",
                                None,
                                item.get("id"),
                                err_str,
                            )
                        return "Done", downloadable_url, item.get("id"), None
                return "NotFound", None, None, "未找到对应的视频"
            else:
                result = response.json()
                err_str = f"获取视频链接失败: {result.get('error', {}).get('message')}"
                logger.error(err_str)
                return "ServerError", None, None, err_str
        except Timeout as e:
            logger.error(f"网络请求超时: {e}")
            return (
                "Timeout",
                None,
                None,
                "获取视频链接失败：网络请求超时，请检查网络连通性",
            )
        except Exception as e:
            logger.error(f"获取视频链接失败: {e}")
            return "EXCEPTION", None, None, "获取视频链接失败"

    async def download_video(
        self, video_url: str, task_id: str
    ) -> tuple[str | None, str | None]:
        try:
            logger.debug(f"正在下载视频: {video_url}")
            response = await self.session.get(video_url, proxies=self.proxies)
            if response.status_code == 200:
                # 保存视频内容到本地文件
                video_path = os.path.join(self.video_data_dir, f"{task_id}.mp4")
                with open(video_path, "wb") as f:
                    f.write(response.content)
                return video_path, None
            else:
                return None, f"下载视频失败: {response.status_code}"
        except Timeout as e:
            logger.error(f"网络请求超时: {e}")
            return None, "下载视频失败：网络请求超时，请检查网络连通性"
        except Exception as e:
            logger.error(f"下载视频失败: {e}")
            return None, "下载视频失败"

    def delete_video(self, task_id: str) -> None:
        """删除视频文件，仅传递任务ID"""
        try:
            video_path = os.path.join(self.video_data_dir, f"{task_id}.mp4")
            if os.path.exists(video_path):
                os.remove(video_path)
                logger.debug(f"已删除视频文件：{task_id}")
            else:
                logger.warning(f"删除视频失败: 视频文件不存在：{video_path}")
        except Exception as e:
            logger.error(f"删除视频失败: {e}")

    async def check_token_validity(self, authorization: str) -> str:
        try:
            response = await self.session.get(
                self.sora_base_url + "/backend/nf/pending",
                headers={"Authorization": authorization},
                proxies=self.proxies,
            )
            if response.status_code == 200:
                return "Success"
            else:
                result = response.json()
                err_str = f"Token {authorization[-16:]} 无效: {result.get('error', {}).get('message')}"
                logger.error(err_str)
                return "Invalid"
        except Timeout as e:
            logger.error(f"网络请求超时: {e}")
            return "Timeout"
        except Exception as e:
            logger.error(f"程序错误: {e}")
            return "EXCEPTION"

    async def refresh_access_token(
        self, sessionToken: str
    ) -> tuple[str | None, str | None, str | None]:
        headers = {
            "Origin": "https://sora.chatgpt.com",
            "Referer": "https://sora.chatgpt.com/",
            "Cookie": f"__Secure-next-auth.session-token={sessionToken}",
        }
        try:
            response = await self.session.get(
                self.chatgpt_base_url + "/api/auth/session",
                headers=headers,
                proxies=self.proxies,
            )
            result = response.json()
            if response.status_code == 200:
                access_token = result.get("accessToken")
                expires = result.get("expires")
                if access_token:
                    return access_token, expires, None
                else:
                    logger.error("刷新AccessToken失败: 未获取到AccessToken")
                    return None, None, "刷新AccessToken失败"
            else:  # 可能是请求失败，鉴权失效不会报错
                err_str = (
                    f"刷新AccessToken失败: {result.get('error', {}).get('message')}"
                )
                logger.error(err_str)
                return None, None, None
        except Timeout as e:
            logger.error(f"网络请求超时: {e}")
            return None, None, None
        except Exception as e:
            logger.error(f"刷新AccessToken失败: {e}")
            return None, None, None

    async def _post_video(
        self, authorization: str, generation_id: str
    ) -> tuple[str | None, str | None]:
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://sora.chatgpt.com",
            "Referer": "https://sora.chatgpt.com/",
            "Authorization": authorization,
        }
        payload = {
            "attachments_to_create": [{"generation_id": generation_id, "kind": "sora"}],
            "post_text": "",
        }
        try:
            response = await self.session.post(
                self.sora_base_url + "/backend/project_y/post",
                headers=headers,
                json=payload,
                proxies=self.proxies,
            )
            result = response.json()
            if response.status_code == 200:
                permalink = result.get("post", {}).get("permalink")
                if not permalink:
                    logger.error("发布视频失败: 未获取到分享链接")
                    return None, "发布视频失败: 未获取到分享链接"
                return permalink, None
            else:
                err_str = f"发布视频失败: {result.get('error', {}).get('message')}"
                logger.error(err_str)
                return None, err_str
        except Exception as e:
            logger.error(f"发布视频失败: {e}")
            return None, "发布视频失败"

    async def get_not_watermark(
        self, authorization: str, generation_id: str
    ) -> tuple[str | None, str | None]:
        permalink, err = await self._post_video(authorization, generation_id)
        if err:
            return None, err
        payload = {
            "url": permalink,
            "token": None,
        }
        try:
            response = await self.session.post(
                self.get_not_watermark_url,
                json=payload,
            )
            result = response.json()
            if response.status_code == 200:
                download_link = result.get("download_link")
                if not download_link:
                    logger.error("获取无水印视频失败: 未获取到无水印链接")
                    return None, "获取无水印视频失败: 未获取到无水印链接"
                return download_link, None
            else:
                err_str = f"获取无水印视频失败，状态码: {response.status_code}，响应: {response.text[:100]}"
                logger.error(err_str)
                return None, "获取无水印视频失败"
        except Exception as e:
            logger.error(f"获取无水印视频失败: {e}")
            return None, "获取无水印视频失败"

    async def close(self):
        await self.session.close()
