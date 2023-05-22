from random import shuffle
from datetime import datetime
from typing import Dict, Union, List
from pathlib import Path
from asyncio import sleep, gather
from utils.manager import group_manager
from services.log import logger
from utils.utils import scheduler
from utils.http_utils import AsyncPlaywright
from utils.image_utils import text2image
from utils.message_builder import image
from configs.config import Config
from apscheduler.triggers.cron import CronTrigger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot import on_command
from nonebot.adapters.onebot.v11.permission import GROUP
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me
from utils.message_builder import image
from configs.config import Config
from nonebot import Driver, get_driver, get_bot
from time import strftime, localtime
import yaml

try:
    import ujson as json
except:
    import json

from .weibo_spider import (
    weibo_record_path,
    weibo_id_name_file,
    UserWeiboSpider,
    KeywordWeiboSpider,
)
from ._utils import get_image_cqcode, SendManager

tasks_dict: Dict[str, List[Union[UserWeiboSpider, KeywordWeiboSpider]]] = {}


def _load_config():
    weibo_record_path.mkdir(parents=True, exist_ok=True)
    with open(
        Path(__file__).parent / "weibo_config.yaml",
        "r",
        encoding="utf8",
    ) as f:
        configs = yaml.safe_load(f)
    default_format = Config.get_config(Path(__file__).parent.name, "DEFAULT_FORMAT")
    for task_name, v in configs.items():
        enable_on_default = v["enable_on_default"]
        users = v["users"]
        if "format" in v:
            cur_format = v["format"]
        else:
            cur_format = default_format
        task_spider_list = []
        for user in users:
            if "format" not in user:
                user["format"] = cur_format
            if "keyword" in user:
                wb_spider = KeywordWeiboSpider(user)
            elif "user_id" in user:
                wb_spider = UserWeiboSpider(user)
            task_spider_list.append(wb_spider)
        __plugin_task__[task_name] = v["desciption"]
        Config.add_plugin_config(
            "_task",
            f"default_{task_name}",
            enable_on_default,
            help_=f"被动 {v['desciption']} 进群默认开关状态",
        )
        tasks_dict[task_name] = task_spider_list


__zx_plugin_name__ = "微博推送"
__plugin_usage__ = """
usage：
    自动推送微博（可推送范围由维护者设定）
    发送[可订阅微博列表]（需要at）可查看订阅列表
""".strip()
__plugin_des__ = "自动推送微博（可推送范围由维护者设定）"
__plugin_version__ = 0.1
__plugin_cmd__ = ["可订阅微博列表", "更新微博用户名 [_superuser]"]
__plugin_author__ = "migang"
__plugin_task__ = {}
_load_config()
__plugin_settings__ = {"cmd": ["微博推送"]}
__plugin_configs__ = {
    "forward_mode": {
        "value": False,
        "help": "是否以转发模式推送微博，当配置项为true时将以转发模式推送",
        "default_value": False,
    },
    "default_format": {
        "value": 1,
        "help": "默认推送格式：0 文本，1 图片",
        "default_value": 1,
    },
    "cookie": {
        "value": None,
        "help": "添加cookie后可以获取到更多的微博",
        "default_value": None,
    },
    "custom_send_time": {
        "value": None,
        "help": "自定义发送时间，cron格式，若空，则检测到微博后立即发送，反之，仅设定的时刻推送",
        "default_value": None,
    },
}


weibo_list = on_command(
    "可订阅微博列表",
    aliases={"weibo-list"},
    rule=to_me(),
    permission=GROUP,
    priority=5,
    block=True,
)

weibo_update_username = on_command(
    "更新微博用户名",
    rule=to_me(),
    permission=SUPERUSER,
    priority=5,
    block=True,
)

driver: Driver = get_driver()
forward_mode = Config.get_config(Path(__file__).parent.name, "FORWARD_MODE")
custom_send_time = Config.get_config(
    Path(__file__).parent.name, "CUSTOM_SEND_TIME", default=None
)

