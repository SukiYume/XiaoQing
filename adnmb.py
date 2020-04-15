import re
import json
import requests
import numpy as np
from bs4 import BeautifulSoup

def get_head_info(card):
    landtitle = card.find(attrs={'class':'h-threads-info'}).find(attrs={'class':'h-threads-info-title'}).string
    landemail = card.find(attrs={'class':'h-threads-info'}).find(attrs={'class':'h-threads-info-email'}).string
    landcdate = card.find(attrs={'class':'h-threads-info'}).find(attrs={'class':'h-threads-info-createdat'}).string
    landuid = card.find(attrs={'class':'h-threads-info'}).find(attrs={'class':'h-threads-info-uid'}).string
    landid = card.find(attrs={'class':'h-threads-info'}).find(attrs={'class':'h-threads-info-id'}).string
    if landuid:
        return landtitle + ' ' + landemail + ' ' + landcdate + ' ' + landuid + ' ' + landid
    else:
        return landtitle + ' ' + landemail + ' ' + landcdate + ' ' + 'ATM' + ' ' + landid

def get_content(card):
    landcontent = card.find(attrs={'class':'h-threads-content'}).get_text().split('\r\n')
    landcontent = [re.sub(r'[ \xa0]+', '',i) for i in landcontent if i != '']
    landcontent = '\n'.join(landcontent).strip()
    return landcontent

def get_block(num, a):
    Landlord = a[num].find(attrs={'class':'h-threads-item-main'})
    FollowReply = a[num].find(attrs={'class':'h-threads-item-replys'})
    string = ''
    LandlordInfo = get_head_info(Landlord)
    LandlordContent = get_content(Landlord)
    LandlordAll = LandlordInfo + '\n' + LandlordContent + '\n'
    string += ' '.join(LandlordAll.split(' ')[2:])
    if FollowReply:
        FollowReply = FollowReply.find_all(attrs={'class':'h-threads-item-reply'})
        for i in FollowReply:
            tmpInfo = get_head_info(i)
            tmpContent = get_content(i)
            string += ' '.join(tmpInfo.split(' ')[2:]) + '\n' + tmpContent + '\n'
    return string

def get_list(head, reply):
    Landlord = head
    FollowReply = reply
    string = ''
    LandlordInfo = get_head_info(Landlord)
    LandlordContent = get_content(Landlord)
    LandlordAll = LandlordInfo + '\n' + LandlordContent + '\n'
    string += ' '.join(LandlordAll.split(' ')[2:])
    FollowReply = FollowReply.find_all(attrs={'class':'h-threads-item-reply'})
    for i in FollowReply:
        try:
            tmpInfo = get_head_info(i)
            tmpContent = get_content(i)
            string += ' '.join(tmpInfo.split(' ')[2:]) + '\n' + tmpContent + '\n'
        except:
            continue
    return string[:-1]

def get_index_info(half_url, num):
    url = 'https://adnmb2.com' + half_url
    r = requests.get(url)
    soup = BeautifulSoup(r.text, 'html.parser')
    a = soup.find_all(attrs={'class':"h-threads-item uk-clearfix"})
    string = ''
    for i in range(int(num)):
        string += get_block(i, a) + '\n'
    return string[:-2]

def get_page_info(url):
    r = requests.get(url)
    soup = BeautifulSoup(r.text, 'html.parser')
    pages = soup.find(attrs={'class':"uk-pagination uk-pagination-left h-pagination"}).find_all('li')
    pagesnum = []
    for i in pages:
        searchresult = re.search(r'page=(\d+)', str(i))
        if searchresult:
            pagesnum.append(int(searchresult.groups(1)[0]))
    lastpagenum = np.max(pagesnum)
    return lastpagenum

def get_detail_info(listnumber, page):
    page = int(page)
    string = str(listnumber)+' '
    url = 'https://adnmb2.com/t/' + listnumber
    lastpagenum = get_page_info(url)
    if lastpagenum >= page:
        looppage = page
    else:
        looppage = lastpagenum
        string += '该串只有 '+str(lastpagenum)+' 页。\n'
