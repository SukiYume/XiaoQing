import requests

def get_lines(content):
    data = {
        "text":content,
        "bangumi_id":"",
        "duplicate":'true',
        "sort_values":"",
        "history":[]
    }
    url = 'https://windrises.net:8123/search/'
    headers = {
    'User-Agent': 'Mozilla/5.0 Chrome/79.0.3945.130 Safari/537.36'
    }
    r = requests.post(url, json=data, headers=headers)
    result = r.json()
    string = ''
    for i in result['dialogues'][:10]:
        string += i['subject_name']+' Ep: '+i['ep']+' Time: '+i['time_current']+'\n'+i['text_before']+'\n'+i['text_current']+'\n'+i['text_after']+'\n\n'
    return string[:-2]