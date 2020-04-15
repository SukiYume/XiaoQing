import re
import requests
from bs4 import BeautifulSoup
def get_search_result(content):
    query = re.split(r'[\|\｜]+',content)
    if len(query)>1:
        q = query[0]
        num = query[1]
    else:
        q = query[0]
        num = 3
    url = 'http://www.webcrawler.com/serp?q=' + q
    headers = {
        'User-Agent': 'Mozilla/5.0 Chrome/79.0.3945.130 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9'
    }
    try:
        r = requests.get(url, headers = headers)
        soup = BeautifulSoup(r.text,'html.parser')
    except:
        return '好像搜不到相关内容呢。'
    a = soup.find_all(name='div',attrs={'class':'web-google__result'})
    string = '关于 '+q+' 的搜索结果：\n\n'
    if len(a)>num:
        a = a[:num]
    for i in a:
        string += i.a.get_text() + '\n'
        string += i.a.get('href') + '\n'
        string += i.find_all('span')[1].get_text() + '\n\n'
    return string[:-2]