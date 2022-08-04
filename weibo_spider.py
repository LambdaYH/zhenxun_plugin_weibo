import random
import re
import sys
import httpx
import asyncio
import time
import calendar
from urllib.parse import unquote
from datetime import datetime

from lxml import etree
from services.log import logger
from .exception import *
from configs.path_config import TEMP_PATH, TEXT_PATH

try:
    import ujson as json
except:
    import json

http_prefix = "https"
api_url = f"{http_prefix}://m.weibo.cn/api/container/getIndex"
month = {month: index for index, month in enumerate(calendar.month_abbr) if month}
weibo_record_path = TEMP_PATH / "weibo"
weibo_id_name_file = TEXT_PATH / "weibo_id_name.json"


class WeiboSpider(object):
    def __init__(self, config):
        """Weibo类初始化"""
        self.validate_config(config)
        self.filter_retweet = config["filter_retweet"]
        self.user_id = config["user_id"]
        self.filter_words = config["filter_words"]
        self.received_weibo_ids = []
        self.__recent = False
        self.record_file_path = weibo_record_path / f"{self.user_id}.json"
        self.user_name = self.user_id
        try:
            with open(self.record_file_path, "r", encoding="UTF-8") as f:
                self.received_weibo_ids = json.load(f)
        except FileNotFoundError:
            pass
        try:
            with open(weibo_id_name_file, "r", encoding="UTF-8") as f:
                id_name_map = json.load(f)
            if self.user_id in id_name_map:
                self.user_name = id_name_map[self.user_id]
        except FileNotFoundError:
            pass

    async def get_json(self, params):
        """
        获取网页中json数据
        """
        async with httpx.AsyncClient() as client:
            for _ in range(5):
                r = await client.get(api_url, params=params, timeout=20.0)
                if r.status_code == 200:
                    return r.json()
                await asyncio.sleep(random.randint(2, 6))

    async def init(self):
        self.__init = True
        if not self.record_file_path.exists():
            await self.get_latest_weibos()
        self.__init = False

    async def update_username(self):
        try:
            js = await self.get_json({"containerid": f"100505{self.user_id}"})
            if js["ok"]:
                info = js["data"]["userInfo"]
                self.user_name = info.get("screen_name")
        except Exception as e:
            logger.warning(f"微博用户{self.user_id}更新user_name异常: {e}")
            return None
        return self.user_name

    def get_userid(self):
        return self.user_id

    def get_username(self):
        return self.user_name

    def save(self):
        with open(self.record_file_path, "w", encoding="utf8") as f:
            json.dump(self.received_weibo_ids, f, indent=4, ensure_ascii=False)

    def clear_buffer(self):
        """
        如果清理缓存前一分钟，该微博账号瞬间发送了 20 条微博
        然后清理缓存仅仅保留后 10 条的微博id，因此可能会重复推送前 10 条微博
        当然这种情况通常不会发生
        """
        self.received_weibo_ids = self.received_weibo_ids[-20:]
        self.save()

    def validate_config(self, config):
        """验证配置是否正确"""
        exist_argument_list = ["user_id", "filter_words"]
        true_false_argument_list = ["filter_retweet"]

        for argument in true_false_argument_list:
            if argument not in config:
                raise NotFoundError(f"未找到参数{argument}")
            if config[argument] != True and config[argument] != False:
                raise ParseError(f"{argument} 值应为 True 或 False")

        for argument in exist_argument_list:
            if argument not in config:
                raise NotFoundError(f"未找到参数{argument}")

    def get_pics(self, weibo_info):
        """获取微博原始图片url"""
        if weibo_info.get("pics"):
            pic_info = weibo_info["pics"]
            pic_list = [pic["large"]["url"] for pic in pic_info]
        else:
            pic_list = []
        """获取文章封面图片url"""
        if "page_info" in weibo_info and weibo_info["page_info"]["type"] == "article":
            if "page_pic" in weibo_info["page_info"]:
                pic_list.append(weibo_info["page_info"]["page_pic"]["url"])

        return pic_list

    def get_live_photo(self, weibo_info):
        """获取live photo中的视频url"""
        live_photo_list = []
        live_photo = weibo_info.get("pic_video")
        if live_photo:
            prefix = (
                f"{http_prefix}://video.weibo.com/media/play?livephoto=//us.sinaimg.cn/"
            )
            for i in live_photo.split(","):
                if len(i.split(":")) == 2:
                    url = prefix + i.split(":")[1] + ".mov"
                    live_photo_list.append(url)
            return live_photo_list

    def get_video_url(self, weibo_info):
        """获取微博视频url"""
        video_url = ""
        video_poster_url = ""
        video_url_list = []
        video_poster_url_list = []
        if weibo_info.get("page_info"):
            if (
                weibo_info["page_info"].get("media_info")
                and weibo_info["page_info"].get("type") == "video"
            ):
                media_info = weibo_info["page_info"]["media_info"]
                video_url = media_info.get("mp4_720p_mp4")
                video_poster_url = weibo_info["page_info"].get("page_pic").get("url")
                if not video_url:
                    video_url = media_info.get("mp4_hd_url")
                    if not video_url:
                        video_url = media_info.get("mp4_sd_url")
                        if not video_url:
                            video_url = media_info.get("stream_url_hd")
                            if not video_url:
                                video_url = media_info.get("stream_url")
        if video_url:
            video_url_list.append(video_url)
        if video_poster_url:
            video_poster_url_list.append(video_poster_url)
        live_photo_list = self.get_live_photo(weibo_info)
        if live_photo_list:
            video_url_list += live_photo_list
        return video_url_list, video_poster_url_list

    def get_location(self, selector):
        """获取微博发布位置"""
        location_icon = "timeline_card_small_location_default.png"
        span_list = selector.xpath("//span")
        location = ""
        for i, span in enumerate(span_list):
            if span.xpath("img/@src"):
                if location_icon in span.xpath("img/@src")[0]:
                    location = span_list[i + 1].xpath("string(.)")
                    break
        return location

    def get_topics(self, selector):
        """获取参与的微博话题"""
        span_list = selector.xpath("//span[@class='surl-text']")
        topics = ""
        topic_list = []
        for span in span_list:
            text = span.xpath("string(.)")
            if len(text) > 2 and text[0] == "#" and text[-1] == "#":
                topic_list.append(text[1:-1])
        if topic_list:
            topics = ",".join(topic_list)
        return topics

    def get_at_users(self, selector):
        """获取@用户"""
        a_list = selector.xpath("//a")
        at_users = ""
        at_list = []
        for a in a_list:
            if "@" + a.xpath("@href")[0][3:] == a.xpath("string(.)"):
                at_list.append(a.xpath("string(.)")[1:])
        if at_list:
            at_users = ",".join(at_list)
        return at_users

    def get_text(self, text_body):
        selector = etree.HTML(text_body)
        url_elems = selector.xpath("//a[@href]/span[@class='surl-text']")
        for br in selector.xpath("br"):
            br.tail = "\n" + br.tail
        """
        Add the url of <a/> to the text of <a/>
        For example:
            <a data-url="http://t.cn/A622uDbW" href="http_prefix://weibo.com/ttarticle/p/show?id=2309404507062473195617">
            <span class=\'url-icon\'>
            <img style=\'width: 1rem;height: 1rem\' src=\'http_prefix://h5.sinaimg.cn/upload/2015/09/25/3/timeline_card_small_article_default.png\'></span>
            <span class="surl-text">本地化笔记第三期——剧情活动排期调整及版本更新内容前瞻</span>
            </a>
            replace <span class="surl-text">本地化笔记第三期——剧情活动排期调整及版本更新内容前瞻</span>
            with <span class="surl-text">本地化笔记第三期——剧情活动排期调整及版本更新内容前瞻(http://t.cn/A622uDbW)</span>
        """
        for elem in url_elems:
            url = elem.getparent().get("href")
            if (
                not elem.text.startswith("#")
                and not elem.text.endswith("#")
                and (
                    url.startswith("https://weibo.cn/sinaurl?u=")
                    or url.startswith("https://video.weibo.com")
                )
            ):
                url = unquote(url.replace("https://weibo.cn/sinaurl?u=", ""))
                elem.text = f"{elem.text}({url} )"
        return selector.xpath("string(.)")

    def string_to_int(self, string):
        """字符串转换为整数"""
        if isinstance(string, int):
            return string
        elif string.endswith("万+"):
            string = int(string[:-2] + "0000")
        elif string.endswith("万"):
            string = int(string[:-1] + "0000")
        return int(string)

    def standardize_date(self, created_at):
        """标准化微博发布时间"""
        created_at = created_at.split(" ")
        h_m_s = list(map(int, created_at[3].split(":")))
        created_at = time.mktime(
            datetime(
                int(created_at[5]),
                month[created_at[1]],
                int(created_at[2]),
                h_m_s[0],
                h_m_s[1],
                h_m_s[2],
                0,
            ).timetuple()
        )
        deltaTime = time.time() - created_at
        if deltaTime <= 7200:
            self.__recent = True
        elif deltaTime > 7200 and deltaTime < 86400:
            if self.__init:
                self.__recent = True
            else:
                self.__recent = False
        else:
            self.__recent = False
        return created_at

    def standardize_info(self, weibo):
        """标准化信息，去除乱码"""
        for k, v in weibo.items():
            if (
                "bool" not in str(type(v))
                and "int" not in str(type(v))
                and "list" not in str(type(v))
                and "long" not in str(type(v))
            ):
                weibo[k] = (
                    v.replace("\u200b", "")
                    .encode(sys.stdout.encoding, "ignore")
                    .decode(sys.stdout.encoding)
                )
        return weibo

    def parse_weibo(self, weibo_info):
        weibo = {}
        if "user" in weibo_info:
            weibo["screen_name"] = weibo_info["user"]["screen_name"]
        else:
            weibo["screen_name"] = ""
        weibo["id"] = weibo_info["id"]
        weibo["bid"] = weibo_info["bid"]
        text_body = weibo_info["text"]
        text_body = text_body.replace("<br/>", "\n").replace("<br />", "\n")

        weibo["text"] = self.get_text(text_body)

        weibo["pics"] = self.get_pics(weibo_info)
        weibo["video_url"], weibo["video_poster_url"] = self.get_video_url(weibo_info)
        weibo["created_at"] = weibo_info["created_at"]
        return self.standardize_info(weibo)

    async def get_weibo_json(self, page):
        """获取网页中微博json数据"""
        params = {"containerid": f"107603{self.user_id}", "page": page}
        js = await self.get_json(params)
        return js

    async def get_long_weibo(self, id):
        """获取长微博"""
        async with httpx.AsyncClient() as client:
            for _ in range(5):
                url = f"{http_prefix}://m.weibo.cn/detail/{id}"
                html = await client.get(url, timeout=15)
                html = html.text
                p = re.compile(
                    r"var \$render_data = (.*)[\s\S]{7}{};", re.MULTILINE | re.DOTALL
                )
                html = p.findall(html)
                if html:
                    html = html[0]
                    js = json.loads(html[1:-1])
                    weibo_info = js.get("status")
                    if weibo_info:
                        weibo = self.parse_weibo(weibo_info)
                        return weibo
                await asyncio.sleep(random.randint(6, 10))

    async def get_one_weibo(self, info):
        """获取一条微博的全部信息"""
        try:
            weibo_info = info["mblog"]
            weibo_id = weibo_info["id"]
            retweeted_status = weibo_info.get("retweeted_status")
            is_long = weibo_info.get("isLongText") or weibo_info.get("pic_num", 0) > 9
            if is_long:
                weibo = await self.get_long_weibo(weibo_id)
                if not weibo:
                    weibo = self.parse_weibo(weibo_info)
            else:
                weibo = self.parse_weibo(weibo_info)
            if retweeted_status and retweeted_status.get("id"):  # 转发
                retweet_id = retweeted_status.get("id")
                is_long_retweet = (
                    retweeted_status.get("isLongText")
                    or retweeted_status.get("pic_num", 0) > 9
                )
                if is_long_retweet:
                    retweet = await self.get_long_weibo(retweet_id)
                    if not retweet:
                        retweet = self.parse_weibo(retweeted_status)
                else:
                    retweet = self.parse_weibo(retweeted_status)
                retweet["created_at"] = self.standardize_date(
                    retweeted_status["created_at"]
                )
                weibo["retweet"] = retweet
            weibo["created_at"] = self.standardize_date(weibo_info["created_at"])
            return weibo
        except Exception as e:
            logger.exception(e)
            self.__recent = False

    async def get_latest_weibos(self):
        try:
            latest_weibos = []
            js = await self.get_weibo_json(1)
            if js["ok"]:
                weibos = js["data"]["cards"]
                for w in weibos:
                    if w["card_type"] == 9:
                        if w["mblog"]["id"] in self.received_weibo_ids:
                            continue
                        wb = await self.get_one_weibo(w)
                        if wb:
                            if not self.__recent:
                                continue
                            for word in self.filter_words:
                                if word in wb["text"] or (
                                    "retweet" in wb and word in wb["retweet"]
                                ):
                                    self.received_weibo_ids.append(wb["id"])
                                    break
                            if wb["id"] in self.received_weibo_ids:
                                continue
                            if (not self.filter_retweet) or ("retweet" not in wb):
                                latest_weibos.append(wb)
                                self.received_weibo_ids.append(wb["id"])
                                # self.print_weibo(wb)
            if latest_weibos:
                self.save()
            return latest_weibos
        except Exception as e:
            logger.exception(e)
            return []
