import re
import json

def get_memo(content):
    with open('memo.json','r',encoding='utf-8') as f:
        data = json.load(f)
    content = re.split(r'[\s]+', content)
    if len(content)<=1:
        if content[0] in data.keys():
            return '\n'.join(data[content[0]])
        elif content[0] == '':
            return '\n'.join(list(data.keys()))
        else:
            return '笔记中没有这个条目哦。'
    else:
        fl = content[0]
        detail = ' '.join(content[1:])
        if fl in data.keys():
            data[fl].append(detail)
        else:
            data[fl] = [detail]
        with open('memo.json','w',encoding='utf-8') as f:
            json.dump(data, f)
        return '笔记添加，该条目位于 ' + fl + ' 分类下。'