message_storehouse: Dict[str, List[Message]] = None
if custom_send_time is not None:
    import pickle
    from .weibo_spider import PATH

    last_push_time: datetime = datetime.now()
    message_storehouse_file = PATH / "message_storehouse.pkl"
    message_storehouse_file.parent.mkdir(parents=True, exist_ok=True)

    async def custom_send():
        bot = get_bot()
        if not bot:
            return
        gl = await bot.get_group_list()
        gl = [g["group_id"] for g in gl]
        shuffle(gl)
        global message_storehouse, last_push_time
        # 防止推送过程来新的
        message_storehouse_cp = message_storehouse
        start_line = MessageSegment.node_custom(
            bot.self_id,
            "微博威",
            f"以下为{last_push_time.strftime('%Y-%m-%d %H:%M:%S')}至今群内的订阅微博",
        )
        last_push_time = datetime.now()
        message_storehouse = {}
        for group_id in gl:
            group_message = [start_line]
            for task, messages in message_storehouse_cp.items():
                if group_manager.check_group_task_status(group_id, task):
                    group_message += [
                        MessageSegment.node_custom(bot.self_id, "微博威", message)
                        for message in messages
                    ]
            if len(group_message) != 1:
                await bot.send_forward_msg(group_id=group_id, messages=group_message)
                await sleep(0.3)

    @driver.on_startup
    async def _():
        global message_storehouse
        if message_storehouse_file.exists():
            with message_storehouse_file.open("rb") as f:
                message_storehouse = pickle.load(f)
        else:
            message_storehouse = {}
        scheduler.add_job(custom_send, CronTrigger.from_crontab(custom_send_time))

    @driver.on_shutdown
    async def _():
        with message_storehouse_file.open("wb") as f:
            pickle.dump(message_storehouse, f)


@driver.on_startup
async def _():
    tasks = []
    for _, spiders in tasks_dict.items():
        for spider in spiders:
            tasks.append(spider.init())
    try:
        await gather(*tasks)
        logger.info("微博推送初始化完成")
    except Exception as e:
        logger.error(f"微博推送初始化异常: {e}")


@weibo_list.handle()
async def _(event: GroupMessageEvent):
    group_id = event.group_id
    msg = "\n以下为可订阅微博列表，请发送[开启xxx]来订阅\n=====================\n"
    ret = []
    for task, spiders in tasks_dict.items():
        tmp = f'{__plugin_task__[task]}[{"√" if group_manager.check_group_task_status(group_id, task) else "×"}]:'
        users = []
        for spider in spiders:
            users.append(
                f"{spider.get_notice_name()}[{'图片' if spider.get_format() == 1 else '文本'}]"
            )
        ret.append(tmp + " ".join(users))
    await weibo_list.finish(
        image(b64=(await text2image(msg + "\n\n".join(ret) + "\n")).pic2bs4())
    )


@weibo_update_username.handle()
async def _():
    await weibo_update_username.send("开始更新微博用户名")
    await update_user_name()
    await weibo_update_username.send("微博用户名更新结束")


async def wb_to_text(wb: Dict):
    msg = f"{wb['screen_name']}'s Weibo:\n====================="
    # id = wb["id"]
    bid = wb["bid"]
    time = wb["created_at"]
    if "retweet" in wb:
        msg = f"{msg}\n{wb['text']}\n=========转发=========\n>>转发@{wb['retweet']['screen_name']}"
        wb = wb["retweet"]
    msg += f"\n{wb['text']}"
    if len(wb["pics"]) > 0:
        image_urls = wb["pics"]
        msg += "\n"
        res_imgs = await gather(*[get_image_cqcode(url) for url in image_urls])
        for img in res_imgs:
            msg += img

    if len(wb["video_poster_url"]) > 0:
        video_posters = wb["video_poster_url"]
        msg += "\n[视频封面]\n"
        video_imgs = await gather(*[get_image_cqcode(url) for url in video_posters])
        for img in video_imgs:
            msg += img

    msg += f"\nURL:https://m.weibo.cn/detail/{bid}\n时间: {strftime('%Y-%m-%d %H:%M', localtime(time))}"

    return msg


