# Changelog

中文在后面：[0.5.1 中文](#051中文)。

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
