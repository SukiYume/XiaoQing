import requests

urli = 'https://api.bilibili.com/x/activity/bnj2020/hotpot/increase'
urld = 'https://api.bilibili.com/x/activity/bnj2020/hotpot/decrease'

headers = {'User-Agent': 
           'Mozilla/5.0 Chrome/78.0.3904.70 Safari/537.36'}

datai = {
    'count': 999999999999999999,
    'csrf': '25896097c261f81623fc3f29d54634d0'
}

datad = {
    'csrf': '25896097c261f81623fc3f29d54634d0'
}
def change_bili_hotpot(choice):
    if choice == '加菜':
        url = urli
        data = datai
    else:
        url = urld
        data = datad
    r = requests.post(url, data = data, headers = headers)
    result = r.json()
    result = result['data']
    if choice == '加菜':
        if result:
            reply = '加入了 '+result['name']+'\n' +result['desc']
        else:
            reply = '什么也没放进去好像。'
    else:
        reply = result['toast']+' CD:'+str(result['cd'])
    return reply