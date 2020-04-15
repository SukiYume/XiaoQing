import re
import asyncio
from urlmessage import get_url_message

table = 'fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF'
tr = {}
for i in range(58):
    tr[table[i]] = i
s = [11,10,3,8,4,6,2,9,5,7]
xor = 177451812
add = 100618342136696320

def dec(x):
    r = 0
    for i in range(10):
        r += tr[x[s[i]]]*58**i
    return (r-add)^xor

def enc(x):
    x = int(x)
    x = (x^xor)+add
    r = list('BV          ')
    for i in range(10):
        r[s[i]]=table[x//58**i%58]
    return ''.join(r)

async def get_bili_code(content):
    content = re.split(r'\s+', content)
    if content[0] == 'a2b':
        if content[1].isdigit():
            url = 'https://www.bilibili.com/video/' + enc(content[1])
            try:
                return get_url_message(url)
            except:
                return url
        else:
            code = re.search(r'(av\d+)', content[1])
            if code:
                url = 'https://www.bilibili.com/video/' + enc(code.groups(0)[0][2:])
                try:
                    return get_url_message(url)
                except:
                    return url
            else:
                return '看看写错了什么东西？'
    elif content[0] == 'b2a':
        code = re.search(r'(BV.{10})', content[1])
        if code:
            url = 'https://www.bilibili.com/video/av' + str(dec(code.groups(0)[0]))
            try:
                return get_url_message(url)
            except:
                return url
        else:
            return '是不是输错了BV号？'
    else:
        return '要做什么？'

