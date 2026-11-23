# Changelog

中文在后面：[0.6.0-rc.2 中文](#060-rc2中文)、[0.5.2 中文](#052中文)。

## 0.6.0-rc.2 (pre-release)

2026-11-23

A release candidate: other devices can use the models on this Mac. **0.5.2 remains the stable version.** 0.5.x does not offer this build as an update; you only get it by downloading it from its release page.

**What has been tested, and what has not.** Tested on a single Mac, against its real network addresses and official oMLX 0.8.2: opening and closing a network, requests through the local-network address (model list, chat, streaming), the rules for other devices listed below, and Health Check; plus the full automated test suite. **Not tested yet**: a second device actually connecting (including what the macOS firewall does), moving to another Wi-Fi and back, any mesh VPN (Tailscale, Oray PgyVPN and the others), client apps on phones and tablets, the field names under **What to enter in each app**, **Create and Show Chatbox QR Code**, and replacing an installed 0.5.x with this build. If you try any of these, please tell us how it went in Issues.

- **Let devices on your networks connect**: in **Settings → oMLX**, a new section lists the networks this Mac is on (home Wi-Fi or Ethernet, Tailscale, other virtual networks from mesh VPN apps). Tick one and the gateway opens on that network only. Everything is off by default, and oMLX itself still listens on this Mac only.
- **Away from home**: install a mesh VPN (Tailscale, Oray PgyVPN, EasyTier, ZeroTier) on this Mac and on the other device, then tick its network. No port needs to be opened to the internet.
- **A home network is tied to the one you ticked**: on a different Wi-Fi it closes by itself and comes back when you return. A home network is not encrypted; use it only on your own home or office network.
- **Rules for other devices** (apps on this Mac are not affected): private addresses only; 8 wrong keys in a row lock that device out for 30 seconds, then 300; each sub-key runs 2 requests at a time (the rest wait in line) and 60 per minute, 6 at a time across all devices; sub-keys shorter than 32 characters are refused. Other devices can only call models that are already loaded. These numbers may change before 0.6.0.
- **Connect Clients page**: one address row per open network, with **Copy**, **Copy Root Address** and a QR code (address only); **What to enter in each app** for Chatbox, Cherry Studio, Open WebUI, Continue, Cline / Roo Code and dsh; **If another device can't connect**; a **Last Used** column for sub-keys.
- **Keep this Mac awake while a network is open** (off by default; applies on power only).
- **Health Check** now verifies the gateway listens only on this Mac and the ticked networks, and tells you if the macOS firewall is blocking it.
- **Guide**: two new chapters, "Letting other devices use this Mac's models" and "Using it away from home (mesh VPN)".

Settings, keys and models from 0.5.x carry over. Nothing changes until you tick a network. To go back, untick the networks and replace the app with 0.5.2.

## 0.5.2

2026-11-22

Two fixes found while installing 0.5.1 on a real Mac.

- **View on the orange oMLX notice now opens Settings → Updates**, where **Upgrade…** is. It used to open Settings on whichever page was showing last.
- **Choosing where to download oMLX from** is now two plainly labelled options in the install and upgrade window: **GitHub** (oMLX's official release page) and **ModelScope** (an unmodified copy kept by this project's developer, for mainland China). Both are the same official installer and both are checked against the SHA-256 in the compatibility table. With the download source on Auto there is also an **Automatic** option that picks the faster one. The old text called both "the official release address", which was wrong for the mirror.

Nothing else changed; settings and keys from 0.5.1 carry over. To update, download the new zip and replace the app.

## 0.5.1

2026-11-22

The first release of ModelTower: a console for running open-source LLMs on Apple Silicon Macs, built on the official oMLX app. It does not bundle oMLX. **0.5.1 works on a single Mac**: one Mac runs the models for itself and for the apps on that Mac. Released under the Apache License 2.0.

**Getting started**

- **Get Started in three steps**: install oMLX → find the models you already have → pick one and click **Load and self-test**.
- **Three ways to get oMLX**: the official installer from GitHub, the ModelScope mirror for mainland China, or an installer you already have (Homebrew also works). On Auto, the download source is chosen by speed. Every installer is checked against the SHA-256 in the compatibility table before it is used.
- **Models you already have** are listed automatically; **Add an Existing Model Folder…** adds another folder to oMLX, so models already on disk need no new download. oMLX's own download folder stays as it is.
- When there are several models, step 3 offers a menu with each model's size; models that do not fit are marked "· too big".

**Models**

- **Load, unload, switch** from the model card. Before loading, the card says how much memory it needs, roughly how long it takes and whether it fits; if it does not fit alongside another model, the button says **Switch**.
- **Self-test** after loading: the model answers a test question.
- **Switches per model**, shown when oMLX reports the model supports them: **MTP** (multi-token prediction, faster generation, identical output) and **Tables on SSD** (frees memory, identical output; off by default, locked on when oMLX has to keep the tables on the SSD). Each has an ⓘ with benefits, costs and when to use it.
- **Context**: defaults to what you set in oMLX for that model, otherwise 256K (or the model's maximum if smaller).
- **Multi-turn cache**: Auto, a fixed limit, or off; **Clear Cache…** removes it.

**Connecting clients**

- **Connect Clients page** (under **Keys**): the address with **Copy** and **Copy Root Address** (without `/v1`), this Mac's model names, and which key to use. The address works on this Mac only.
- **Sub-keys**: one per app, made with **Create and Copy**; 👁 shows a key for 10 seconds and copies it (the clipboard clears after 30 seconds); **Disable / Enable**; **Delete…**.
- **Gateway**: apps connect through it, and it only passes requests for models that are already loaded. Web-based and Electron clients on this Mac can connect through it too.

**Main window**

- **Title area**: this Mac's name with a **Single Mac** tag, what is running and the client address; **Unload All** and **Refresh** on the right.
- **Memory bar**: Used, Most when full, and the Safety line. Going over the line comes with the reason and the fix, including a one-click button such as **Change to 512K**.
- **Top right**: **Settings / Arrange / Mini Bar / Pin**.
- **Health Check**, **Activity** and **Logs** at the bottom.
- The red close button hides the window and the console keeps running; **Quit the console when closing the window** in **Settings → General** changes that.

**Settings and updates**

- **Settings**: General, Updates, Guide, About and oMLX. The oMLX tab has **Open oMLX Settings** and **Open oMLX Admin**, the main key permission, the Gateway and the model folders.
- **Online check** (on by default, can be turned off): once a day the console looks for one small signed file with the oMLX compatibility table, the latest ModelTower version and notices, in this repository or on the ModelScope mirror. Nothing is uploaded. If it cannot be reached, nothing stops working and the compatibility table built into the app is used.
- **Download source**: Auto, International (GitHub) or China (ModelScope mirror).
- **Compatibility**: official oMLX 0.8.2; minimum 0.8.0.

**Coming next**

- **0.6**: other devices on your local network use this Mac's models; connect back from outside through mesh VPN apps such as Tailscale.
- **1.0**: link several Macs and manage them together. In 0.5.1, **Machines → Add Machine / This Mac's Access** open pages that preview it.

**Known limitations**

- Single Mac only.
- The app is not notarized. The first launch needs **Open Anyway** in **System Settings → Privacy & Security**.
- The app does not ship its own Python. It uses the one that comes with oMLX, or Apple's command line tools.
- New versions of ModelTower are not installed from inside the app: download them from Releases.
- English and Simplified Chinese.

## 0.6.0-rc.2（中文）

2026-11-23，预发布

候选版：别的设备可以用这台 Mac 上的模型。**稳定版仍然是 0.5.2。** 0.5.x 不会提示升级到这一版，要用只能到它的发布页自己下载。

**测过什么，没测什么。** 在一台 Mac 上、用它真的网络地址、对着官方 oMLX 0.8.2 测过：打开和关掉一个网络，经局域网地址发请求（列模型、问答、流式），下面列的"别的设备的规矩"，体检；另外全套自动测试都过了。**还没测的**：真的另一台设备连进来（包括系统防火墙会怎么反应），换到别的 Wi-Fi 再换回来，任何一家组网软件（Tailscale、蒲公英等），手机和平板上的客户端，**各个软件里怎么填** 里每个框的名字，**新建并出 Chatbox 二维码**，以及在装着 0.5.x 的 Mac 上直接换成这一版。你试了其中哪一项，欢迎到 Issues 里说一声结果。

- **让你网络里的设备连进来**：**设置 → oMLX** 多了一节，列出这台现在连着的网络（家里的 Wi-Fi 或网线、Tailscale、组网软件的其他虚拟网络）。勾哪个，入口就只在那个网络上开门。默认都不开，oMLX 自己仍然只听本机。
- **在外面用**：在这台和另一台设备上装同一个组网软件（Tailscale、蒲公英、EasyTier、ZeroTier），再勾它的网络。不用开公网端口。
- **家里的网络只认勾的那一个**：换到别的 Wi-Fi 会自动关上，回来自动恢复。家里的网络不加密，只在自己的家庭、办公室网络里用。
- **别的设备的规矩**（这台自己的软件不受影响）：只收私网地址；密钥连错 8 次，那台设备锁 30 秒，再犯 300 秒；每把子密钥同时 2 个请求（多的排队）、每分钟 60 个，所有设备合计同时 6 个；少于 32 个字符的子密钥不认。别的设备只能调用已经加载的模型。这几个数在 0.6.0 之前可能调。
- **连接客户端页**：每个开着的网络一行地址，带 **复制**、**复制根地址** 和二维码（只有地址）；**各个软件里怎么填**（Chatbox、Cherry Studio、Open WebUI、Continue、Cline / Roo Code、dsh）；**别的设备连不上怎么办**；子密钥多一栏 **最近使用**。
- **开着网络时不让这台睡眠**（默认关；只在接着电源时起作用）。
- **体检** 多查一项：入口只在本机和勾了的网络上听；系统防火墙拦住入口时会告诉你。
- **教程** 多两章："让别的设备用这台的模型""在外面用（组网软件）"。

0.5.x 的设置、密钥、模型都照旧。不勾网络，什么都不变。要退回去：先把勾着的网络都关掉，再把应用换回 0.5.2。

## 0.5.2（中文）

2026-11-22

在真机上装 0.5.1 时发现的两处，修了。

- **橙色 oMLX 提示上的「查看」直接到「设置 → 更新」**，**升级…** 就在那一页。以前只是打开设置窗口，停在上次看的那一页。
- **oMLX 从哪里下载**，在安装和升级窗口里改成明着列出来的两项：**GitHub**（oMLX 官方发布页）、**魔搭**（本项目开发者的原样转存，供中国大陆下载）。两个是同一份官方安装包，下完都按兼容表里的 SHA-256 核对。下载源是「自动」时多一项**自动**，下载时测速挑快的。原来那句把两个来源都叫「官方发布的地址」，对魔搭不准确，改了。

别的没变；0.5.1 的设置和密钥照用。更新办法：下载新的 zip，换掉原来的应用。

## 0.5.1（中文）

2026-11-22

ModelTower 的第一个版本：在 Apple Silicon Mac 上跑开源大模型的中控台，对接官方 oMLX，不捆绑它。**0.5.1 只支持单机**：一台 Mac 自己跑模型，给本机的软件用。以 Apache License 2.0 发布。

**开始使用**

- **「开始使用」三步**：装 oMLX → 认出已有的模型 → 挑一个，点「加载并自检」。
- **三条路装 oMLX**：从 GitHub 下官方安装包、从中国大陆的魔搭转存下、选手头已有的安装包（也能用 Homebrew）。下载源选「自动」时按速度挑。不管哪条路，都先按兼容表里的 SHA-256 核对。
- **已有的模型**自动列出；「添加已有的模型文件夹…」给 oMLX 多加一个模型文件夹，硬盘上已有的模型不用重新下载，oMLX 自己下载模型的地方不变。
- 有几个模型时，第 3 步用下拉框挑，每项写着大小，放不下的标「· 放不下」。

**模型**

- **加载、卸载、切换**都在模型卡片上。加载前写明要占多少内存、大概多久、放不放得下；和别的模型放不下一起跑时，按钮写「切换」。
- **自检**：加载完让模型答一道题。
- **模型开关**，oMLX 说这个模型支持才显示：**MTP**（多 token 预测，生成更快，答案不变）、**表放 SSD**（省内存，答案不变；默认关，oMLX 不得不放 SSD 时锁定为开）。每个开关旁的 ⓘ 写着收益、代价和什么时候开。
- **上下文**：oMLX 里给这个模型设过就用它，没有就 256K（模型上限更小的用上限）。
- **多轮缓存**：自动、设上限或关掉；「清空缓存…」删掉它。

**连接客户端**

- **「连接客户端」页**（点「密钥」）：地址带「复制」和「复制根地址」（不带 `/v1`），这台的模型名，该用哪把密钥。地址只在本机能用。
- **子密钥**：每个软件一把，点「新建并复制」；👁 显示 10 秒并复制（30 秒后清空剪贴板）；「停用 / 启用」；「删除…」。
- **入口**：软件经入口连接，只放行已经加载的模型。这台上的网页版、Electron 做的客户端也能经它连。

**主窗口**

- **标题区**：这台的名字和「单机」标签、在跑什么、客户端地址；右边「卸载全部」「刷新」。
- **内存条**：已用、塞满时最多、安全线。超线时写明原因和改法，有「改成 512K」这样的一键按钮。
- **右上角**：设置 / 排列 / 长条 / 固定。
- 底部有**体检**、**操作记录**和**日志**。
- 红色关闭按钮只收起窗口，控制台继续跑；「设置 → 通用」里的「关闭窗口时退出控制台」能改。

**设置和更新**

- **设置**：通用、更新、教程、关于、oMLX。oMLX 页有「打开 oMLX 设置」「打开 oMLX 管理页」、主密钥的授权、入口和模型文件夹。
- **联网检查**（默认开，可以关）：一天一次，到本仓库或魔搭转存找一份签了名的小文件（oMLX 兼容表、ModelTower 最新版本、通知），不上传任何东西。读不到也不影响使用，就用应用自带的兼容表。
- **下载源**：自动、国际（GitHub）、中国（魔搭转存）。
- **兼容**：官方 oMLX 0.8.2，最低 0.8.0。

**接下来**

- **0.6**：局域网里别的设备用这台的模型；借 Tailscale 这类组网软件，人在外面也能连回来。
- **1.0**：多台 Mac 联机，一起管。0.5.1 里「机器 → 添加机器 / 本机授权」点进去是预告页。

**已知限制**

- 只支持单机。
- 没有公证，第一次打开要到「系统设置 → 隐私与安全性」点「仍要打开」。
- 应用不自带 Python，用 oMLX 带的或苹果命令行工具的。
- ModelTower 的新版不在应用里安装，要到 Releases 下载。
- 界面语言：简体中文、English。
