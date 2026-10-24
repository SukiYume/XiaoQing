# 💻 Shell

[中文](README.md) | English

`shell` lets Bot administrators execute one local command on the Bot host through private or group QQ chats.
Each call creates a separate process. The working directory and environment state are established again for each call.

## 🔐 Permissions and host access

The Manifest command has `admin_only: true` and supports private and group chats.
All group members can see group command output.
Child processes inherit the Bot account file, network, and process permissions.
The command enable list reduces accidental execution. A low-privilege operating-system account, container, or dedicated execution service supplies security isolation.

Inbound authentication, the administrator list, terminal configuration, and command enable list control deployment access together.
A dedicated low-privilege Bot account and a minimal `replace` list are recommended for production.

## ⌨️ Commands

```text
/shell <command>
/shell list
/shell help
```

Manifest aliases are `/shell`, `/sh`, and `/exec`.
`list` shows enabled entries and their availability in the selected terminal.

Git Bash examples:

```text
/shell pwd
/shell ls -la
/shell git status --short
/shell python --version
```

An explicit interpreter is necessary for interpreter built-in commands. For example, the Windows direct backend accepts `/shell cmd /c dir`.
Enabling a general interpreter grants the host operations that the interpreter can express.

## ⚙️ Terminal configuration

Terminal options are in `plugins.shell.terminal` in `config/config.json`.

### Git Bash

```json
{
  "plugins": {
    "shell": {
      "terminal": {
        "backend": "git-bash",
        "executable": "C:/Program Files/Git/bin/bash.exe"
      }
    }
  }
}
```

Git Bash starts with `--noprofile --norc -c`. The deployment configuration must contain the actual executable path.
Availability checks examine each entry. Available entries remain visible when another command is missing.

### Direct

```json
{
  "plugins": {
    "shell": {
      "terminal": {
        "backend": "direct"
      }
    }
  }
}
```

The Direct backend resolves the first token to an external program in the Bot process PATH or an explicit path.
It starts that program with `create_subprocess_exec`.

## ⌨️ Command selection and timeout

Sensitive settings are in `config/secrets.json`:

```json
{
  "plugins": {
    "shell": {
      "whitelist": ["ls", "pwd", "git", "python"],
      "whitelist_mode": "replace",
      "timeout": 30,
      "disable_whitelist": false
    }
  }
}
```

| Field | Rule |
| --- | --- |
| `whitelist` | List of command entry strings |
| `whitelist_mode=replace` | The custom list is the complete enabled set |
| `whitelist_mode=extend` | Custom entries extend the project default set |
| `timeout` | Finite positive seconds. Default: 30 |
| `disable_whitelist` | JSON Boolean `true` enables all command entries |

An empty `replace` list disables all command entries. Configuration parsing uses strict types.

## 🔐 Execution limits

- Startup checks reject command chaining, pipes, command substitution, multiline input, append redirection, and predefined high-risk patterns.
- The raw command passes pattern and entry checks before it reaches the selected terminal.
- Standard input uses the null device for one-shot, noninteractive commands.
- Child processes start in a separate process group.
- On Windows, the startup handshake adds the target process to a Job Object before command execution proceeds.
- Cleanup removes descendants owned by the call even when the parent process exits first.
- Timeout, cancellation, and output overflow trigger cleanup of the complete child process tree.
- stdout and stderr share a raw capture limit of at least 64 KiB.
- QQ reply bodies share a 4000-character head-and-tail limit.
- Replies show the return code, stdout, and stderr in separate sections.

Core `core.process_tree` implements process ownership, output limits, and timeout cleanup.
The XiaoQing agent local tool uses the same implementation.
Enabled interpreters and programs can access files, networks, and child processes according to their capabilities.
Administrators must review the enable list together with host permissions.

## 🔄 Path rules

Use `/` in QQ path input:

- Windows: `C:/workspace/a.txt`.
- Linux and macOS: `/home/user/a.txt`, `~/a.txt`, `./file`.
- Path values in `key=value` arguments are normalized.
- URLs keep their original text.
- Windows options `/c` and `/Y` keep their argument meaning.
- Quotes are necessary for paths with spaces.

## 💾 Audit

Sensitive audit records contain request ID, status, return code, command length, and a process-local one-way fingerprint.
Ordinary log filtering excludes raw commands, stdout, stderr, and configuration secrets.

## 🩺 Troubleshooting

| Symptom | Check |
| --- | --- |
| Invalid terminal configuration | Examine `backend` and the Git Bash `executable`. |
| Rejected command entry | Examine `whitelist` and `whitelist_mode`. |
| Program resolution failure | Examine PATH in the selected terminal, or use an explicit program path. |
| Shell built-in message | Call the applicable interpreter explicitly. |
| Command timeout | Adjust `timeout`, or use background service management for long tasks. |
| Output limit reached | Reduce output, or write a controlled result file and query its summary. |

## ✅ Development checks

Run these commands from the repository root:

```bash
python -m ruff check plugins/shell tests/plugins/shell/test_shell_plugin.py
python -m mypy plugins/shell
python -m pytest -q tests/plugins/shell/test_shell_plugin.py \
  tests/plugins/contracts/test_shell_jupyter_log_privacy.py -n 2
```
