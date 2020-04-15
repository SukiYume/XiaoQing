import re
import json
import asyncio
from datetime import datetime, timedelta
import numpy as np
from aiocqhttp import CQHttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from alpha import get_answer
from xiaoice import chat
from localip import get_localip
from bilibili_hot import change_bili_hotpot
from getnetinfo import getOnlineUserInfo
from learnQA import learnQA, removeQA, getQA
from maoxuan_pred import get_pred
from speedtest import testmain
from determine import make_choice
from abbr import abbr
from adssearch import get_ads, get_papers, get_paper_list, remove_paper
from idcode import random_id
from xiaoqing_help import what_i_can_do
from bingmap import get_coordinate, get_weather
from search import get_search_result
from monitor import get_process
from zuichou import get_zanghua
from caiyun import get_translate
from animelines import get_lines
from moedict import get_dict_result
from adnmb import get_adnmb
from memo import get_memo
from test import testbot
from bashshell import shellexcute
from astrodict import get_astro_trans
from biliatob import get_bili_code
from jpnb import get_jpnb_res
from cctv6list import FetchCCTV6List_today, FetchyCCTV6List_yestarday

bot = CQHttp(enable_http_post=False)

async def send_sche_msg(context, message):
    await bot.send(context, message=message)
#    await bot.send_group_msg(group_id=187217751, message=message)

async def monitor_process(context, message, schid):
    tmpreply = get_process(message)
    if tmpreply[-5:] == '（在炼了）':
        pass
    else:
        text = '进程'+message[3:]+'运行结束了。'
        schedulers.remove_job(schid)
        await bot.send(context, message=text)

async def call_xiaoqing():
    with open('小青.json','r') as f:
        xiaoqing = json.load(f)
    xiaoqing = xiaoqing['小青']
    xiaoqing.append('叫我干嘛？')
    np.random.shuffle(xiaoqing)
    return xiaoqing[0]

