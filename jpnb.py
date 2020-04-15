import re
import json
import uuid
import bot
import asyncio
import requests
import datetime
import traceback
from websocket import create_connection

import json
with open('api.json', 'r') as f:
    data = json.load(f)
cookies = data['jpnb']
base = data['jpnburl']
notebook = data['jpnbnotebook']

def send_execute_request(code):
    msg_type = 'execute_request';
    content = { 'code' : code, 'silent':False }
    hdr = { 'msg_id' : uuid.uuid1().hex, 
        'username': 'test', 
        'session': uuid.uuid1().hex, 
        'data': datetime.datetime.now().isoformat(),
        'msg_type': msg_type,
        'version' : '5.0' }
    msg = { 'header': hdr, 'parent_header': hdr, 
        'metadata': {},
        'content': content }
    return msg

async def get_jpnb_res(context, code):
    base = 'https://'+base
    notebook_path = notebook
    url = base + '/api/sessions'
    cookies = {'Cookie': api}
    r = requests.get(url, cookies=cookies)
    session = json.loads(r.text)[0]
    kernel = session["kernel"]

    cookies = cookies
    ws = create_connection("wss://"+base"/api/kernels/"+kernel["id"]+"/channels?session_id"+session["id"], cookie=cookies)
    code = re.split(r'[;]+', code)
    for c in code:
        ws.send(json.dumps(send_execute_request(c.strip())))

    for i in range(0, len(code)):
        try:
            msg_type = ''
            while True:
                rsp = json.loads(ws.recv())
                msg_type = rsp["msg_type"]

                if msg_type == "stream":
                    print(code[i])
                    print(rsp["content"]["text"])
                    await bot.bot.send(context, rsp["content"]["text"].strip())
                elif msg_type == "execute_result":

                    if "image/png" in (rsp["content"]["data"].keys()):
                        print(code[i])
                        print(rsp["content"]["data"]["image/png"])
                        await bot.bot.send(context, '图片现在还发送不了')

                    else:
                        print(code[i])
                        print(rsp["content"]["data"]["text/plain"])
                        await bot.bot.send(context, rsp["content"]["data"]["text/plain"].strip())

                elif msg_type == "display_data":
                    print(code[i])
                    print(rsp["content"]["data"]["image/png"])
                    await bot.bot.send(context, '图片现在还发送不了')

                elif msg_type == "error":
                    print(code[i])
                    errorrsp = [re.sub(r'\[[01]+(;\d{2})*m', '', i) for i in rsp["content"]["traceback"]]
                    print(errorrsp)
                    await bot.bot.send(context, '\n'.join(errorrsp).strip())

                elif msg_type == "status" and rsp["content"]["execution_state"] == "idle":
                    break
        except:
            traceback.print_exc()
            ws.close()
    ws.close()