# ModelTower FAQ

[简体中文](faq.zh-CN.md) · [Guide](guide.en.md) · [Back to the front page](../README.md)

For ModelTower 0.5.2. When something goes wrong, first click **Health Check** at the bottom of the window. It checks things one by one, and every failing item says why. Below are the questions asked most often beyond that.

## Opening, permissions and updates

### Double-clicking does nothing, or the system says the developer cannot be verified

That is expected. ModelTower is not notarized by Apple, so it needs one manual approval the first time:

1. Close the system's dialog first.
2. Open **System Settings → Privacy & Security**, scroll to **Security**, and click **Open Anyway** next to the line about ModelTower.
3. Enter your Mac password or use Touch ID.

After that it opens normally. You may need to approve it again after updating to a new version.

### The system asks whether to allow access to the Local Network

0.5 only uses this one Mac and does not need it; either answer is fine. You can change it later in **System Settings → Privacy & Security → Local Network**.

### The window is empty after opening, or it says Python cannot be found

If the window shows a page titled "Install oMLX First" instead of the usual main window: ModelTower does not ship its own Python; it uses the one that comes with oMLX. Neither oMLX nor any other Python was found on this Mac. Pick one:

1. **Install oMLX** (you need it anyway). Click "Open the oMLX Download Page", download it, drag oMLX into Applications, then come back to ModelTower and click "Check Again".
2. **Install Apple's command line tools.** Click "Install Apple's Command Line Tools"; macOS shows an installer. When it finishes (a few minutes), click "Check Again".

Either works. If you take the second route, step 1 of Get Started will still walk you through installing oMLX.

### How do I update ModelTower?

