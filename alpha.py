import requests
from xml.etree import ElementTree

import json
with open('api.json', 'r') as f:
    data = json.load(f)
api = data['alpha']

def get_answer(question):
    if question.split(' ')[-1] == 'step':
        data = {
            'input':question[:-4],
            'podstate':'Result__Step-by-step solution',
            'format':'plaintext'
        }
        url = 'http://api.wolframalpha.com/v2/query?appid='+api
        r = requests.post(url, data=data)
        root = ElementTree.fromstring(r.text)
        data_list = root.getiterator('plaintext')
        string = ''
        for i in data_list:
            if i.text != None:
                string += i.text + '\n'
        return string[:-1]
    elif question.split(' ')[-1] == 'cp':
        url = 'http://api.wolframalpha.com/v2/query?'
        data = {
            'appid':api,
            'input':question[:-2],
            'includepodid':'Result',
            'format':'plaintext',
            'output':'json'
        }
        r = requests.post(url, data=data)
        return r.json()['queryresult']['pods'][0]['subpods'][0]['plaintext']
    else:
        data = {
            'i':question
        }
        url = 'http://api.wolframalpha.com/v1/result?appid='+api
        r = requests.post(url, data=data)
        return r.text