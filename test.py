import bot

async def testbot(context, content):
    reply = '1' + content
    await bot.bot.send(context, reply)
    return '发送完了'