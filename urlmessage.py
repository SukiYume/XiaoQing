import requests
from bs4 import BeautifulSoup
from aiocqhttp import MessageSegment

def get_url_message(url):
    r = requests.get(url)
    soup = BeautifulSoup(r.text, 'html.parser')
    title = soup.title.string
    if not title:
        title = ''
    meta = soup.find_all('meta')
    for i in meta:
        if 'name' in i.attrs and i.attrs['name'] == 'description':
            content = i.attrs['content']
    if not content:
        content = ''
    for i in meta:
        if 'content' in i.attrs and i.attrs['content'].split('.')[-1] in ['jpg', 'jpeg', 'png']:
            image_url = i.attrs['content']
    if not image_url:
        image_url = ''
    return MessageSegment.share(url, title=title, content=content, image_url=image_url)
    