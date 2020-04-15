import re
import requests

import json
with open('api.json', 'r') as f:
    data = json.load(f)
api = data['caiyun']

def get_translate(content):
    if '|' in content or '｜' in content:
        source, direction = re.split(r'[\|\｜\s]+', content)
    else:
        source = content
#        zhPattern = re.compile(u'[\u4e00-\u9fa5]+')
        zhPattern = re.compile(r'^[\u4E00-\u9FFF]+$')
        match = zhPattern.search(source)
        if match:
            direction = 'zh2en'
        else:
            direction = 'auto2zh'
    
    url = "http://api.interpreter.caiyunai.com/v1/translator"
    token = api
    payload = {
            "source" : source, 
            "trans_type" : direction,
            "request_id" : "demo",
            "detect": True,
            }
    
    headers = {
            'content-type': "application/json",
            'x-authorization': "token " + token,
    }
    
    r = requests.request("POST", url, json=payload, headers=headers)
    return r.json()['target']