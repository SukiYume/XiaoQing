# 🔐 QingSSH

[中文](README.md) | English

QingSSH supplies SSH remote control to Bot administrators in private and group chats.
It supports saved servers, `~/.ssh/config` import, persistent sessions, streamed output, remote command termination, and image viewing.

## 🔐 Permissions and dependencies

Bot administrator permission is necessary for all Manifest entry points. These entry points support private and group chats.
All group members can see group command output.
Install the SSH backend:

```bash
pip install paramiko
```

Remote commands inherit the target SSH account permissions.
Server configuration, private keys, password secrets, and the Bot administrator list control runtime access together.

## ⌨️ Commands

| Function | Primary command | Manifest aliases |
| --- | --- | --- |
| SSH management and connection | `/ssh` | `/SSH` `/远程` `/ssh连接` `/sshconnect` |
| Disconnect | `/ssh断开` | `/sshdisconnect` `/ssh退出` |
| Server list | `/ssh列表` | `/sshlist` `/ssh服务器` |
| Add server | `/ssh添加` | `/sshadd` |
| Remove server | `/ssh删除` | `/sshremove` `/sshdel` |
| Import SSH config | `/ssh导入` | `/sshimport` |
| View SSH config | `/sshconfig` | `/ssh配置` |
| Active connections | `/ssh状态` | `/sshstatus` `/ssh连接数` `/sshactive` |

The canonical entry points use `/ssh`:

```text
/ssh help
/ssh list
/ssh add [name host [port] [username]]
/ssh remove <server-name>
/ssh import [Host-name|all]
/ssh config
/ssh status
/ssh disconnect [server-name]
/ssh <server-name>
/ssh <username>@<server-name>
```

Standalone entry points share the corresponding `/ssh` subcommand permissions, arguments, and connection policy.

## ⚙️ Server configuration

Saved profiles are in this file:

```text
data/qingssh/servers.json
```

Private-key authentication example:

```json
{
  "myserver": {
    "host": "192.168.1.100",
    "port": 22,
    "username": "root",
    "auth_type": "key",
    "key_path": "/home/bot/.ssh/id_rsa"
  }
}
```

`auth_type` accepts `agent`, `key`, and `password`.
Administrators enter passwords through the `/ssh add` guide in private chat. Core secret storage saves them.
Group additions permit keys and SSH Agent authentication only.

A password selection is rejected.
`servers.json` records the generated `password_ref`.
Configuration and secret writes use a compensating transaction. A failure in either step cleans up resources already created.

Checks control the lengths, characters, and ranges of hosts, ports, usernames, server aliases, and paths.

## 📅 SSH config import

`/ssh config` lists entries with explicit Host names in `~/.ssh/config`.
`/ssh import <Host>` and `/ssh import all` copy usable entries into `servers.json`.
Supported jump-host forms are single-hop `ProxyJump` and structurally validated `ssh -W` ProxyCommand.

Strict verification through `~/.ssh/known_hosts` is always necessary for host keys.
For a new host or changed fingerprint, administrators must verify the fingerprint and update `known_hosts` first.
Configuration parsing rejects local-command ProxyCommand forms.

## 💬 Session operations

After connection, the initiating administrator enters a Core Session in the current private or group chat.
Other group members' messages continue independently. Direct text executes remote commands:

```text
pwd
cd /srv/app
git status
help
停止
退出
```

| Session input | Action |
| --- | --- |
| Ordinary text | Execute a POSIX shell command in the current remote directory |
| `cd <path>` | Change the session working directory |
| `help` | Show session commands |
| `停止` | Terminate the current remote command process group |
| `退出`, `取消`, `exit`, `quit`, `q` | Close the session and SSH connection |

Sessions are isolated by user, private/group chat, and server. The idle limit is 10 minutes.
Each background command has a unique `job_id`.
The SSH channel starts after the session transaction commits. Completion updates state atomically by `job_id`.

`停止` sends `TERM` to the remote process group, then `KILL` if necessary, and cleans up the local channel.
A confirmed remote PID permits a termination status.
During the PID handshake, the reply states that confirmation of remote status is still necessary.

POSIX `sh` and `kill` are necessary on the target system.
With `setsid`, as on Linux, the command owns a process group. `停止` terminates the whole group.
Without `setsid`, the command uses an ordinary background process.

Examples include macOS and Windows OpenSSH with Git Bash. `停止` ends that process directly.
Manual cleanup can be necessary for its additional child processes.

