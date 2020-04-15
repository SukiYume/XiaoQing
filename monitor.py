import re
import psutil

def get_process_status(content):
    username = content[0]
    cmdline = content[1:]
    k = 0
    for i in psutil.pids():
        if psutil.Process(i).username() == username and psutil.Process(i).cmdline() == cmdline:
            k += 1
            pid = i
    if k == 0:
        return '进程 ' + ' '.join(cmdline) + ' 不存在或已结束。'
    else:
        return '进程 ' + str(pid) + ' 正在运行…（在炼了）'
            
def get_process_cmdline(content):
    username = content[0]
    cmdline = []
    for i in psutil.pids():
        if psutil.Process(i).username() == username:
            cmdline.append(psutil.Process(i).cmdline())
    cmdline = [' '.join(i) for i in cmdline]
    string = '\n'.join(cmdline)
    string = username + '在运行的进程有: \n' + string
    return string

def get_process_cpuinfo(content):
    result = get_process_status(content)
    if result[-1] == '。':
        return '这个进程好像不存在呢'
    else:
        pid = int(re.split(r'[\s]+', result)[1])
        cpu_percent = psutil.Process(pid).cpu_percent(interval=1)
        cpu_time_user = psutil.Process(pid).cpu_times()[0]
        cpu_time_syst = psutil.Process(pid).cpu_times()[1]
        string = str(pid) + ' cpu info: \n' + 'cpu_percent: ' + str(cpu_percent) + '%\n' + 'cpu_time: \n' + '    user ' + str(cpu_time_user) + '\n    system ' + str(cpu_time_syst)
        return string

def get_process(content):
    content = re.split(r'[\s\,\，]+', content)
    if content[0] == '状态':
        reply = get_process_status(content[1:])
    elif content[0] == '命令':
        reply = get_process_cmdline(content[1:])
    elif content[0] == 'cpu信息':
        reply = get_process_cpuinfo(content[1:])
    else:
        reply = '您要问关于进程的什么问题，状态还是命令？'
    return reply