#    for i in range(1, looppage+1):
#        url = 'https://adnmb2.com/t/' + listnumber +'?page=' + str(i)
#        r = requests.get(url)
#        soup = BeautifulSoup(r.text, 'html.parser')
#        head = soup.find(attrs={'class':'h-threads-item-main'})
#        reply = soup.find_all(attrs={'class':'h-threads-item-replys'})[0]
#        string += '\n' + str(i) + '/' + str(looppage) + '/LastPage=' + str(lastpagenum) + '\n'
#        string += get_list(head, reply) + '\n'
    url = 'https://adnmb2.com/t/' + listnumber +'?page=' + str(looppage)
    r = requests.get(url)
    soup = BeautifulSoup(r.text, 'html.parser')
    head = soup.find(attrs={'class':'h-threads-item-main'})
    reply = soup.find_all(attrs={'class':'h-threads-item-replys'})[0]
    string += '\n' + str(looppage) + '/LastPage=' + str(lastpagenum) + '\n'
    string += get_list(head, reply) + '\n'
    return string[:-1]

def get_card_info(listnumber, replynumber):
    url = 'https://adnmb2.com/t/' + listnumber
    lastpagenum = get_page_info(url)
    string = ''
    for i in range(1, lastpagenum+1):
        url = 'https://adnmb2.com/t/' + listnumber +'?page=' + str(i)
        r = requests.get(url)
        soup = BeautifulSoup(r.text, 'html.parser')
        Landlord = soup.find(attrs={'class':'h-threads-item-main'})
        FollowReply = soup.find_all(attrs={'class':'h-threads-item-replys'})[0]
        LandlordInfo = get_head_info(Landlord)
        LandlordContent = get_content(Landlord)
        if LandlordInfo.split('No.')[-1] == replynumber:
            LandlordAll = LandlordInfo + '\n' + LandlordContent
            string += ' '.join(LandlordAll.split(' ')[2:])
            break
        FollowReply = FollowReply.find_all(attrs={'class':'h-threads-item-reply'})
        for j in FollowReply:
            try:
                tmpInfo = get_head_info(j)
                tmpInfonumber = tmpInfo.split('No.')[-1]
                if tmpInfonumber == replynumber:
                    tmpContent = get_content(j)
                    string += ' '.join(tmpInfo.split(' ')[2:]) + '\n' + tmpContent
                    break
                    break
            except:
                continue
    return string

def get_adnmb_list():
    url = 'https://adnmb2.com/Forum'
    r = requests.get(url)
    soup = BeautifulSoup(r.text, 'html.parser')
    content_list = soup.find(attrs={'id':'h-menu-content'})
    content_list_dict = {}
    try:
        for i in content_list.find_all('li'):
            for j in i.find_all('li'):
                content_list_dict[re.split(r'([\(W]+)|(New)', j.get_text().strip())[0]] = j.a['href']
        with open('adnmblist.json', 'w+') as f:
            json.dump(content_list_dict, f)
    except:
        with open('adnmblist.json', 'r+') as f:
            content_list_dict = json.load(f)
    return content_list_dict
content_list_dict = get_adnmb_list()

def get_adnmb(content):
    if content:
        content = content.split(' ')
        if content[0].isdigit():
            page = 1
            if len(content)>1:
                if len(content[1]) > 5:
                    try:
                        card_info = get_card_info(content[0], content[1])
                        if card_info:
                            return card_info
                        else:
                            return '是不是打错了回复号？'
                    except:
                        return '这个回复是不是被删除了'
                else:
                    page = content[1]
            try:
                return get_detail_info(content[0], page)
            except:
                return '是不是打错了串号？'
        else:
            if len(content)<2:
                if content[0] == '列表':
                    global content_list_dict
                    content_list_dict = get_adnmb_list()
                    content_list = list(content_list_dict.keys())
                    return ' | '.join(content_list)
                else:
                    try:
                        half_url = content_list_dict[content[0]]
                        return get_index_info(half_url, 3)
                    except:
                        return '好像没有这个板块哦。'
            else:
                try:
                    half_url = content_list_dict[content[0]]
                    return get_index_info(half_url, content[1])
                except:
                    return '好像没有这个板块哦。'
    else:
        return get_index_info(content_list_dict['欢乐恶搞'], '3')