@bot.on_message('private')
async def handle_msg(context):
    message = context['message']
    try:
        content = re.split(r'^小青[\s\,\，]*', message)[1]
    except:
        content = message
    if message == '小青':
        reply = await call_xiaoqing()
    elif content[:5] == 'alpha':
        question = re.split(r'^alpha[\s\,\，]*', content)[1]
        print(question)
        reply = get_answer(question)
    elif content[:2] == 'ip' or content[:4] == '你的ip':
        reply = get_localip()
    elif content in ['流量', '流量信息', '剩余流量', '你还剩多少流量', '你还有多少流量', '你还剩多少流量？', '你还有多少流量？']:
        reply = getOnlineUserInfo()

    elif content[:2] in ['记忆','记住','学习']:
        question = re.split(r'^[(记忆)(记住)(学习)]*[\s\,\，]*', content)[1]
        reply = learnQA(question)
    elif content[:2] == '对话':
        question = re.split(r'^[(对话)]*[\s\,\，]*', content)[1]
        reply = getQA(question)
    elif content[:4] == '删除对话':
        question = re.split(r'^[(删除对话)]*[\s\,\，]*', content)[1]
        reply = removeQA(question)

    elif content[:2] == '赤化':
        reply = get_pred(content[3:])
    elif content in ['测速','测网速','再测一次','再测一遍']:
        await bot.send(context, '要测咯~')
        reply = '测完啦，结果：\n' + testmain()
    elif content[:2] == '决定' or content[:3] == '做决定':
        reply = make_choice(re.split(r'[\s\,\，]+', content)[1:])
    elif content[:2] == '缩写':
        reply = abbr(re.split(r'[\s\,\，]+', content)[1:])
    elif content[:3] == 'ads':
        reply = get_ads(content[4:])
    elif '身份证号' in content and ('随机' in content or '生成' in content or '给我' in content or '帮我' in content):
        reply = random_id()
    elif content in ['你能干啥','你能做啥','你会干啥','你能干什么','你能做什么','你会干什么','你会做什么','你会做啥','你会干啥','你都能干啥','你能能做啥','你都能干什么','你都能做什么','你都会什么']:
        reply = what_i_can_do()
    elif content[:4] == '获取进程':
        reply = get_process(content[4:])
    elif content[-2:] == '坐标':
        reply = get_coordinate(content[:-2])
    elif content[-2:] == '天气':
        reply = get_weather(content[:-2])
    elif content[:2] == '搜索':
        reply = get_search_result(content[3:])
    elif content[:3] == '提醒我':
        q = re.split(r'[\s]+',content[4:])
        Job = schedulers.add_job(send_sche_msg, 'cron', year=q[1], month=q[2], day=q[3], hour=q[4], minute=q[5], second=q[6], args=[context, q[0]])
        reply = '记下来了。'
    elif content[:4] == '监视进程':
        q = '状态'+content[4:]
        jobsid = [i.id for i in schedulers.get_jobs()]
        if content[5:] in jobsid:
            reply = '进程已经在控制中啦。'
        else:
            schedulers.add_job(monitor_process, 'cron', minute='*/1', args=[context, q, content[5:]], id=content[5:])
            reply = '知道啦，进程停下就会告诉你的。'
    elif content[:2] in ['骂人', '嘴臭']:
        reply = get_zanghua(content[3:])
    elif content[:2] in ['翻译', '通訳', '翻訳'] or content[:6] == 'define' or content[:9] == 'translate':
        question = re.split(r'^[(翻译)(通訳)(翻訳)(define)(translate)]*[\s\,\，]*', content)[1]
        reply = get_translate(question)
    elif content[:4] == '下载文献':
        reply = get_papers(content[5:])
    elif content[:4] == '删除文献':
        reply = remove_paper(content[5:])
    elif content[:4] == '文献列表':
        reply = get_paper_list()
    elif content[:4] == '台词出处':
        reply = get_lines(content[5:])
    elif content[:2] == '查词':
        reply = get_dict_result(content[3:])
    elif content[:5] == 'adnmb':
        reply = get_adnmb(content[6:])
    elif content[:2] == '笔记':
        reply = get_memo(content[3:])
    elif content[:2] == '测试':
        reply = await testbot(context, content[3:])
    elif content[:2] == '执行' or content[:7] in ['Execute', 'execute']:
        command = re.split(r'^[(执行)(Execute)(execute)]*[\s\,\，]*', content)[1]
        await shellexcute(context, command)
        reply = None
    
    elif content[:5] in ['天文学词典', 'astro']:
        question = re.split(r'^[(天文学词典)(astro)]*[\s\,\，]*', content)[1]
        reply = await get_astro_trans(question)
    elif content[:4] == 'bili':
        question = re.split(r'^bili[\s\,\，]*', content)[1]
        reply = await get_bili_code(question)
    elif content[:4] == 'jpnb':
        code = re.split(r'^jpnb[\s\,\，]*', content)[1]
        await get_jpnb_res(context, code)
        reply = None
    elif content in ['今日六公主','今日列表','今日央六列表','今天央六节目单', '今天节目单']:
        await FetchCCTV6List_today(context)
        reply = None
    elif content in ['昨日六公主','昨日列表','昨日央六列表','昨天央六节目单', '昨天节目单']:
        await FetchyCCTV6List_yestarday(context)
        reply = None
    
    else:
        with open('QA.json','r',encoding='utf-8') as f:
            data = json.load(f)
        if content in data.keys():
            answer = data[content]
            np.random.shuffle(answer)
            reply = answer[0]
        else:
            reply = chat(content)
    if reply:
        await bot.send(context, reply)

