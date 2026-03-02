"""
Shell 插件配置

包含命令白名单、危险模式、默认超时等配置常量。
"""

from __future__ import annotations

import sys

# ============================================================
# 默认允许的命令白名单
# ============================================================

DEFAULT_WHITELIST: set[str] = {
    # ============================================================
    # 系统信息
    # ============================================================
    "ls", "dir", "pwd", "cd", "cat", "head", "tail", "echo", "date", "uptime",
    "whoami", "hostname", "uname", "df", "du", "free", "top", "ps", "which",
    "env", "printenv", "id", "groups", "w", "who", "last", "lastlog",
    "lscpu", "lsmem", "lsblk", "lsusb", "lspci", "dmidecode",  # 硬件信息
    "arch", "nproc", "getconf", "nvidia-smi", # 系统架构
    
    # ============================================================
    # 文件操作
    # ============================================================
    # 只读
    "find", "grep", "egrep", "fgrep", "wc", "sort", "uniq", "diff", "file", 
    "stat", "tree", "less", "more", "strings", "xxd", "hexdump",
    "locate", "whereis", "readlink", "realpath", "basename", "dirname",
    "md5sum", "sha256sum", "sha1sum", "cksum",  # 校验
    # 读写（谨慎使用）
    "cp", "mv", "mkdir", "touch", "ln",  # 基本操作
    "tar", "gzip", "gunzip", "zip", "unzip", "7z", "rar", "unrar",  # 压缩
    "sed", "awk", "cut", "tr", "paste", "join", "split",  # 文本处理
    "tee", "xargs",
    
    # ============================================================
    # 进程管理
    # ============================================================
    "ps", "top", "htop", "pgrep", "pkill", "killall", "kill",
    "jobs", "bg", "fg", "nohup", "screen", "tmux",
    "nice", "renice", "ionice", "timeout", "time",
    "lsof", "fuser", "pstree",
    # Windows 进程
    "tasklist", "taskkill", "wmic",
    
    # ============================================================
    # 网络诊断
    # ============================================================
    "ping", "ping6", "curl", "wget", "nslookup", "dig", "host",
    "traceroute", "tracert", "mtr", "pathping",
    "netstat", "ss", "ip", "ifconfig", "ipconfig", "arp", "route",
    "nc", "ncat", "telnet", "ssh", "scp", "rsync", "ftp", "sftp",
    "whois", "nmap", "tcpdump", "iptables", "firewall-cmd",
    # Windows 网络
    "netsh", "getmac", "nbtstat", "net",
    
    # ============================================================
    # 磁盘管理
    # ============================================================
    "df", "du", "mount", "umount", "fdisk", "parted", "blkid",
    "lsblk", "findmnt", "sync",
    # Windows 磁盘
    "chkdsk", "diskpart", "fsutil", "vol", "label",
    
    # ============================================================
    # 服务/系统管理
    # ============================================================
    "systemctl", "service", "journalctl", "dmesg", "sysctl",
    "crontab", "at", "batch",
    "shutdown", "reboot", "poweroff", "halt",  # 慎用!
    # Windows 服务
    "sc", "net", "schtasks", "reg",
    
    # ============================================================
    # 用户管理（只读）
    # ============================================================
    "id", "groups", "whoami", "finger", "getent", "passwd",
    "last", "lastlog", "who", "w", "users",
    # Windows 用户
    "net", "whoami", "query",
    
    # ============================================================
    # Python/开发工具
    # ============================================================
    "python", "python3", "pip", "pip3", "pipx", "conda", "mamba",
    "node", "npm", "npx", "yarn", "pnpm", "deno", "bun",
    "git", "gh", "svn", "hg",
    "make", "cmake", "ninja", "gcc", "g++", "clang", "rustc", "cargo",
    "java", "javac", "mvn", "gradle", "go", "ruby", "perl", "php",
    "docker", "docker-compose", "podman", "kubectl", "helm",
    "code", "vim", "nano", "vi", "emacs",
    
    # ============================================================
    # 其他实用工具
    # ============================================================
    "man", "info", "help", "type", "alias", "history",
    "clear", "cls", "reset", "tput",
    "sleep", "wait", "watch", "yes",
    "bc", "expr", "factor", "seq", "shuf",
    "jq", "yq", "xmllint",  # JSON/YAML/XML 处理
    "base64", "openssl", "gpg",  # 编码/加密
    "convert", "identify", "ffmpeg", "ffprobe",  # 媒体处理
    
    # ============================================================
    # Windows 特有命令
    # ============================================================
    "cmd", "powershell", "pwsh", "where", "type", "more", "find", "findstr",
    "attrib", "icacls", "cacls", "takeown",
    "systeminfo", "hostname", "ver", "set", "path",
    "copy", "xcopy", "robocopy", "move", "del", "rd", "rmdir", "md",
    "start", "explorer", "notepad", "mspaint", "calc",
    "control", "mmc", "msconfig", "devmgmt.msc", "diskmgmt.msc",
    "eventvwr", "perfmon", "resmon", "taskmgr",
    "gpresult", "gpupdate", "sfc", "dism",
    "certutil", "cipher", "compact",
    "mode", "chcp", "title", "color", "prompt",
}

# ============================================================
# 危险模式（正则表达式）
# ============================================================

DANGEROUS_PATTERNS: list[str] = [
    r"[;&|`$]",            # 命令链接和变量扩展
    r"[\r\n]",             # 换行
    r"\$\(",               # 命令替换
    r">\s*>",              # 追加重定向
    r">\s*/",              # 重定向到根目录
    r"rm\s+-rf",           # 危险删除
    r"mkfs",               # 格式化
    r"dd\s+if=",           # dd 命令
    r":()\{",              # Fork bomb
    r"chmod\s+777",        # 危险权限
]

# ============================================================
# 其他默认配置
# ============================================================

# 执行超时（秒）
DEFAULT_TIMEOUT: int = 30

# 输出最大字符数
MAX_OUTPUT_LENGTH: int = 4000

# Windows 中文系统编码
WINDOWS_ENCODING: str = "gbk" if sys.platform == "win32" else "utf-8"
