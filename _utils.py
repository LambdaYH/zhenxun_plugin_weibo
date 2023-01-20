import re
import asyncio
from random import random
from typing import Optional, Dict, List, Union
from yarl import URL
import aiohttp

from nonebot.log import logger
from nonebot.adapters.onebot.v11 import MessageSegment, ActionFailed, Bot, Message

# ref: https://github.com/DIYgod/RSSHub/blob/5c7aff76a3a90d6ac5d5e7e139bc182c9c147cb6/lib/v2/weibo/utils.js#L425
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
    return None


async def get_image_cqcode(url: str):
    if img := await download_image(url):
        return MessageSegment.image(img)
    return f"\n图片获取失败: {url}\n"


class SendManager:
    def __init__(
        self,
        bot: Bot,
        group_list,
        msg: Union[List[Message], Message],
        forward=False,
        retry_limit: int = 3,
        retry_interval: int = 5,
    ):
        self.bot = bot
        self.group_list = group_list
        self.msg = msg
        self.forward = forward
        self.retry_limit = retry_limit
        self.retry_interval = retry_interval
        self.failed_dict: Dict[int, Union[set[int], bool]] = {}

    async def retry(self):
        for i in range(self.retry_limit):
            if self.forward:
                for group in list(self.failed_dict):
                    await asyncio.sleep(random() + 0.3)
                    try:
                        await self.bot.send_group_forward_msg(
                            group_id=group, messages=self.msg
                        )
                        self.failed_dict.pop(group)
                    except ActionFailed as e:
                        logger.error(f"GROUP {group} 微博推送失败 {type(e)}: {e}")
            else:
                for group in list(self.failed_dict):
                    for idx in list(self.failed_dict[group]):
                        await asyncio.sleep(random() + 0.3)
                        try:
                            await self.bot.send_group_msg(
                                group_id=group, message=self.msg[idx]
                            )
                            self.failed_dict[group].remove(idx)
                        except ActionFailed as e:
                            logger.error(f"GROUP {group} 微博推送失败 {type(e)}: {e}")
                    if not self.failed_dict[group]:
                        self.failed_dict.pop(group)

            if not self.failed_dict:
                break
            logger.warning(
                f"剩余 {len(self.failed_dict)} 个群微博推送失败，重试次数{i+1}/{self.retry_limit}"
            )
            await asyncio.sleep(self.retry_interval)

    async def Do(self):
        if self.forward:
            for group in self.group_list:
                await asyncio.sleep(random() + 0.3)
                try:
                    await self.bot.send_group_forward_msg(
                        group_id=group, messages=self.msg
                    )
                except ActionFailed as e:
                    logger.error(f"GROUP {group} 微博推送失败 {type(e)}: {e}")
                    self.failed_dict[group] = True
        else:
            for group in self.group_list:
                for i, weibo in enumerate(self.msg):
                    await asyncio.sleep(random() + 0.3)
                    try:
                        await self.bot.send_group_msg(group_id=group, message=weibo)
                    except ActionFailed as e:
                        logger.error(f"GROUP {group} 微博推送失败 {type(e)}: {e}")
                        if group not in self.failed_dict:
                            self.failed_dict[group] = set()
                        self.failed_dict[group].add(i)
        if self.failed_dict:
            logger.warning(f"共 {len(self.failed_dict)} 个群微博推送失败，即将重试")
            await asyncio.sleep(self.retry_interval)
            await self.retry()