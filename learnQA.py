import re
import json

def learnQA(content):
    with open('QA.json','r',encoding='utf-8') as f:
        data = json.load(f)
    question, answer = re.split(r'[\s]+', content)
    if question in data.keys():
        if answer in data[question]:
            return '这个我已经知道了。'
        else:
            data[question].append(answer)
    else:
        data[question] = [answer]
    with open('QA.json','w',encoding='utf-8') as f:
        json.dump(data, f)
    return '对话添加成功了！'

def getQA(content):
    with open('QA.json','r',encoding='utf-8') as f:
        data = json.load(f)
    if content:
        string = content + '：\n' + '\n'.join(data[content])
    else:
        string = '\n'.join(list(data.keys()))
    return string

def removeQA(content):
    with open('QA.json','r',encoding='utf-8') as f:
        data = json.load(f)
    content = re.split(r'[\s]+', content)
    if len(content) > 1:
        question, answer = content
    elif len(content) == 1:
        question = content[0]
        answer = 0
        if question == '':
            return '要删除哪个对话？' 
    else:
        return '要删除哪个对话？'
    if question in data.keys():
        if len(data[question])==1:
            popout = data.pop(question)
        else:
            if answer:
                data[question].remove(answer)
                popout = [answer]
            else:
                popout = data.pop(question)
        with open('QA.json','w',encoding='utf-8') as f:
            json.dump(data, f)
        return '对话：'+question+' - '+'|'.join(popout)+' 删除成功了。'
    else:
        return '似乎没有这个对话呢'