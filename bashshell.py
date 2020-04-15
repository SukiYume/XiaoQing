import re
import bot
import asyncio
import subprocess, shlex

async def shellexcute(context, content):
    command = shlex.split(content)
    if command[0] == 'echo':
        p = subprocess.Popen(content, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    else:
        p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    while p.poll() == None:
        out = p.stdout.read().strip().decode('utf-8')
        if out:
            if len(out.split('\n')) > 100:
                out = '\n'.join(out.split('\n')[:20]+['...']+out.split('\n')[-10:])
            await bot.bot.send(context, out)
        else:
            await bot.bot.send(context, '好了。')