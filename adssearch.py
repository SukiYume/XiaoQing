import re
import os
import time
import shutil
import requests
from bs4 import BeautifulSoup

import json
with open('api.json', 'r') as f:
    data = json.load(f)
api = data['ads']

def get_ads(content):
    if '|' in content:
        question, qfilter = content.split('|')
        qfilter = re.split(r'[\s\,\，]+', qfilter)
        if qfilter[-1].isdigit():
            rows = str(qfilter[-1])
            qfilter = ','.join(qfilter[:-1])
        else:
            rows = str(3)
            qfilter = ','.join(qfilter)
    else:
        question = re.split(r'[\s\,\，]+', content)
        qfilter = 'title,author,keyword,year,doi'
        if question[-1].isdigit():
            rows = question[-1]
            question = ' '.join(question[:-1])
        else:
            rows = str(3)
            question = ' '.join(question)
    
    headers = {'Authorization': api}
    url = 'https://api.adsabs.harvard.edu/v1/search/query?q=' + question + '&fl=' + qfilter + '&rows=' + rows
    r = requests.get(url, headers = headers)
    tmp = r.json()['response']['docs']

    string = ''
    qfilter = qfilter.split(',')
    qfilter.remove('title')
    for i in range(len(tmp)):
        try:
            string += 'Title: ' + tmp[i]['title'][0] + '\n'
        except:
            continue
        for j in qfilter:
            try:
                tmp_context = tmp[i][j]
                if j == 'year' or j == 'abstract' or j == 'bibcode': 
                    context = tmp_context
                elif j == 'author':
                    if len(tmp_context)<3:
                        context = ', '.join(tmp_context)
                    else:
                        context = ', '.join(tmp_context[:3])
                else:
                    context = tmp_context[0]
                string += j.capitalize() + ': ' + context + '\n'
            except:
                continue
        if i != len(tmp)-1:
            string += '\n'
    if string == '':
        return 'ads中好像没有收录这篇文献。'
    else:
        return string[:-1]

def get_papers(content):
    headers = {'Authorization': api}
    pdffrom = ['EPRINT_PDF', 'PUB_PDF']
    content = content.split(' ')
    if len(content)>1:
        bibcode = content[0]
        if content[1] == '1' or content[1] == 'pub':
            pdffrom = pdffrom[1]
        else:
            pdffrom = pdffrom[0]
    else:
        bibcode = content[0]
        pdffrom = pdffrom[0]
    url = 'https://ui.adsabs.harvard.edu/link_gateway/' + bibcode + '/' + pdffrom
    try:
        r = requests.get(url, headers = headers, allow_redirects=True, timeout=30, stream=True)
        if r.status_code == 404:
            url = 'http://articles.adsabs.harvard.edu/pdf/'+bibcode
            try:
                r = requests.get(url, headers = headers, allow_redirects=True, timeout=30, stream=True)
                if r.status_code == 404:
                    return '论文可能太早了，找不到下载呢。'
            except:
                return '论文可能太早了，找不到下载呢。'
        path = 'papers/'+bibcode+'.pdf'
        webpath = '/var/www/mc/papers/ismcloud/'
        indexpath = '/var/www/mc/papers/index.html'
        with open(path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=2048):
                if chunk:
                    f.write(chunk)
        shutil.copy(path, webpath)
        rewrite_index(bibcode, indexpath)
        return '论文下载好啦'
    except:
        return '好像没有办法下载这篇论文呢。'

def rewrite_index(bibcode, path):
    with open(path,'r') as f:
        html = f.read()
    soup = BeautifulSoup(html, 'html.parser')
    filepath = '/papers/ismcloud/'+bibcode+'.pdf'
    new_atag = soup.new_tag("a")
    new_atag.attrs = {'class':'title','href':filepath}
    title = get_paper_title(bibcode)
    new_atag.string = title+'.pdf'
    new_atag.append(soup.new_tag('br'))
    soup.div.append(new_atag)
    with open(path,'w') as f:
        f.write(str(soup.prettify()))

def get_paper_title(bibcode):
    try:
        headers = {'Authorization': api}
        url = 'https://api.adsabs.harvard.edu/v1/search/query?q=' + bibcode + '&fl=title'
        r = requests.get(url, headers = headers)
        title = r.json()['response']['docs'][0]['title'][0]
        return title
    except:
        return bibcode

def remove_paper(bibcode):
    path = 'papers/'+bibcode+'.pdf'
    filepath = '/var/www/mc/papers/ismcloud/'+bibcode+'.pdf'
    indexpath = '/var/www/mc/papers/index.html'
    if os.path.exists(path):
        os.remove(path)
    else:
        pass
    if os.path.exists(filepath):
        os.remove(filepath)
    else:
        return '没有这个文件哦。'
    with open(indexpath,'r') as f:
        html = f.read()
    soup = BeautifulSoup(html, 'html.parser')
    for i in soup.div.find_all('a'):
        if i.attrs['href'].split('/')[-1] == bibcode+'.pdf':
            i.extract()
    with open(indexpath,'w') as f:
        f.write(str(soup.prettify()))
    return bibcode+' 删除成功。'

def get_paper_list():
    path = '/var/www/mc/papers/index.html'
    with open(path,'r') as f:
        html = f.read()
    soup = BeautifulSoup(html, 'html.parser')
    string = ''
    for i in soup.div.find_all('a'):
        string += i.attrs['href'].split('/')[-1] + '\n'
    return string[:-1]