"""
SSH 连接管理器

负责：
- 加载和保存服务器配置
- 读取 ~/.ssh/config
- 管理 SSH 连接
- 执行远程命令
"""

import asyncio
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Optional

from .config import (
    COMMAND_TIMEOUT, 
    MAX_OUTPUT_LENGTH, 
    CONNECT_TIMEOUT,
    EXIT_CODE_INTERRUPTED,
    EXIT_CODE_ERROR,
)

# 尝试导入 paramiko，如果未安装则提供友好提示
try:
    import paramiko
    from paramiko.config import SSHConfig
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False
    paramiko = None  # type: ignore
    SSHConfig = None  # type: ignore

class SSHManager:
    """
    SSH 连接管理器

    负责管理 SSH 连接、执行命令和服务器配置。
    所有连接使用 user_id:server_name 作为键进行用户隔离。
    """

    def __init__(self, data_dir: Path, context=None):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.servers_file = data_dir / "servers.json"
        self.connections: dict[str, "paramiko.SSHClient"] = {}
        self._ssh_config: Optional["SSHConfig"] = None
        self.context = context  # 用于日志记录
        self.servers = {}  # 初始化为空，等待异步加载
        # 活跃的命令通道：user_id:server_name -> channel
        self.active_channels: dict[str, Any] = {}
        # 标记是否已初始化
        self._initialized = False

    def _log(self, level: str, message: str, **kwargs):
        """
        辅助方法：记录日志（如果上下文有 logger）

        Args:
            level: 日志级别 (debug, info, warning, error)
            message: 日志消息
            **kwargs: 传递给 logger 的额外参数
        """
        if self.context and hasattr(self.context, 'logger'):
            logger = self.context.logger
            log_method = getattr(logger, level, None)
            if log_method and callable(log_method):
                log_method(message, **kwargs)

    async def initialize(self):
        """
        初始化管理器
        
        异步加载服务器配置和 SSH Config。
        """
        await self._load_servers()
        await self._load_ssh_config()
        self._initialized = True
    
    async def _load_servers(self):
        """
        加载服务器配置（异步）
        
        在线程池中执行文件读取，避免阻塞事件循环。
        """
        if self.servers_file.exists():
            try:
                # 在线程池中执行文件读取
                def _read_servers():
                    with open(self.servers_file, "r", encoding="utf-8") as f:
                        return json.load(f)
                
                self.servers = await asyncio.to_thread(_read_servers)
            except Exception as e:
                self._log("error", f"Failed to load servers.json: {e}")
                self.servers = {}
        else:
            self.servers = {}
    
    async def _load_ssh_config(self):
        """
        加载 ~/.ssh/config 文件（异步）
        
        在线程池中执行文件读取，避免阻塞事件循环。
        对于挂载在网络驱动器上的 config 文件尤其重要。
        """
        if not PARAMIKO_AVAILABLE or SSHConfig is None:
            return
        
        ssh_config_path = Path.home() / ".ssh" / "config"
        if ssh_config_path.exists():
            try:
                # 在线程池中执行文件读取和解析
                def _read_and_parse_config():
                    config = SSHConfig()
                    with open(ssh_config_path, "r", encoding="utf-8") as f:
                        config.parse(f)
                    return config
                
                self._ssh_config = await asyncio.to_thread(_read_and_parse_config)
            except Exception:
                self._ssh_config = None
    
    def get_ssh_config_hosts(self) -> list[str]:
        """获取 ~/.ssh/config 中的所有 Host 名称"""
        if self._ssh_config is None:
            return []
        
        hosts = []
        for host in self._ssh_config.get_hostnames():
            # 跳过通配符和 * 配置
            if '*' not in host and '?' not in host:
                hosts.append(host)
        return hosts
    
    def get_ssh_config_for_host(self, host: str) -> Optional[dict]:
        """获取 ~/.ssh/config 中特定 Host 的配置"""
        if self._ssh_config is None:
            return None
        
        try:
            config = self._ssh_config.lookup(host)
            # 有 hostname 或有 proxycommand 都是有效配置
            # 对于使用 ProxyCommand 跳转的配置，可能没有 hostname，此时使用 host 名称
            if config.get('hostname') or config.get('proxycommand'):
                return {
                    'hostname': config.get('hostname', host),
                    'port': int(config.get('port', 22)),
                    'user': config.get('user', 'root'),
                    'identityfile': config.get('identityfile', []),
                    'proxycommand': config.get('proxycommand'),
                }
        except Exception:
            pass
        return None
    
    async def import_from_ssh_config(self, host_name: str, alias: Optional[str] = None) -> tuple[bool, str]:
        """从 ~/.ssh/config 导入服务器配置"""
        config = self.get_ssh_config_for_host(host_name)
        if not config:
            return False, f"在 ~/.ssh/config 中未找到 Host: {host_name}"
        
        name = alias or host_name
        if name in self.servers:
            return False, f"服务器 '{name}' 已存在"
        
        # 确定密钥路径
        key_path = None
        if config.get('identityfile'):
            # identityfile 是一个列表，取第一个
            key_path = Path(os.path.expanduser(config['identityfile'][0])).as_posix()
        
        server_config = {
            "host": config['hostname'],
            "port": config['port'],
            "username": config['user'],
            "auth_type": "key" if key_path else "agent",
            "key_path": key_path,
            "from_ssh_config": True,  # 标记来源
            "ssh_config_host": host_name,  # 原始 Host 名
        }
        
        # 保存 ProxyCommand（如果有）
        if config.get('proxycommand'):
            server_config["proxycommand"] = config['proxycommand']
        
        self.servers[name] = server_config
        await self._save_servers()
        
        # 根据是否有 proxycommand 生成不同的提示信息
        if config.get('proxycommand'):
            return True, f"✅ 已导入: {name} (通过跳板机: {config['proxycommand'][:50]}...)"
        return True, f"✅ 已导入: {name} ({config['hostname']}:{config['port']})"
    
    async def import_all_from_ssh_config(self) -> tuple[int, list[str]]:
        """从 ~/.ssh/config 导入所有服务器"""
        hosts = self.get_ssh_config_hosts()
        imported = []
        for host in hosts:
            if host not in self.servers:
                success, _ = await self.import_from_ssh_config(host)
                if success:
                    imported.append(host)
        return len(imported), imported
    
    async def _save_servers(self):
        """
        保存服务器配置（异步）
        
        在线程池中执行文件写入，避免阻塞事件循环。
        """
        def _write_servers():
            with open(self.servers_file, "w", encoding="utf-8") as f:
                json.dump(self.servers, f, ensure_ascii=False, indent=4)
        
        await asyncio.to_thread(_write_servers)
    
    async def add_server(
        self,
        name: str,
        host: str,
        port: int = 22,
        username: str = "root",
        auth_type: str = "password",
        password: Optional[str] = None,
        password_ref: Optional[str] = None,
        key_path: Optional[str] = None,
    ) -> bool:
        """
        添加服务器配置
        
        Args:
            name: 服务器名称
            host: 主机地址
            port: SSH 端口
            username: 用户名
            auth_type: 认证方式 (password/key/agent)
            password: 密码（不推荐，建议使用 password_ref）
            password_ref: 密码引用（指向 secrets.json 中的 key）
            key_path: 密钥文件路径
            
        Returns:
            是否添加成功
        """
        self.servers[name] = {
            "host": host,
            "port": port,
            "username": username,
            "auth_type": auth_type,
            "key_path": key_path,
        }
        
        # 优先使用 password_ref，否则存储密码（不推荐）
        if password_ref:
            self.servers[name]["password_ref"] = password_ref
        elif password:
            self.servers[name]["password"] = password
            
        await self._save_servers()
        
        self._log("info", f"Server '{name}' added: {host}:{port}")
        
        return True
    
    async def remove_server(self, name: str) -> bool:
        """删除服务器配置"""
        if name in self.servers:
            del self.servers[name]
            await self._save_servers()
            return True
        return False
    
    def get_server(self, name: str) -> Optional[dict]:
        """获取服务器配置"""
        return self.servers.get(name)
    
    def list_servers(self) -> dict[str, dict]:
        """列出所有服务器"""
        return self.servers
    
    def _build_connection_key(self, user_id, group_id, name: str) -> str:
        """
        生成用户隔离的连接标识符
        
        Args:
            user_id: 用户 ID
            group_id: 群 ID (可为 None)
            name: 服务器名称
            
        Returns:
            格式为 "user_id:group_id:server_name" 的连接键
        """
        return f"{str(user_id)}:{str(group_id)}:{name}"

    async def connect(self, user_id: str, group_id: Optional[str], name: str, use_ssh_config_direct: bool = False, username_override: str = None) -> tuple[bool, str]:
        """
        连接到服务器（用户+群隔离）
        
        使用线程池执行同步的 SSH 连接操作，避免阻塞事件循环。
        
        Args:
            user_id: 用户 ID
            group_id: 群 ID
            name: 服务器名称
            use_ssh_config_direct: 是否直接使用 ssh_config
            username_override: 用户名覆盖（用于 user@server 格式）
        """
        if not PARAMIKO_AVAILABLE:
            return False, "❌ 未安装 paramiko 库，请运行: pip install paramiko"
        
        server = self.get_server(name)
        ssh_config = None
        
        # 如果没有保存的配置，或者明确要求使用 ssh_config，尝试从 ~/.ssh/config 获取
        if server is None or use_ssh_config_direct:
            ssh_config = self.get_ssh_config_for_host(name)
            if ssh_config:
                server = {
                    "host": ssh_config['hostname'],
                    "port": ssh_config['port'],
                    "username": ssh_config['user'],
                    "auth_type": "key" if ssh_config.get('identityfile') else "agent",
                    "key_path": os.path.expanduser(ssh_config['identityfile'][0]) if ssh_config.get('identityfile') else None,
                }
                if ssh_config.get('proxycommand'):
                    server["proxycommand"] = ssh_config['proxycommand']
        
        if not server:
            available_hosts = self.get_ssh_config_hosts()
            if available_hosts:
                hint = f"\n💡 ~/.ssh/config 中可用的 Host: {', '.join(available_hosts[:5])}"
                if len(available_hosts) > 5:
                    hint += f" ... (共 {len(available_hosts)} 个)"
                return False, f"❌ 服务器 '{name}' 不存在{hint}"
            return False, f"❌ 服务器 '{name}' 不存在"
        
        # 如果提供了用户名覆盖，创建副本并修改用户名
        if username_override:
            server = server.copy()
            server['username'] = username_override
        
        try:
            client = paramiko.SSHClient()
            # 使用 WarningPolicy 而非 AutoAddPolicy 以避免中间人攻击风险
            # 未知的 host key 会记录警告但允许连接（首次连接）
            client.set_missing_host_key_policy(paramiko.WarningPolicy())
            
            known_hosts_path = Path.home() / ".ssh" / "known_hosts"
            if known_hosts_path.exists():
                try:
                    client.load_host_keys(str(known_hosts_path))
                except Exception as e:
                    self._log("warning", f"Failed to load known_hosts: {e}")
            
            connect_kwargs = {
                "hostname": server["host"],
                "port": server["port"],
                "username": server["username"],
                "timeout": CONNECT_TIMEOUT,
            }
            
            auth_type = server.get("auth_type", "agent")
            
            if auth_type == "key":
                if server.get("key_path"):
                    connect_kwargs["key_filename"] = server["key_path"]
            elif auth_type == "password":
                password = None
                if server.get("password_ref") and self.context:
                    try:
                        password = self.context.get_secret(server["password_ref"])
                    except Exception:
                        pass
                
                if not password and server.get("password"):
                    password = server["password"]
                    
                if password:
                    connect_kwargs["password"] = password
            elif auth_type == "agent":
                connect_kwargs["allow_agent"] = True
                connect_kwargs["look_for_keys"] = True
            
            proxycommand = server.get("proxycommand")
            if proxycommand:
                proxycommand = proxycommand.replace("%h", server["host"])
                proxycommand = proxycommand.replace("%p", str(server["port"]))
                proxycommand = proxycommand.replace("%r", server["username"])
                
                # Windows 平台下 ProxyCommand 的特殊处理
                # paramiko 的 ProxyCommand 在 Windows 上使用管道，会导致 select() 调用失败
                # 解决方案：手动解析 jump host 并建立 direct-tcpip 通道
                use_proxy_command_fallback = True
                
                if sys.platform == 'win32' and 'ssh' in proxycommand:
                    try:
                        # 尝试更健壮地解析 Jump Host
                        # ProxyCommand 通常格式: ssh -W host:port [-p port] [-l user] jump_host
                        # 使用 shlex 处理引号裹住的路径（如 IdentityFile）
                        try:
                            parts = shlex.split(proxycommand)
                        except ValueError:
                            parts = proxycommand.split()
                        
                        # 解析 SSH 命令行参数以找到目标主机
                        jump_dest = None
                        # 这些 SSH 选项后面紧跟着参数
                        ssh_opts_with_arg = {
                            '-B', '-b', '-c', '-D', '-E', '-e', '-F', '-I', '-i', 
                            '-J', '-L', '-l', '-m', '-O', '-o', '-p', '-Q', '-R', 
                            '-S', '-W', '-w'
                        }
                        
                        # 跳过命令本身 (ssh)
                        idx = 1
                        while idx < len(parts):
                            part = parts[idx]
                            
                            # 碰到 - 开头认为是选项
                            if part.startswith('-'):
                                # 检查是否是 -p2222 这种连写
                                flag = part[:2]
                                if len(part) > 2 and flag in ssh_opts_with_arg:
                                    # 连写形式，当前部分已经包含参数，跳过当前
                                    idx += 1
                                    continue
                                
                                if part in ssh_opts_with_arg:
                                    # 也就是 -p 2222 这种分开的形式，需要多跳过一个 args
                                    idx += 2
                                else:
                                    # 假设是不带参数的选项 (如 -v, -4, -C)
                                    idx += 1
                            else:
                                # 找到非选项参数，即为目标主机
                                jump_dest = part
                                break
                                
                        # 如果解析失败，作为兜底，取最后一个参数
                        if not jump_dest and len(parts) > 1:
                            jump_dest = parts[-1]
                        
                        if self.context and hasattr(self.context, 'logger'):
                            self.context.logger.info(f"Attempting Windows ProxyJump workaround for: {jump_dest}")

                        # 解析跳板机用户名和地址
                        jump_user = None
                        jump_host_name = jump_dest
                        if '@' in jump_dest:
                            jump_user, jump_host_name = jump_dest.split('@', 1)
                        
                        # 获取跳板机的配置
                        jump_conf = self.get_ssh_config_for_host(jump_host_name)
                        if not jump_conf:
                            # 如果没有配置，使用默认值
                            jump_conf = {
                                'hostname': jump_host_name,
                                'port': 22,
                                'user': 'root'
                            }
                        
                        # 准备跳板机连接参数
                        j_kwargs = {
                            "hostname": jump_conf.get('hostname', jump_host_name),
                            "port": jump_conf.get('port', 22),
                            # 命令行中的用户优先于配置中的用户
                            "username": jump_user if jump_user else jump_conf.get('user', 'root'),
                            "timeout": CONNECT_TIMEOUT
                        }
                        
                        if jump_conf.get('identityfile'):
                            j_kwargs['key_filename'] = os.path.expanduser(jump_conf['identityfile'][0])
                            
                        # 创建跳板机客户端
                        jump_client = paramiko.SSHClient()
                        jump_client.set_missing_host_key_policy(paramiko.WarningPolicy())
                        
                        # 加载 known_hosts
                        if known_hosts_path.exists():
                            try:
                                jump_client.load_host_keys(str(known_hosts_path))
                            except Exception:
                                pass
                        
                        # 连接跳板机
                        # 注意：这里需要 await run_in_thread
                        await asyncio.to_thread(jump_client.connect, **j_kwargs)
                        
                        # 建立通道
                        dest_addr = (server["host"], server["port"])
                        src_addr = ('0.0.0.0', 0)
                        sock = jump_client.get_transport().open_channel("direct-tcpip", dest_addr, src_addr)
                        
                        connect_kwargs["sock"] = sock
                        # 将 jump_client 附加到主 client 上，防止被 GC，并在断开时关闭
                        client._jump_client = jump_client
                        
                        use_proxy_command_fallback = False

                        self._log("info", f"Jump host connected: {jump_host_name}")

                    except Exception as e:
                        self._log("warning", f"Windows ProxyJump workaround failed: {e}. Falling back to standard ProxyCommand.")

                if use_proxy_command_fallback:
                    self._log("info", f"Using ProxyCommand: {proxycommand}")

                    proxy = paramiko.ProxyCommand(proxycommand)
                    connect_kwargs["sock"] = proxy

            self._log("info", f"User {user_id} (Group {group_id}) connecting to {name}...")

            await asyncio.to_thread(client.connect, **connect_kwargs)

            key = self._build_connection_key(user_id, group_id, name)
            self.connections[key] = client

            self._log("info", f"Connected to {name} successfully")
            
            return True, f"✅ 成功连接到 {name} ({server['host']})"
        
        except paramiko.AuthenticationException as e:
            self._log("error", f"Authentication failed for {name}: {e}")
            return False, (
                "❌ 认证失败\n"
                f"服务器: {server['host']}:{server['port']}\n"
                f"用户: {server['username']}\n\n"
                "💡 请检查:\n"
                "  1. 用户名和密码是否正确\n"
                "  2. 如使用密钥，确保 ssh-agent 已运行\n"
                f"  3. 密钥路径: {server.get('key_path', 'N/A')}"
            )
        except paramiko.SSHException as e:
            error_msg = str(e)
            self._log("error", f"SSH error for {name}: {e}")
            
            if "does not match" in error_msg or "Host key" in error_msg:
                known_hosts_path = Path.home() / ".ssh" / "known_hosts"
                return False, (
                    "❌ SSH Host Key 不匹配\n"
                    f"服务器: {server['host']}:{server['port']}\n\n"
                    "这通常发生在：\n"
                    "  • 服务器重新安装了系统\n"
                    "  • 服务器重新生成了密钥\n\n"
                    f"请检查: {known_hosts_path}"
                )
            
            return False, f"❌ SSH 连接错误: {e}\n\n💡 请检查网络连接和服务器状态"
        except Exception as e:
            self._log("error", f"Connection failed for {name}: {e}", exc_info=True)
            return False, f"❌ 连接失败: {e}"
    
    def disconnect(self, user_id: str, group_id: Optional[str], name: str) -> bool:
        """断开连接（用户+群隔离）"""
        key = self._build_connection_key(user_id, group_id, name)

        # 先停止正在运行的命令
        self.stop_command(user_id, group_id, name)

        if key in self.connections:
            client = self.connections[key]
            try:
                client.close()
            except Exception as e:
                # 记录关闭异常但继续清理
                self._log("warning", f"Error closing SSH client for {name}: {e}")
            finally:
                # 确保 cleanup 无论是否异常都执行
                # 如果存在跳板机连接，也一并关闭
                if hasattr(client, '_jump_client'):
                    try:
                        client._jump_client.close()
                    except Exception as e:
                        self._log("warning", f"Error closing jump host: {e}")

                self.connections.pop(key, None)
            return True
        return False
    
    def is_connected(self, user_id: str, group_id: Optional[str], name: str) -> bool:
        """检查是否已连接（用户+群隔离）"""
        key = self._build_connection_key(user_id, group_id, name)
        if key not in self.connections:
            return False
        try:
            transport = self.connections[key].get_transport()
            return transport is not None and transport.is_active()
        except Exception:
            # 如果底层断开了，清理一下
            if key in self.connections:
                del self.connections[key]
            return False
    
    def get_active_connections(self) -> list[dict[str, str]]:
        """
        获取当前活跃连接列表 (解析 key)
        
        Returns:
            list[dict]: 包含 user_id, group_id, server_name 的字典列表
        """
        active_list = []
        for key in self.connections.keys():
            # key format: user_id:group_id:server_name
            parts = key.split(":", 2)
            if len(parts) == 3:
                active_list.append({
                    "user_id": parts[0],
                    "group_id": parts[1],
                    "server_name": parts[2]
                })
        return active_list

    def stop_command(self, user_id: str, group_id: Optional[str], name: str) -> bool:
        """
        停止指定服务器上正在运行的命令（用户+群隔离）
        
        先尝试发送 Ctrl+C (SIGINT) 优雅终止，然后关闭通道。
        """
        key = self._build_connection_key(user_id, group_id, name)
        if key in self.active_channels:
            channel = self.active_channels[key]
            try:
                # 尝试发送 Ctrl+C (SIGINT) 信号
                if channel.send_ready():
                    channel.send("\x03")  # Ctrl+C
            except Exception:
                pass
            
            try:
                # 关闭通道
                channel.close()
            except Exception:
                pass
            
            self.active_channels.pop(key, None)
            return True
        return False
    
    async def execute_command_stream(
        self, 
        user_id: str,
        group_id: Optional[str],
        name: str, 
        command: str, 
        output_callback,
        use_pty: bool = False
    ) -> int:
        """
        流式执行命令（用户+群隔离）
        
        通过回调函数实时推送命令输出。
        """
        if not self.is_connected(user_id, group_id, name):
            await output_callback("❌ 未连接到服务器")
            return EXIT_CODE_ERROR
        
        key = self._build_connection_key(user_id, group_id, name)
            
        channel = None  # Initialize to prevent UnboundLocalError in finally block
        decoder = None
        
        try:
            client = self.connections[key]
            transport = client.get_transport()
            
            # 使用 asyncio.to_thread 包装所有阻塞的 paramiko 调用
            def open_channel():
                ch = transport.open_session()
                if use_pty:
                    ch.get_pty()
                ch.set_combine_stderr(True)
                ch.exec_command(command)
                return ch
            
            channel = await asyncio.to_thread(open_channel)
            
            # 使用隔离的 Key
            self.active_channels[key] = channel
            
            # 创建增量 UTF-8 解码器
            import codecs
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            
            # 主循环：等待命令执行完成
            while not channel.exit_status_ready():
                # 使用 to_thread 包装阻塞的 recv 调用
                if channel.recv_ready():
                    data = await asyncio.to_thread(channel.recv, 4096)
                    if data:
                        # 使用增量解码器正确处理多字节字符
                        text = decoder.decode(data)
                        if text:
                            await output_callback(text)
                
                await asyncio.sleep(0.05)  # 减少延迟提高响应性
                
                # 检查是否被中断
                if key not in self.active_channels:
                    if not channel.active:
                        return EXIT_CODE_INTERRUPTED
            
            # 命令执行完成，读取剩余输出直到 EOF
            # recv_ready() 不可靠，应该读取直到返回空字节 b''
            while True:
                data = await asyncio.to_thread(channel.recv, 4096)
                if not data:  # EOF
                    break
                text = decoder.decode(data)
                if text:
                    await output_callback(text)
            
            # 解码剩余的缓冲区
            remaining = decoder.decode(b'', final=True)
            if remaining:
                await output_callback(remaining)
            
            # exit_status_ready() 为 True 时，退出码已经可用
            # 使用 exit_status 属性获取（非阻塞），而不是 recv_exit_status() 方法（阻塞）
            exit_code = channel.exit_status
            return exit_code
            
        except Exception as e:
            await output_callback(f"\n❌ 执行出错: {e}")
            if self.context and hasattr(self.context, 'logger'):
                self.context.logger.error(f"Command execution error: {e}", exc_info=True)
            return EXIT_CODE_ERROR
        finally:
            self.active_channels.pop(key, None)
            if channel is not None:
                try:
                    # 包装 close 调用以防阻塞
                    await asyncio.to_thread(channel.close)
                except Exception:
                    pass

    async def execute_command(self, user_id: str, group_id: Optional[str], name: str, command: str) -> tuple[bool, str]:
        """
        执行命令并返回完整输出（非流式）
        """
        if not self.is_connected(user_id, group_id, name):
            return False, "❌ 未连接到服务器"
        
        output_buffer = []
        async def collector(text):
            output_buffer.append(text)
            
        try:
            exit_code = await asyncio.wait_for(
                self.execute_command_stream(user_id, group_id, name, command, collector),
                timeout=COMMAND_TIMEOUT
            )
            
            result = "".join(output_buffer)
            if len(result) > MAX_OUTPUT_LENGTH:
                result = result[:MAX_OUTPUT_LENGTH] + f"\n\n... (输出被截断)"
                
            return True, result.strip() if result.strip() else "(无输出)"
            
        except asyncio.TimeoutError:
            self.stop_command(user_id, group_id, name)
            return False, f"❌ 命令执行超时 ({COMMAND_TIMEOUT}s)"
        except Exception as e:
            return False, f"❌ 执行失败: {e}"

    async def download_file(self, user_id: str, group_id: Optional[str], name: str, remote_path: str, local_path: str) -> tuple[bool, str]:
        """
        从远程服务器下载文件到本地
        
        Args:
            user_id: 用户 ID
            group_id: 群 ID
            name: 服务器名称
            remote_path: 远程文件路径
            local_path: 本地保存路径
            
        Returns:
            (success, message)
        """
        if not self.is_connected(user_id, group_id, name):
            return False, "❌ 未连接到服务器"
        
        key = self._build_connection_key(user_id, group_id, name)
        
        if key not in self.connections:
            return False, f"❌ 服务器 '{name}' 未连接"
        
        try:
            client = self.connections[key]
            sftp = await asyncio.to_thread(client.open_sftp)
            
            try:
                await asyncio.to_thread(sftp.get, remote_path, local_path)
                return True, f"✅ 文件已下载: {remote_path}"
            finally:
                await asyncio.to_thread(sftp.close)
                
        except FileNotFoundError:
            return False, f"❌ 远程文件不存在: {remote_path}"
        except PermissionError:
            return False, f"❌ 权限不足，无法访问: {remote_path}"
        except Exception as e:
            if self.context and hasattr(self.context, 'logger'):
                self.context.logger.error(f"Download failed for {remote_path}: {e}", exc_info=True)
            return False, f"❌ 下载失败: {e}"

    async def list_files(self, user_id: str, group_id: Optional[str], name: str, remote_dir: str, pattern: str = "*") -> tuple[bool, list]:
        """
        列出远程目录中匹配模式的文件
        
        Args:
            user_id: 用户 ID
            group_id: 群 ID
            name: 服务器名称
            remote_dir: 远程目录路径
            pattern: 文件匹配模式（支持通配符）
            
        Returns:
            (success, file_list)
        """
        if not self.is_connected(user_id, group_id, name):
            return False, []
        
        key = self._build_connection_key(user_id, group_id, name)
        
        if key not in self.connections:
            return False, []
        
        try:
            client = self.connections[key]
            sftp = await asyncio.to_thread(client.open_sftp)
            
            try:
                import fnmatch
                files = []
                
                def _list_files():
                    return sftp.listdir(remote_dir)
                
                all_files = await asyncio.to_thread(_list_files)
                
                for filename in all_files:
                    if fnmatch.fnmatch(filename, pattern):
                        files.append(filename)
                
                return True, files
            finally:
                await asyncio.to_thread(sftp.close)
                
        except Exception as e:
            if self.context and hasattr(self.context, 'logger'):
                self.context.logger.error(f"List files failed for {remote_dir}: {e}", exc_info=True)
            return False, []
    
    def close_all(self):
        """
        关闭所有连接
        
        应在插件卸载或重启时调用以释放资源。
        """
        if self.context and hasattr(self.context, 'logger'):
            self.context.logger.info(f"Closing all SSH connections ({len(self.connections)} active)")
            
        for key in list(self.connections.keys()):
            # key 是 "user_id:server_name"
            if key in self.connections:
                try:
                    self.connections[key].close()
                except Exception:
                    pass
                del self.connections[key]
        
        # 清理活跃通道
        self.active_channels.clear()

async def get_manager(context) -> SSHManager:
    """
    获取 SSH 管理器实例
    
    使用 context.state 存储单例，实现插件级别的持久化。
    创建后会自动进行异步初始化（加载配置文件）。
    
    Args:
        context: 插件上下文
        
    Returns:
        SSHManager 实例（已初始化）
    """
    # 从 context.state 中获取管理器
    manager = context.state.get("ssh_manager")
    
    if manager is not None:
        # 更新上下文以便日志能关联到当前请求
        manager.context = context
        if not manager._initialized:
            await manager.initialize()
        return manager
    
    # 创建新实例
    data_dir = Path(context.plugin_dir) / "data"
    manager = SSHManager(data_dir, context=context)
    
    # 异步初始化（加载配置文件）
    await manager.initialize()
    
    # 保存到 state 中
    context.state["ssh_manager"] = manager
    
    if hasattr(context, 'logger'):
        context.logger.info("SSH manager initialized")
    
    return manager
