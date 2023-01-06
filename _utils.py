# ref: https://github.com/DIYgod/RSSHub/blob/5c7aff76a3a90d6ac5d5e7e139bc182c9c147cb6/lib/v2/weibo/utils.js#L425
import re
import asyncio
from typing import Optional
from yarl import URL

import aiohttp
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import MessageSegment


sinaimgwx_pattern = re.compile(r"(?<=\/\/)wx(?=[1-4]\.sinaimg\.cn\/)", re.I)


def sinaimgtvax(url) -> str:
    return re.sub(sinaimgwx_pattern, "tvax", url)


# https://github.com/Quan666/ELF_RSS/blob/5e3fde857b4bed0297a0bcd4c9a59c2f5ea724c6/src/plugins/ELF_RSS2/parsing/handle_images.py#L160
async def download_image(url: str) -> Optional[bytes]:
    for _ in range(5):
        async with aiohttp.ClientSession(raise_for_status=True) as session:
            referer = f"{URL(url).scheme}://{URL(url).host}/"
            headers = {"referer": referer}
            try:
                resp = await session.get(url, headers=headers)
                # 如果图片无法获取到，直接返回
                if len(await resp.read()) == 0:
                    return None
                # 如果图片格式为 SVG ，先转换为 PNG
                if resp.headers["Content-Type"].startswith("image/svg+xml"):
                    next_url = str(
                        URL("https://images.weserv.nl/").with_query(
                            f"url={url}&output=png"
                        )
                    )
                    return await download_image(next_url)
                return await resp.read()
            except Exception as e:
                logger.warning(f"图片[{url}]下载失败！将重试最多 5 次！\n{e}")
                await asyncio.sleep(10)


async def get_image_cqcode(url: str):
    if img := await download_image(url):
        return MessageSegment.image(img)
    return f"\n图片获取失败: {url}\n"
