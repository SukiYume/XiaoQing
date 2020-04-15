from bot import bot, schedulers

if __name__ == '__main__':
    schedulers.start()
    bot.run(host='172.17.42.1', port=8080)