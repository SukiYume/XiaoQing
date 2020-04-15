import json
import requests

def getOnlineUserInfo():
    r = requests.get('http://210.77.16.21')
    try: 
        userIndex = r.url.split('userIndex=')[1]
    except:
        return '掉线了？'

    url = 'http://210.77.16.21/eportal/InterFace.do?method=getOnlineUserInfo&userIndex='+userIndex
    r = requests.get(url)
    r.encoding = 'utf-8'
    result = r.json()
    
    userName = result['userName']
    offlineurl = result['offlineurl']
    ballInfo = result['ballInfo']
    
    ballInfo = json.loads(ballInfo)
    flow = ballInfo[1]['value'] if ballInfo[1]['id'] == 'flow' else 0
    flow_with_mb = float(flow) / 1024 / 1024
    flow_info = ''
    if flow_with_mb > 1024:
        flow_info = str.format('{:.2f} GB', flow_with_mb / 1024)
    else:
        flow_info = str.format('{:.2f} MB', flow_with_mb)
    onlinedevice = ballInfo[2]['value'] if ballInfo[2]['id'] == 'onlinedevice' else 0
    
    info = {}
    info['flow_info'] = flow_info
    info['flow'] = flow
    info['onlinedevice'] = onlinedevice
    
    info['userId'] = result['userId']
    info['userName'] = userName
    string = info['userName']+' 剩余流量：'+info['flow_info']
    return string