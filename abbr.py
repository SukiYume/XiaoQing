import requests
import pandas as pd

def abbr(content):
    url = 'https://acronymify.com/search?q='
    if content[-1].isdigit():
        words = '+'.join(content[:-1])
        num = content[-1]
        string = ' '.join(content[:-1]) + '\n'
    else:
        words = '+'.join(content)
        num = str(len(content))
        string = ' '.join(content) + '\n'
    url = url + words

    r = requests.get(url)
    result = pd.read_html(r.text)[0]
    columns = list(result.columns)[:-1]
    columns.append('label')
    result.columns = columns
    
    qstr = 'label==' + num
    for i in zip(result.query(qstr).Acronym[-5:],result.query(qstr).Expanded[-5:]):
        string += ' / '.join(i) + '\n'
    return string[:-1]