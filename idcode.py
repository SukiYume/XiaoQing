import time
import random
import numpy as np
import pandas as pd

def random_id():
    data = pd.read_csv('IDcode.txt')
    locate = np.array(data['行政区划代码'])
    np.random.shuffle(locate)
    address = locate[0]

    for i in data.index:
        if str(address)[:2] == str(data.loc[i,'行政区划代码'])[:2]:
            shengshi = data.loc[i,'单位名称']
            break
    xian = data.loc[(data['行政区划代码']==address),'单位名称'].values[0]

    a1 = (1990, 1, 1, 0, 0, 0, 0, 0, 0)
    a2 = (2020,12,31, 0, 0, 0, 0, 0, 0)

    start = time.mktime(a1)
    end = time.mktime(a2)

    t = random.randint(start,end)
    date_touple = time.localtime(t)
    date = time.strftime("%Y%m%d",date_touple)

    a = random.randint(0,9)
    b = random.randint(0,9)
    c = random.randint(0,9)
    if c%2 != 0:
        gender = '男性'
    else:
        gender = '女性'

    idtmp = str(address) + str(date) + str(a) + str(b) + str(c)

    IDnumberRe = [eval(i) for i in list(idtmp)[::-1]]
    W = [2**i%11 for i in range(18)][1:]
    S = 0
    for i in range(17):
        S += IDnumberRe[i]*W[i]
    CC = (12-(S%11))%11
    if CC == 10:
        CC = 'X'

    idcode = idtmp + str(CC)
    string = '此人位于 ' + shengshi + xian+'\n'+idcode
    return string
    