async def wb_to_image(wb: Dict) -> bytes:
    msg = f"{wb['screen_name']}'s Weibo:\n"
    url = f"https://m.weibo.cn/detail/{wb['bid']}"
    time = wb["created_at"]
    for _ in range(3):
        try:
            async with AsyncPlaywright.new_page(
                is_mobile=True, viewport={"width": 2048, "height": 2732}
            ) as page:
                await page.goto(
                    url,
                    wait_until="networkidle",
                )
                # await page.wait_for_selector(".ad-wrap", state="attached", timeout=8 * 1000)
                # await page.eval_on_selector(
                #     selector=".ad-wrap",
                #     expression="(el) => el.style.display = 'none'",
                # )
                # 去除“小程序看微博热搜”横幅
                try:
                    await page.wait_for_selector(".wrap", state="attached", timeout=30)
                    await page.eval_on_selector(
                        selector=".wrap",
                        expression="(el) => el.style.display = 'none'",
                    )
                except:
                    pass
                card = await page.wait_for_selector(
                    f"xpath=//div[@class='card m-panel card9 f-weibo']",
                    timeout=6 * 1000,
                )
                img = await card.screenshot()
                return (
                    msg
                    + image(img)
                    + f"\n{url}\n时间: {strftime('%Y-%m-%d %H:%M', localtime(time))}"
                )
        except Exception as e:
            logger.warning(f"截取微博主页失败: {e}")
            sleep(1.1)
    return None


async def process_wb(format: int, wb: Dict):
    if (
        not wb["only_visible_to_fans"]
        and format == 1
        and (msg := await wb_to_image(wb))
    ):
        return msg
    return await wb_to_text(wb)


@scheduler.scheduled_job("interval", seconds=120, jitter=10)
async def _():
    for task, spiders in tasks_dict.items():
        weibos = []
        for spider in spiders:
            latest_weibos = await spider.get_latest_weibos()
            format = spider.get_format()
            formatted_weibos = [(await process_wb(format, wb)) for wb in latest_weibos]
            if l := len(formatted_weibos):
                logger.info(f"成功获取{spider.get_notice_name()}的新微博{l}条")
            else:
                logger.info(f"未检测到{spider.get_notice_name()}的新微博")
            if custom_send_time is None:
                weibos += formatted_weibos
            else:
                if task not in message_storehouse:
                    message_storehouse[task] = []
                message_storehouse[task] += formatted_weibos
        if weibos:
            bot = get_bot()
            gl = await bot.get_group_list()
            gl = [
                g["group_id"]
                for g in gl
                if group_manager.check_group_task_status(g["group_id"], task)
            ]
            shuffle(gl)
            if forward_mode:
                weibos = [
                    {
                        "type": "node",
                        "data": {
                            "name": f"微博威",
                            "uin": f"{bot.self_id}",
                            "content": weibo,
                        },
                    }
                    for weibo in weibos
                ]
            await SendManager(bot, gl, weibos, forward=forward_mode).Do()


@scheduler.scheduled_job("cron", second="0", minute="0", hour="5")
async def clear_spider_buffer():
    logger.info("Cleaning weibo spider buffer...")
    for _, spiders in tasks_dict.items():
        for spider in spiders:
            spider.clear_buffer()


@scheduler.scheduled_job("cron", second="0", minute="0", hour="4")
async def update_user_name():
    logger.info("Updating weibo user_name...")
    id_name_map = {}
    try:
        with open(weibo_id_name_file, "r", encoding="UTF-8") as f:
            id_name_map = json.load(f)
    except FileNotFoundError:
        pass
    for _, spiders in tasks_dict.items():
        for spider in spiders:
            if isinstance(spider, UserWeiboSpider):
                if uname := await spider.update_username():
                    id_name_map[spider.get_userid()] = uname
    with open(weibo_id_name_file, "w", encoding="utf8") as f:
        json.dump(id_name_map, f, indent=4, ensure_ascii=False)
