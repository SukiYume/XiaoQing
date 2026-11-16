"""xiaoai 插件测试共用的米家 / 小米账号云端地址。

真实边界是小米账号、米家云端与 miot-spec 的 HTTP 接口；测试用 ``aioresponses`` 按这些地址
登记响应，设备列表、动作与规格的行为由各测试文件的云端替身自己决定。
"""

from __future__ import annotations

import re

LOGIN     = re.compile(r"https://account\.xiaomi\.com/pass/serviceLogin\?.*")
QR        = re.compile(r"https://account\.xiaomi\.com/longPolling/loginUrl\?.*")
POLL      = re.compile(r"https://ak\.lp\.account\.xiaomi\.com/lp/s\?.*")
STS       = re.compile(r"https://sts\.api\.io\.mi\.com/sts\?.*")
MINA_STS  = re.compile(r"https://api2\.mina\.mi\.com/sts\?.*")
DEVICES   = re.compile(r"https://api\.io\.mi\.com/app/home/device_list")
PROP_GET  = re.compile(r"https://api\.io\.mi\.com/app/miotspec/prop/get")
PROP_SET  = re.compile(r"https://api\.io\.mi\.com/app/miotspec/prop/set")
ACTION    = re.compile(r"https://api\.io\.mi\.com/app/miotspec/action")
UBUS      = re.compile(r"https://api2\.mina\.mi\.com/remote/ubus")
MINA_LIST = re.compile(r"https://api2\.mina\.mi\.com/admin/v2/device_list\?.*")
SPEC_ALL  = re.compile(r"https://miot-spec\.org/miot-spec-v2/instances\?.*")
SPEC_ONE  = re.compile(r"https://miot-spec\.org/miot-spec-v2/instance\?.*")
