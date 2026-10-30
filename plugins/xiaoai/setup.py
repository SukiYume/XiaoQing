"""本机交互登录与音箱绑定：python -m plugins.xiaoai.setup。"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import time
from pathlib import Path

import aiohttp

from .client import (
    BridgeError,
    directive_spec,
    fetch_spec,
    protect_directory,
    save_binding,
    services,
    speaker_candidates,
)


async def setup(directory: Path, region: str, audible: bool) -> None:
    try:
        from miservice import MiAccount
    except ImportError as exc:
        raise BridgeError('请先运行 python -m pip install -e ".[xiaoai]"') from exc
    # 上游登录异常包含凭据片段；此独立进程仅输出固定诊断。
    logging.disable(logging.CRITICAL)
    username = input("小米账号：").strip()
    password = getpass.getpass("小米密码（隐藏输入）：")
    if not username or not password:
        raise BridgeError("请填写小米账号和密码。")

    async def otp(method: str) -> str:
        return await asyncio.to_thread(getpass.getpass, f"{method} 验证码（隐藏输入）：")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        account = MiAccount(session, username, password, token_store=None, otp_callback=otp)
        for sid in ("xiaomiio", "micoapi"):
            if not await account.login(sid):
                raise BridgeError("小米登录失败，请核对账号、密码及验证码后重试。")
        io, mina = services(session, account.token, region)
        speakers = await mina.device_list()
        devices  = await io.device_list("full")
        # 使用米家 DID 关联 MiNA deviceID；多个音箱由用户明确选择。
        candidates = speaker_candidates(speakers or [], devices)
        if not candidates:
            raise BridgeError("账号下暂未找到可关联的小爱音箱，请检查账号和地区。")
        for index, (_, device) in enumerate(candidates, 1):
            print(f"{index}. {device['name']} ({device['model']})")
        selected = input("选择音箱序号：").strip()
        if not selected.isdecimal() or not 1 <= int(selected) <= len(candidates):
            raise BridgeError("音箱序号无效。")
        speaker, device = candidates[int(selected) - 1]
        directive = directive_spec(await fetch_spec(session, device["model"]))
        profile   = {
            "name"     : device["name"],
            "model"    : device["model"],
            "did"      : str(device["did"]),
            "mina_id"  : speaker["deviceID"],
            "region"   : region,
            "silent"   : not audible,
            "directive": directive,
        }
        protect_directory(directory)
        # 全部检查通过后写入一个账号的令牌和绑定，密码仅存在本进程内存。
        account.token["_refreshed_at"] = {sid: time.time() for sid in ("xiaomiio", "micoapi")}
        save_binding(directory, account.token, profile)
        print(f"已绑定 {device['name']}；执行动作 {directive['siid']}-{directive['aiid']}。")
        print(f"配置目录：{directory}")
        print("初始化完成，仅执行了登录和只读发现。私聊小青发送 /小爱 检查 开始验证。")


def main() -> None:
    parser = argparse.ArgumentParser(description="小爱云端桥接：本机登录、选择音箱和读取型号规格")
    parser.add_argument("--data-dir", type=Path, default=Path.cwd() / "data" / "xiaoai")
    parser.add_argument("--region", choices=("cn", "de", "i2", "ru", "sg", "us"), default="cn")
    parser.add_argument("--audible", action="store_true", help="让音箱语音回应；默认静默")
    args = parser.parse_args()
    try:
        asyncio.run(setup(args.data_dir.resolve(), args.region, args.audible))
    except BridgeError as exc:
        parser.exit(1, f"{exc}\n")
    except (KeyboardInterrupt, EOFError):
        parser.exit(1, "初始化已取消。\n")
    except Exception:
        parser.exit(1, "初始化未完成，请检查网络、账号地区及数据目录权限后重试。\n")


if __name__ == "__main__":
    main()
