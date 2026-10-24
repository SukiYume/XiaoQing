# 📓 Jupyter

[中文](README.md) | English

`jupyter` supplies Python execution, persistent kernels, a REPL code buffer, and kernel management to Bot administrators.
Private and group chats are supported. Each user has a separate kernel owner key in each private or group chat.

## 🔐 Permissions and execution limits

Both Manifest commands have `admin_only: true`. They work in private and group chats. All group members can see group code and output.
Python kernels inherit the Bot account file, network, CPU, and memory permissions.
Trusted administrators and operating-system resource isolation are necessary for production deployment.

Install the optional dependencies:

```bash
pip install "xiaoqing[jupyter]"
```

## ⌨️ Commands

| Function | Primary command | Manifest aliases |
| --- | --- | --- |
| Execute Python or start REPL | `/jupyter` | `/py` `/python` |
| Manage the current user's kernel | `/kernel` | `/内核` |

```text
/py print("hello")
/py -t 60 print("bounded")
/py --timeout=30 print("bounded")
/py repl
/py help
/kernel status
/kernel start
/kernel restart
/kernel shutdown
```

Timeout options precede the code: `-t <seconds>`, `--timeout <seconds>`, and `--timeout=<seconds>`.
The permitted range is 0.1–600 seconds. All subsequent text is literal Python code.

## 💬 REPL session

`/py repl` starts a code-buffer session with a 10-minute idle limit.

| Input | Effect |
| --- | --- |
| `run`, `执行` | Execute the complete buffer. Clear it after success |
| `show`, `显示` | Show a preview within the output limit |
| `clear`, `清空` | Clear the buffer |
| `help`, `帮助` | Show REPL help |
| `退出`, `取消`, `exit`, `quit`, `q` | End the Core Session |

The buffer holds 64 lines, with 3000 characters per line.
Complete code also has limits of 16000 characters and 32 KiB.
Execution errors keep the buffer for correction and another attempt.
If another plugin owns an active Session, the entry point asks the user to end that Session first.

## 📌 Kernel and output limits

| Item | Limit |
| --- | ---: |
| Code per execution | 16000 characters, 32 KiB |
| Execution time | Default 30 seconds. Range 0.1–600 seconds |
| Text output | 64 KiB collection. 2000 characters displayed |
| Image count | 5 |
| One image | 5 MiB, 20 million pixels |
| Total image size | 10 MiB |
| Kernels | 64 globally |
| Kernel idle time | Approximately 5 minutes |
| REPL idle time | 10 minutes |

The kernel becomes available after its ready check.
At an execution timeout or output limit, the manager interrupts code and starts recovery.
PNG results pass byte, format, pixel, and total-size checks before transmission as memory message segments.

`/kernel status` reads kernel state in the thread executor.
When kernel start or shutdown holds the management lock, the status query waits in that thread.
The event loop continues to process other requests.

## 🔐 Audit and privacy

Sensitive audit records contain operation type, status, code length, and a one-way fingerprint.
Ordinary log filtering excludes code bodies, REPL buffers, user input, and exception bodies.
Invalid user identity stops kernel-owner creation at the entry point.

## ⏰ Lifecycle

`init()` examines the availability of `jupyter_client` and `ipykernel`. Kernels start on demand. An idle-monitor task removes unused kernels.
At unload, reload, or Bot shutdown, `shutdown()` stops the monitor first. It then removes active and quarantined kernels concurrently.
REPL Sessions and kernel processes have separate lifecycles.

## 🩺 Troubleshooting

| Symptom | Check |
| --- | --- |
| Missing Jupyter dependencies | Install `xiaoqing[jupyter]`, then reload the plugin. |
| Kernel start failure | Examine Python, `ipykernel`, process permissions, and startup logs. |
| Execution timeout | Adjust `-t`. Examine code loops and external I/O. |
| Output limit reached | Reduce printed output, image count, or image size. |
| Invalid kernel state | Use `/kernel restart` to create a clean kernel. |
| Another REPL Session is active | Send `退出` to end the current Core Session. |

## ✅ Development checks

Run these commands from the repository root:

```bash
python -m pytest -q tests/plugins/jupyter/test_jupyter.py \
  tests/plugins/jupyter/test_jupyter_contracts.py \
  tests/plugins/jupyter/test_jupyter_lifecycle.py \
  tests/plugins/jupyter/test_jupyter_manager_contracts.py \
  tests/plugins/contracts/test_shell_jupyter_log_privacy.py
python -m ruff check plugins/jupyter
python -m mypy plugins/jupyter
```
