import re
from io import BytesIO

from PIL import Image

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.core.platform.astr_message_event import AstrMessageEvent


def handle_image(image_bytes: bytes) -> bytes:
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            # 如果不是 GIF，直接返回原图
            if img.format != "GIF":
                return image_bytes
            # 处理 GIF
            buf = BytesIO()
            # 判断是否为动画 GIF（多帧）
            if getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1:
                img.seek(0)  # 只取第一帧
            # 单帧 GIF 或者多帧 GIF 的第一帧都走下面的保存逻辑
            img = img.convert("RGBA")
            img.save(buf, format="PNG")
            return buf.getvalue()
    except Exception as e:
        logger.warning(f"GIF 处理失败，返回原图: {e}")
        return image_bytes


def get_image(event: AstrMessageEvent) -> str:
    """遍历消息链，获取第一张图片（Sora网页端点不支持多张图片的视频生成，至少测试的时候是这样）"""
    image_url = ""
    for comp in event.get_messages():
        if isinstance(comp, Comp.Image) and comp.url:
            image_url = comp.url
            break
        elif isinstance(comp, Comp.Reply) and comp.chain:
            for quote in comp.chain:
                if isinstance(quote, Comp.Image) and quote.url:
                    image_url = quote.url
                    break
            if image_url:
                break
    return image_url


def _get_image_orientation(image_bytes: bytes) -> str:
    # 把 bytes 转成图片对象
    img = Image.open(BytesIO(image_bytes))

    width, height = img.size
    if width > height:
        return "landscape"
    elif width < height:
        return "portrait"
    else:
        return "portrait"


def get_screen_mode(
    msg, def_prompt, def_screen_mode: str, image_bytes: bytes | None
) -> tuple[str, str]:
    """解析用户文本，获取视频提示词和横竖屏设置"""
    # 解析参数
    msg_reg = re.match(
        r"^(?:生成视频|sora)(?:\s+(横屏|竖屏)?\s*([\s\S]*))?$",
        msg,
    )
    # 提取提示词
    prompt = msg_reg.group(2).strip() if msg_reg and msg_reg.group(2) else def_prompt
    # 竖屏还是横屏
    screen_mode = "portrait"
    if msg_reg and msg_reg.group(1):
        params = msg_reg.group(1).strip()
        screen_mode = "landscape" if params == "横屏" else "portrait"
    elif def_screen_mode in ["横屏", "竖屏"]:
        screen_mode = "landscape" if def_screen_mode == "横屏" else "portrait"
    elif def_screen_mode == "自动" and image_bytes:
        screen_mode = _get_image_orientation(image_bytes)
    return prompt, screen_mode
