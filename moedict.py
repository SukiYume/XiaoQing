import requests
from si2cp.langconv import *

def Simplified2Traditional(sentence):
    sentence = Converter('zh-hant').convert(sentence)
    return sentence
def get_dict_result(phrases):
    simplified_sentence = phrases
    traditional_sentence = Simplified2Traditional(simplified_sentence)
    phrases = traditional_sentence
    url = 'https://www.moedict.tw/uni/'+ phrases
    r = requests.get(url)
    if 'title' not in r.json().keys():
        return '我的词典里好像没有这个词呢。'
    else:
        string = ''
        string += r.json()['title'] + ' ' + r.json()['heteronyms'][0]['pinyin'] + ' ' + r.json()['heteronyms'][0]['bopomofo'] + '\n'
        for i in r.json()['heteronyms'][0]['definitions']:
            string += '  ' + i['def']
            if 'type' in i.keys():
                string += ' ' + i['type'] + '\n'
            else:
                string += '\n'
            if 'quote' in i.keys():
                for j in i['quote']:
                    string += '    ' + j + '\n'
            if 'example' in i.keys():
                for j in i['example']:
                    string += '    ' + j + '\n'
        return string[:-1]