@bot.on_message('group')
async def handle_msg(context):
    message = context['message']
    if message[:2] == '小青':
        if message == '小青':
            reply = await call_xiaoqing()
        else:
            content = re.split(r'^小青[\s\,\，]*', message)[1]
            if content[:5] == 'alpha':
                question = re.split(r'^alpha[\s\,\，]*', content)[1]
                print(question)
                reply = get_answer(question)
            elif content[:2] == 'ip' or content[:4] == '你的ip':
                reply = get_localip()
            elif content in ['流量', '流量信息', '剩余流量', '你还剩多少流量', '你还有多少流量', '你还剩多少流量？', '你还有多少流量？', '流量报告', '报告流量']:
                reply = getOnlineUserInfo()

            elif content[:2] in ['记忆','记住','学习']:
                question = re.split(r'^[(记忆)(记住)(学习)]*[\s\,\，]*', content)[1]
                reply = learnQA(question)
            elif content[:2] == '对话':
                question = re.split(r'^[(对话)]*[\s\,\，]*', content)[1]
                reply = getQA(question)
            elif content[:4] == '删除对话':
                question = re.split(r'^[(删除对话)]*[\s\,\，]*', content)[1]
                reply = removeQA(question)

            elif content[:2] == '赤化':
                reply = get_pred(content[3:])
            elif content in ['测速','测网速','再测一次','再测一遍']:
                await bot.send(context, '要测咯~')
                reply = '测完啦，结果：\n' + testmain()
            elif content[:2] == '决定' or content[:3] == '做决定':
                reply = make_choice(re.split(r'[\s\,\，]+', content)[1:])
            elif content[:2] == '缩写':
                reply = abbr(re.split(r'[\s\,\，]+', content)[1:])
            elif content[:3] == 'ads':
                reply = get_ads(content[4:])
            elif '身份证号' in content and ('随机' in content or '生成' in content or '给我' in content or '帮我' in content):
                reply = random_id()
            elif content in ['你能干啥','你能做啥','你会干啥','你能干什么','你能做什么','你会干什么','你会做什么','你会做啥','你会干啥','你都能干啥','你能能做啥','你都能干什么','你都能做什么','你都会什么']:
                reply = what_i_can_do()
            elif content[:4] == '获取进程':
                reply = get_process(content[4:])
            elif content[-2:] == '坐标':
                reply = get_coordinate(content[:-2])
            elif content[-2:] == '天气':
                reply = get_weather(content[:-2])
            elif content[:2] == '搜索':
                reply = get_search_result(content[3:])
            elif content[:3] == '提醒我':
                q = re.split(r'[\s]+',content[4:])
                Job = schedulers.add_job(send_sche_msg, 'cron', year=q[1], month=q[2], day=q[3], hour=q[4], minute=q[5], second=q[6], args=[context, q[0]])
                reply = '记下来了。'
            elif content[:4] == '监视进程':
                q = '状态'+content[4:]
                jobsid = [i.id for i in schedulers.get_jobs()]
                if content[5:] in jobsid:
                    reply = '进程已经在控制中啦。'
                else:
                    schedulers.add_job(monitor_process, 'cron', minute='*/1', args=[context, q, content[5:]], id=content[5:])
                    reply = '知道啦，进程停下就会告诉你的。'
            elif content[:2] in ['骂人', '嘴臭']:
                reply = get_zanghua(content[3:])
            elif content[:2] == '翻译':
                reply = get_translate(content[3:])
            elif content[:4] == '下载文献':
                reply = get_papers(content[5:])
            elif content[:4] == '文献列表':
                reply = get_paper_list()
            elif content[:4] == '台词出处':
                reply = get_lines(content[5:])
            elif content[:2] == '查词':
                reply = get_dict_result(content[3:])
            elif content[:5] == 'adnmb':
                reply = get_adnmb(content[6:])
            elif content[:2] == '笔记':
                reply = get_memo(content[3:])
            elif content[:2] == '执行' or content[:7] in ['Execute', 'execute']:
                command = re.split(r'^[(执行)(Execute)(execute)]*[\s\,\，]*', content)[1]
                await shellexcute(context, command)
                reply = None
            
            elif content[:5] in ['天文学词典', 'astro']:
                question = re.split(r'^[(天文学词典)(astro)]*[\s\,\，]*', content)[1]
                reply = await get_astro_trans(question)
            elif content[:4] == 'bili':
                question = re.split(r'^bili[\s\,\，]*', content)[1]
                reply = await get_bili_code(question)
            elif content[:4] == 'jpnb':
                code = re.split(r'^jpnb[\s\,\，]*', content)[1]
                await get_jpnb_res(context, code)
                reply = None
            elif content in ['今日六公主','今日列表','今日央六列表','今天央六节目单', '今天节目单']:
                await FetchCCTV6List_today(context)
                reply = None
            elif content in ['昨日六公主','昨日列表','昨日央六列表','昨天央六节目单', '昨天节目单']:
                await FetchyCCTV6List_yestarday(context)
                reply = None
            
            else:
                with open('QA.json','r',encoding='utf-8') as f:
                    data = json.load(f)
                if content in data.keys():
                    answer = data[content]
                    np.random.shuffle(answer)
                    reply = answer[0]
                else:
                    reply = chat(content)
    else:
        if np.random.rand() > 0.97:
            with open('QA.json','r',encoding='utf-8') as f:
                data = json.load(f)
            if message in data.keys():
                answer = data[content]
                np.random.shuffle(answer)
                reply = answer[0]
            else:
                if message[1:3] == 'CQ':
                    answer = ['这是个啥？','哎。？','好像很厉害的样子。','嗯…','是这样嘛', '我没明白…']
                    np.random.shuffle(answer)
                    reply = answer[0]
                else:
                    reply = chat(message)
        else:
            reply = None
    if reply:
        await bot.send(context, reply)
#    return {'reply': context['message']}


@bot.on_notice('group_increase')
async def handle_group_increase(context):
    await bot.send(context, message='欢迎新领导来我群视察～',
                   at_sender=True, auto_escape=True)


@bot.on_request('group', 'friend')
async def handle_request(context):
    return {'approve': True}

schedulers = AsyncIOScheduler()
#schedulers.add_job(check_update, 'cron', second='20', minute='*/30')
#schedulers.add_job(check_update, 'interval', seconds=30)