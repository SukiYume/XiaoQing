import requests
def get_zanghua(content='min'):
    url = 'https://nmsl.shadiao.app/api.php?'
    data = {
        'level':'min',
        'lang':'zh_cn'
    }
    if content == 'max':
        data = {
            'lang':'zh_cn'
        }
    headers = {
        'user-agent': 'Mozilla/5.0 Chrome/79.0.3945.130 Safari/537.36'
    }
    r = requests.post(url, data=data, headers=headers)
    return r.text