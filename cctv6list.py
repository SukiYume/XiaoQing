import bot
import time
import asyncio
import requests
import datetime
from bs4 import BeautifulSoup,element
headers = {'user-agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\
            (KHTML, like Gecko) Chrome/77.0.3865.90 Safari/537.36',
           'accept':'text/html,application/xhtml+xml,application/xml;q=0.9,\
           image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3',
           'accept-encoding':'gzip, deflate'
           }

res = []
update_time = 0

async def FetchCCTV6List_today(context):
    global res,update_time
    if time.time()-update_time >86400:
        try:
            res = UpdateList()
            update_time = time.time()
        except ConnectionError:
            await bot.bot.send(context, "网络错误，请稍后再试。")
            return
    await bot.bot.send(context, f"今日六公主节目单：\n{res[0].strip()}")

async def FetchyCCTV6List_yestarday(context):
    global res,update_time
    if time.time()-update_time >86400:
        try:
            res = UpdateList()
            update_time = time.time()
        except ConnectionError:
            await bot.bot.send(context, "网络错误，请稍后再试。")
            return
    await bot.bot.send(context, f"昨日六公主节目单：\n{res[1].strip()}")

def UpdateList():
    try:
        response = requests.get("http://www.yue365.com/tv/jiemubiao/cctv6.shtml", headers=headers)
        if response.status_code != 200:
            raise ConnectionError
    except:
        raise
    else:
        content = BeautifulSoup(response.content, 'html.parser')
        content_list = content.find("div", attrs={
            "id": "m_nav_1",
        }).find_all('dd')
        today_pos = (7 + datetime.datetime.now().weekday()) % 7
        yesterday_pos = (6 + datetime.datetime.now().weekday()) % 7
        today = content_list[today_pos].text
        yesterday = content_list[yesterday_pos].text
    return [today, yesterday]