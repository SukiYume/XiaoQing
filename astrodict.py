import re
import asyncio
import pandas as pd

async def get_astro_trans(content):
    content = re.split(r'\s+', content.strip())
    searchnum = 10
    searchway = 'mohu'
    if content[-1].isdigit():
        searchnum = int(content[-1])
        if content[-2] == 'pr':
            searchway = 'jingque'
            searchkey = content[:-2]
        else:
            searchkey = content[:-1]
    elif content[-1] == 'pr':
        searchway = 'jingque'
        searchkey = content[:-1]
    else:
        searchkey = content
    
    zhPattern = re.compile(r'^[\u4E00-\u9FFF]+$')
    match = zhPattern.search(content[0])
    if match:
        direction = 'zh2en'
    else:
        direction = 'en2zh'
    
    if direction == 'zh2en':
        cedict = pd.read_csv('./astrodict/astrodict_191103ce.txt', sep='\t', header=None)
        cedict.columns = ['C', 'E']
        if searchway == 'mohu':
            for i in searchkey:
                pattern = r'('+i+')'
                cedict = cedict[cedict['C'].str.contains(pattern)]
            cedict.reset_index(inplace=True)
            dictastro = {}
            if len(cedict) < searchnum:
                searchnum = len(cedict)
            string = ''
            for i in range(searchnum):
                string += cedict['C'][i] +'：' + cedict['E'][i] +'\n'
        else:
            i = ''.join(searchkey)
            cedict = cedict.loc[cedict['C'] == i]
            cedict.reset_index(inplace=True)
            dictastro = {}
            if len(cedict) < searchnum:
                searchnum = len(cedict)
            string = ''
            for i in range(searchnum):
                string += cedict['C'][i] +'：' + cedict['E'][i] +'\n'
    else:
        ecdict = pd.read_csv('./astrodict/astrodict_191103ec.txt', sep='\t', header=None)
        ecdict.columns = ['E', 'C']
        if searchway == 'mohu':
            for i in searchkey:
                pattern = r'('+i+')'
                ecdict = ecdict[ecdict['E'].str.contains(pattern)]
            ecdict.reset_index(inplace=True)
            dictastro = {}
            if len(ecdict) < searchnum:
                searchnum = len(ecdict)
            string = ''
            for i in range(searchnum):
                string += ecdict['E'][i] +'：' + ecdict['C'][i] +'\n'
        else:
            i = ''.join(searchkey)
            ecdict = ecdict.loc[ecdict['E'] == i]
            ecdict.reset_index(inplace=True)
            dictastro = {}
            if len(ecdict) < searchnum:
                searchnum = len(ecdict)
            string = ''
            for i in range(searchnum):
                string += ecdict['E'][i] +'：' + ecdict['C'][i] +'\n'
    if string:
        return string[:-1]
    else:
        return '天文学词典中暂时还没收录相关词条。'