When there is a newer ModelTower, **Settings → Updates** says so and gives a **Download Page** link (this repository's [Releases](https://github.com/dan-cube/modeltower/releases)). The app does not download or replace itself:

1. Press ⌘Q to quit ModelTower.
2. Download the new zip from Releases, unzip it, and drag the new ModelTower into Applications, replacing the old one. Settings live in `~/Library/Application Support/ModelTower/`, so replacing the app does not lose them.
3. Open the new version. If the system blocks it again, approve it as described under "the developer cannot be verified" above.

The notice relies on the online check reading the signed update information. If the online check is off or cannot get through for a while, nothing stops working, and you can always look at Releases yourself.

## oMLX

### The console says oMLX is not installed on this Mac, but I installed it

The console connects to two kinds of install only: the oMLX app from the official installer, and oMLX installed with Homebrew.

- Installed some other way (for example from source): the console can see it but cannot start or stop it, and tells you to use its own way of starting and stopping.
- Just installed: wait a few seconds. The console connects by itself; no restart needed.
- You dragged oMLX to the Trash and did not reinstall: the console treats it as not installed. Click **Install oMLX…** to install it again; your earlier settings are picked up.

### The console says "This oMLX is not signed by its developer"

The console checked the oMLX app's signature and found it was not signed by its developer, so it will not connect. Delete it and reinstall from the official installer with **Install oMLX…**.

### Next to the oMLX version it says "version too old" or "not verified yet"

The console carries a compatibility table that records which oMLX versions have been verified; when the online check reads a newer signed table, it switches to that. The words next to the version come from it:

| It says | Meaning | What to do |
|---|---|---|
| verified | This version was fully tested | Nothing |
| works, with known shortcomings | Usable; a note says what is missing | Read the note and decide whether to upgrade |
| being verified | Under test, no problems found so far | Fine to use |
| newer than the verified version, not verified yet, usually works | Your oMLX is newer than what the console was tested with | Fine to use; if something odd happens, suspect this first |
| not in the compatibility table, not verified yet | The console does not know this version | Same as above |
| version too old | Below the minimum (0.5 needs oMLX 0.8.0 or later) | Go to **Settings → Updates** and click **Upgrade…** |
| this version has known problems | This version has been flagged | Follow the note and change versions |

If a test build of oMLX is installed (0.8.0rc1, for example), **Settings → Updates** says "Test build, please upgrade to the release": the release is already out, so upgrade to the latest release.

### oMLX released a new version, but the console has not suggested it yet

The console verifies a new version before suggesting it, so it lags oMLX's release a little. Once it is verified, a new signed compatibility table is published, and when the console's online check reads it, it suggests "Can upgrade to ××".

If you do not want to wait, you can upgrade yourself; the version will then say "newer than the verified version, not verified yet, usually works".

### Why does the console need to read oMLX's main key? Is that safe?

oMLX's management features (listing, loading and unloading models, managing sub-keys, logs and the cache) only accept the main key. For the console to do these things for you, it has to use it.

- The console **only remembers whether you allowed it**. It never stores the key, and reads it fresh from oMLX's own settings each time.
- It still works if you do not allow it: you can see the status, but not which models this Mac has, and you cannot load or unload.
- You can change it at any time: **Settings → oMLX**, click **Allow…** or **Disallow**.
- The main key is never given to clients or sent anywhere else.

### What is the Gateway, and can I turn it off?

The Gateway is a door between clients and oMLX. It adds about 1 ms to each request.

Official oMLX's sub-keys have one catch: when a client asks for a model that is not loaded, oMLX loads it straight away. A few clients each asking for a different model can fill up memory. The Gateway prevents this: it lets through only models that are already loaded, and keeps clients away from loading, unloading and the admin page. Disabling sub-keys is also done by the Gateway.

You can turn it off in **Settings → oMLX**. Clients then have to connect to oMLX's own port, models that are not loaded can be started by a client request, and disabled sub-keys work again.

### The Gateway is not running and says the port is in use by another program

The Gateway uses port 8900 by default, and oMLX uses 8000. If another program holds either one, it cannot start.

- The Gateway row in **Settings → oMLX** says which program holds the port. Quit that program, then click **Start Now**.
- Click **Health Check** at the bottom; port problems get their own item.

## Downloads

### The oMLX installer downloads slowly, or not at all

- In mainland China: under **Settings → Updates → Download Source**, choose **China** (the ModelScope mirror only) or **Auto** (tests both and uses the faster one).
- Still no luck: click **Open release page** in the install window and download it in your browser (GitHub or the ModelScope mirror both work), then click **I Already Downloaded It → Choose Installer…**.
- An interrupted download: click **Download** again and it resumes instead of starting over.

### After downloading the oMLX installer, it says the file does not match

The console has already deleted the file: its SHA-256 did not match the one in the compatibility table, so it was either damaged in transit or altered. Try another download source (the **Source** in the install window).

If you got the file elsewhere yourself: click **I Already Downloaded It → Choose Installer…**. The console verifies it and tells you which version the file is and whether it can be used.

### Is the ModelScope mirror official? Is it safe?

It is not official. It is an unmodified copy this project keeps on ModelScope, and it is not affiliated with the oMLX project. Address: <https://www.modelscope.cn/models/skyeyes1999/omlx-release-copy>

- It holds oMLX's official installers, not a single byte changed, plus oMLX's LICENSE and a `SHA256SUMS` file.
- Wherever the console downloads from, it checks the SHA-256 in the compatibility table and refuses to install if a single byte differs. A second source therefore adds no extra risk.

See [section 8 of the guide](guide.en.md#downloads-for-china-the-modelscope-mirror).

### Model downloads are slow

Models are downloaded on oMLX's admin page, and oMLX decides where they come from.

- In mainland China: choose ModelScope on oMLX's model download page. It is built into official oMLX and is usually fastest.
- Set the download source to **China** in **Settings → Updates**, and Get Started reminds you of this step.
- For models ModelScope does not have: on the same page, tick **Also switch oMLX's Hugging Face address to the community mirror**. Note that it is a community mirror run by an individual, with no official guarantee; you can untick it when the download is done.

## Models

### My models are in another folder and the console does not see them

Click **Add an Existing Model Folder…** in step 2 of Get Started (it is also in the **Model Folders** section of **Settings → oMLX**), and pick the folder in Finder.

- It only adds one more model folder to oMLX: where oMLX downloads models stays the same, and neither the console nor oMLX writes anything into the folder you picked.
- If a model is online, you are asked first: changing oMLX's model folders unloads any loaded models.
- The models must be in a format oMLX understands (MLX format).

### A model will not load, or the button says Switch

- It says **Switch**: this Mac does not have the memory to run both models together. Clicking it unloads the other one first, then loads this one.
- Loading failed: click **Activity** to see why. The most common reason is not enough memory. Pick a model within the size suggested in step 2 of Get Started, and skip the ones marked "· too big" in step 3's menu.
- Stuck on **Loading…** for a long time: when "May be stuck" appears at the top, click **Check Again Now**; if that does not help, restart this Mac's oMLX (**Restart** on the machine card).

### "Over the Memory Limit" stays at the top

When conversations fill the context, the whole Mac may go over the safety line (90% of total memory). The warning gives the reason and the fix. Any one of these helps:

- lower the model's context: the warning has a button such as **Change to 512K** that does it in one click;
- turn on **Tables on SSD** on the model card (for models that have this switch);
- unload a model;
- quit other programs that use a lot of memory.

### The GPU memory limit line on the oMLX admin page is red

You can ignore it. The console's safety line is 90% of total memory, which is already below the system's default GPU memory limit. Following the console's warnings keeps you clear of it.

### Why is the default context 256K?

If you set a context for this model in oMLX, the console uses that. Otherwise the default is 256K, or the model's own maximum if that is smaller. The console does not start at the model's maximum because a longer context uses more memory when it fills up. If you want more, change it on the model card.

## Clients

### A client cannot connect, or says the model does not exist

Check in this order:

1. Does the model card say **Online**? The Gateway only lets through loaded models; any model greyed out as "available once loaded" on the Connect Clients page has to be loaded first.
2. Is the address the one on the **Keys → Connect Clients** page? Most clients want the address with `/v1` (click **Copy**); some, such as Chatbox and Cherry Studio, want it without `/v1` (click **Copy Root Address**).
3. Is the model name one of those listed on that page?
4. Is the key a sub-key, and has it been disabled or deleted? (Check on the same page.)
5. Is the client running on this Mac? The address only works on this Mac.

### Do web-based or Electron clients work?

Yes. The Gateway answers the browser's preflight requests, so web-based clients opened in a browser on this Mac and desktop clients built with Electron can connect; the actual requests still need a sub-key.

### Can other computers or phones use this Mac's models?

That comes in 0.6. In 0.5 the client address only works on this Mac. For when you are away from home, 0.6 will support connecting back through mesh VPN apps such as Tailscale, PgyVPN or EasyTier.

### Can I link several Macs together?

Linking Macs comes in 1.0: several Macs managed together in one window, each running a different model; pairing checks a fingerprint and a check code without handling the other Mac's password; access can be revoked at any time, and you choose how much access to give. In 0.5, **Add Machine** and **This Mac's Access** open preview pages.

## Uninstalling

### How do I uninstall completely?

The basic steps are in [section 10 of the guide](guide.en.md#10-uninstalling). Here is everything ModelTower leaves outside the app itself:

| Where | What it is | How to remove it |
|---|---|---|
| `~/Library/Application Support/ModelTower/` | The console's settings, downloaded oMLX installers, logs and the Gateway's settings | Delete the whole folder |
| `~/Library/Preferences/io.github.dan-cube.modeltower.plist` | App preferences (such as whether the window is pinned) | Delete the file |
| **System Settings → General → Login Items** | Only there if you turned on opening the console when you log in | Turn it off in **Settings → General** before uninstalling; if ModelTower is already gone, remove it from Login Items |
| oMLX's settings | What you asked the console to write there: sub-keys, each model's context and switches, the cache limit, the model folder you added; plus oMLX's auto-update turned off and the Hugging Face community mirror, if you did either | Before uninstalling, click **Restore** (auto-update) and **Restore the Hugging Face Address** in the console; delete or change sub-keys, contexts and the model folder on oMLX's admin page. If ModelTower is already gone, change all of them on oMLX's admin page |

ModelTower installs no background service of its own. oMLX, its settings and its model files do not belong to ModelTower; removing ModelTower does not touch them.

## Still stuck

**Settings → About** has this project's GitHub address. Open an issue in the repository and describe what you clicked, what you saw, and what **Health Check** and **Activity** say. Remove any keys before pasting.
