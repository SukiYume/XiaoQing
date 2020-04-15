from datetime import datetime

def what_i_can_do():
    functions = [
        '1. 日常聊天',
        '2. alpha question (step/cp)',
        '3. ip or 你的ip',
        '4. 流量 or 你的流量 or 流量报告 or 你还剩多少流量',
        '5. 记忆（or 记住 or 学习） 提问 回答 | 删除对话 | 对话',
        '6. 获取进程命令 用户名 or 获取进程状态 用户名 进程名 or 获取进程cpu信息 用户名 进程名 or 监视进程 用户名 进程名',
        '7. 测速 or 测网速 or 再测一遍',
        '8. 决定（or 做决定） 事情 选项1 选项2 ...',
        '9. 缩写 sentence num（默认词的数量）',
        '10. ads query | filter (title, abstract, doi, year, author, keyword, bibcode, citation_count, ...) num（返回结果数量，默认3）',
        '11. 下载文献 bibcode pub/1｜arxiv   //   删除文献 bibcode   //   文献列表',
        '12. 身份证号 （加上 随机 或者 生成 或者 给我 或者 帮我）',
        '13. 赤化 开头词 长度',
        '14. 某地坐标/某地天气',
        '15. 搜索 搜索内容｜/| 返回结果数量',
        '16. 提醒我 事项 年 月 日 时 分 秒',
        '17. 骂人（max）',
        '18. 翻译（通訳、翻訳、define、translate） 内容 | auto/en/ja2zh or zh2en/ja',
        '19. 台词出处 台词',
        '20. 查词 词',
        '21. adnmb 板块（数量）｜ 串号 ｜ 列表',
        '22. 笔记 分类 （内容）',
        '23. 更新课程 课程名 （课件｜视频）',
        '24. 天文学词典｜astro words (pr num)',
        '25. 执行|execute|Execute command',
        '26. bili(a2b|b2a) url|av|BV',
        '27. jpnb code;code',
        '28. 央六列表'
        ]
    now = datetime.now()
    now = now.strftime("%Y年%m月%d日")
    string = '\n'.join(functions)
    string = '至' + now + ': \n' + string
    return string