## 📌 Output limits

The complete remote command goes to the SSH shell. QQ displays output within configured limits.
Long output is archived here:

```text
data/qingssh/command_outputs/ssh-output-*.txt
```

```json
{
  "plugins": {
    "qingssh": {
      "command_timeout_seconds": 30,
      "max_connections": 32,
      "qq_max_actions": 6,
      "qq_max_text_chars": 10000,
      "qq_max_message_chars": 1800,
      "qq_head_chars": 6000,
      "qq_tail_chars": 2000,
      "qq_send_interval_seconds": 0.35,
      "qq_send_timeout_seconds": 5,
      "archive_max_bytes": 67108864,
      "archive_tail_bytes": 1048576,
      "archive_retention_files": 20
    }
  }
}
```

`command_timeout_seconds=0` leaves termination of long tasks to the administrator.
QQ limits control action count, total text, message size, kept head/tail, send interval, and wait time.
Archive limits control file bytes, kept tail, and file count.
At the archive limit, the file keeps controlled head and tail sections.

Task cancellation terminates the remote process before it removes the temporary archive.
QQ sees only the filename for a completed long-output archive. Internal logs contain the full local path.

When a thread creates a connection, command channel, or SFTP resource, cancellation waits for creation and closes the returned resource.
A pending remote status keeps an explicit notice.
Text and image delivery treat `False` as failure. Success counts include accepted sends only.

## 🎨 Remote images

Session examples:

```text
showimg plot.png
showimg *.png
showimg plot-??.jpg
showimg ./*
showimg ./plots/*.png
showimg ./* --page 2
showimg /srv/charts/*.jpg --page 3
```

Paths support `./`, relative directories, and absolute directories.
The directory portion must be explicit. The final filename supports `*`, `?`, and `[]`.
For example, `./plots/*.png` matches images in the `plots` subdirectory of the current working directory.

Results use filename lexical order and pages of five images.
The first page omits `--page`. Later pages use `--page N`.
Replies include previous-page and next-page commands.

Pagination covers all matching images in the selected directory.
Each message shows the global index, match count, remote filename, and image in that order.
Each image has a 10 MiB limit.

SFTP downloads use temporary files. Cleanup follows OneBot action acceptance, send errors, or task cancellation.

## ⏰ Lifecycle

First use creates `SSHManager`.
Every minute, `cleanup_orphans` uses Core `silent` mode to compare Core Sessions with active connections.
It removes connections without session owners.
XiaoQing agent one-shot connections have separate ownership and are excluded from this cleanup.
At unload, reload, or Bot shutdown, `shutdown()` cleans up active commands, SSH connections, jump connections, SFTP, and temporary archives.

## 🛠 XiaoQing agent commands

`xiaoqing_agent` uses `qingssh.agent_command` to list server summaries or execute one command on one server.
Summaries contain name, host, port, and username. Authentication material is excluded.
Core issues this service according to the `/ssh` declaration in `plugin.json`.
The caller must have permission to use `/ssh`. Bot administrator status is necessary for this permission.

- Each execution creates a separate connection and closes it immediately afterward.
- The administrator's `/ssh` session remains independent. Output has a 32,000-character limit.
- Timeout or task stop uses the ordinary remote termination procedure.
- Agent and interactive commands use the same wrapper: an exclusive process group with `setsid`, or an ordinary background process without it.

## 💾 Logs and audit

Ordinary logs contain operation type, status, lengths, counts, error categories, and one-way digests.
Log filtering excludes remote commands, output, passwords, private-key content, host credentials, and user text.

## 🩺 Troubleshooting

| Symptom | Check |
| --- | --- |
| Missing Paramiko | Install `paramiko`, then reload the plugin. |
| Host-key failure | Verify the server fingerprint and maintain `~/.ssh/known_hosts`. |
| Authentication failure | Examine `auth_type`, private key, agent, or Core secret. |
| Jump connection failure | Examine `ProxyJump`, `ssh -W` arguments, and the jump-host key. |
| Command timeout | Adjust `command_timeout_seconds`, or send `停止` in the session. |
| QQ shows an archive file | Read the corresponding file in `data/qingssh/command_outputs/` on the Bot host. |
| Session ended | Send `/ssh <server-name>` again. |

## ✅ Development checks

Run these commands from the repository root:

```bash
python -m ruff check plugins/qingssh tests/plugins/qingssh
python -m mypy plugins/qingssh
python -m pytest -q tests/plugins/qingssh -n 2
```
