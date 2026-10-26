# XiaoAi cloud bridge

This plugin discovers Xiaomi Home device capabilities and supports administrator private-chat queries and controls through Xiaomi Cloud. The bot and devices can use different networks.

Run in the bot's Python environment from the project root:

```shell
python -m pip install -e ".[xiaoai]"
```

Load the plugin with administrator `/reload`, then send `/小爱 登录` in private chat. The bot sends a QR image directly to the requesting user. Scan it with Xiaomi Home and confirm authorization. The bot then lists speakers; send `/小爱 选择 1` to bind one. QR validity is capped at five minutes; speaker selection has its own five-minute window. `/小爱 状态` shows progress and `/小爱 取消登录` cancels the current flow. Selection and cancellation belong to the user who started login. Repeating login replaces that user's previous flow.

Use `/小爱 登录 de` to select another region (`cn/de/i2/ru/sg/us`). Existing region and silent settings are preserved by default. Login uses the model's `execute-text-directive` action and input order. QR images and pending credentials stay in memory until binding. Timeout, cancellation and plugin shutdown discard pending login state. Existing saved credentials remain available throughout the pending flow.

Every 30 minutes the running bot checks cloud access. Service tokens older than six hours are renewed using the existing `passToken`; command execution also checks token age. Six hours is a renewal policy. Xiaomi controls actual validity. Explicit authorization errors trigger one renewal attempt; read-only queries can retry once. Device actions are submitted once. `/小爱 续期` requests immediate renewal.

If a service rejects access after successful renewal, the plugin preserves login state and requests a later check. Reauthorization alerts follow account-renewal failures, with delivery receipts tracked separately for each expiration event.

When Xiaomi requires fresh authorization, the bot privately alerts the binding user to run `/小爱 登录`. Successful delivery records the notification; failed delivery is retried on a later health check. Network outages preserve credentials and do not create reauthorization alerts. Older terminal setups register the recipient on the first administrator device command or `/小爱 检查`.

Configuration defaults to `data/xiaoai/`: `token.json` stores plaintext credentials, `speaker.json` stores speaker and notification settings, `auth-state.json` stores reauthorization/notification state, and `requests.json` stores duplicate-action protection. Windows setup restricts this directory to the current OS user.

The terminal option remains available: `python -m plugins.xiaoai.setup`. It reads an account, hidden password and verification code locally. Passwords remain in process memory. Options include `--region`, `--audible` and `--data-dir`. Run it with the bot's OS account while the plugin is idle.

The default command discovers account devices and builds a capability catalog from model-specific MIoT specifications. Natural-language planning uses `plugins.xiaoai.ai.routes.plan`; the existing XiaoQing agent can invoke the published command in administrator private chats.

Use `/米家 设备`, `/米家 刷新设备`, and `/米家 能力 <device name>` to inspect capabilities. `/米家 份量 <feeder ID> 10` defines grams per portion; `/米家 天气城市 北京` sets the weather city. Temperature and humidity come from sensor properties. Weather comes from Open-Meteo. `/米家 调用` accepts a JSON object containing an operations array, with device, capability and op fields; set operations add value and action operations add args.

Every operation is validated before execution. Writes are submitted once, readable properties are checked after writes, and mismatches stop subsequent steps. Duplicate message identities are blocked for 24 hours and equivalent operations for 20 seconds. Caches and preferences are account-scoped. Cloud action acceptance leaves physical completion subject to device feedback.

Infrared bulbs expose power commands. Infrared air conditioners expose power, mode, temperature and relative fan changes. Absolute fan speed and swing use the bound speaker's silent semantic forwarding and return an unconfirmed status. Physical infrared effects require observation. `/小爱 执行 <text>` explicitly forwards text to the speaker and attempts fresh-answer retrieval.

See the [Chinese guide](README.md) for command examples and configuration details.

Device inventories and each device’s complete feature list appear in one response with grouped controls, settings and queries. Common enumeration labels have readable Chinese translations; unknown meanings are explicitly marked. Use full or uniquely matching partial device names. A full device name alone also opens its feature list. `/小爱 接口 <device name> [page]` exposes full structured specifications.

## Data and deployment

Account credentials stay in the local, Git-ignored `data/xiaoai/` directory. Natural-language planning sends the current request, device names, rooms, models, online status, derived device identifiers and relevant capabilities to the configured model service. Login tokens remain within Xiaomi authentication and device requests. Weather queries send the city name and resolved coordinates to Open-Meteo. Device, capability and structured-call commands are parsed locally.

Adding the model route changes `config/config.json`. The running configuration watcher can revoke credentials and close the QQ connection after external configuration changes. Arrange a host-side bot restart during deployment so configuration and credentials load together. A disconnected QQ channel requires host-side recovery.

## Feeder units

The Agent uses `xiaoai_devices`, `xiaoai_capabilities` and `xiaoai_execute` to discover devices, inspect schemas and submit structured operations. These tools reuse the command permission checks and execution pipeline through `/米家 数据`, `/米家 接口` and `/米家 调用`, with zero XiaoAI planner calls. The Agent supplies the task's original user text as `original_request` for feeding quantity validation. Direct natural-language messages continue to use the plugin planner.

Both planning paths validate the complete plan and original quantity at the execution boundary before reserving or submitting mutations. Each plan reuses one capability copy per device while preserving stored specifications. Speaker forwarding and device operations share duplicate-protection rules and retain separate request journals.

The `xiaomi.feeder.pi2001` feed action uses whole portions. Public requests specify grams; the execution boundary converts them using the configured estimated grams per device portion. With 10 g per portion, 10 g sends 1 and 20 g sends 2. Requests for 5 g or 15 g are blocked before any device mutation. Natural-language quantities must match the original request; rounding and changed amounts are rejected. Missing calibration blocks feeding. Replies show submitted portions and estimated weight.

The target-feeding setting stays read-only and schedule writes await separate protocol verification. Actual weight depends on the food. See [Xiaomi's explanation](https://www.mi.com/ph/support/faq/details/KA-243397/).

Feature guides start with copyable Chinese requests. Lamp controls are grouped by power, brightness, warm or cool light and modes; equivalent protocol controls share one user-facing function. Full parameters remain available through the interface command.

Current air conditioners, purifiers, cameras, projectors, speakers, feeders, water dispensers, cookers, routers, bands and sensors have examples for everyday tasks, filtered by current capabilities and access permissions. The charger guide explains its exposed reset function. Long cooker diagnostic labels use short names in the feature guide; original descriptions remain in the interface view.
