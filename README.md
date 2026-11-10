---
title: Auto Rename Bot
emoji: 🏷
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Auto Rename Bot

A personal Telegram auto-rename bot for you and your channel admins. Python, **TDLib** (Telegram's own client library, used through
`pytdbot`), ffmpeg for metadata and track clean-up. Built to run in a free Hugging Face Docker Space.

Send a file → it is downloaded, renamed, optionally tagged / trimmed, and sent back (or to your channel).
**The file extension is always preserved.**

## Features

| Area | What you get |
|---|---|
| Renaming | Template naming `{title} {season} {episode} {1episode} {2episode} {3episode} {quality} {year} {codec} {filename} {ext}` (the numbered episodes add zeros in front, see *Naming templates*), a filename parser (anime / movie / series styles), words to strip, illegal characters cleaned |
| Saved formats | **12 auto formats and 12 manual formats** (`/format`). Each has its own template, **caption**, thumbnail, Title / Author / Artist / Audio / Subtitle / Video titles and Keep Audio / Keep Subtitle lists. The **auto format** in use (chosen in the `/start` menu) renames every file you send; with a **manual format** you use `/rename` → pick one → send one file. A format can be **copied** to the other kind and **shared** with another user of the bot (see below) |
| Rename mode | **Filename** or **Caption**: read the details from the file name or from the message caption (`/mode`) |
| File type | **Same as original / Document / Video / Audio** (`/filetype`). The extension never changes |
| Caption & thumbnail | Caption template: **format it in Telegram like any message** (bold, italic, links, …) and that formatting is kept; the default is the new name in **bold**. Custom thumbnail, automatic video-frame thumbnail otherwise. Every format has its own, and they are edited only on the format's screen in `/format` (the `/start` menu has no Template, Caption or Thumbnail buttons, and a photo you send is never made a thumbnail by itself) |
| Metadata and tracks | **Default metadata** (`/metadata` panel): titles to write into the files, and **Keep Audio / Keep Subtitle**, which keeps only the listed tracks, in your order (stream copy, nothing is re-encoded). A format copies it with one tap: **Apply default metadata** |
| Database Channel | Set `DB_CHANNEL` and `COMMUNICATION_CHAT` and every episode you rename with `/rename` can go on to your **Database Channel** (Yes / No): copied inside Telegram, nothing is downloaded or uploaded again. A manual format's **Channel** option then makes the **channel post bot** publish it there (see *Uploading to the Database Channel*) |
| Episode order | `/sequence` → send files in any order → `/sort`: they come back in episode order, with the **missing** and **repeated** episodes listed |
| Inspect | `/check` (what the bot reads from a name and caption), `/tracks`, `/mediainfo` (everything MediaInfo can read, on a **Telegraph page**), `/dc` (the data center a file is stored on) |
| Pictures | `/ethumb` (the thumbnail of a file or video), `/size` (the dimensions of a picture) and `/resize 720 1080` (the picture at exactly that size): reply to the picture |
| Speed test | `/speed`: an Ookla Speedtest from the machine the bot runs on |
| Start banner | Owner command `/smedia`: pick an image or a GIF for `/start` right in the chat. No environment variable |
| Queue | Files are processed **in the order you sent them** (even a forwarded batch), `/queue`, `/cancel`, live progress with speed / ETA and a Cancel button, FloodWait handling |
| Access | Owner + admins only (`/addadmin`, `/deladmin`); everyone else is ignored (and reported to the log channel) |
| Extras | Send results to a channel (`/setchannel`), error log chat (`LOG_CHANNEL`: every error with its full traceback), `/status`, `/stats`, health endpoint for cron pings |
| Speed | **TDLib** (a native library: it splits a file into parts and moves several at once, there is nothing to set) and `uvloop` for the bot's own code. `/status` shows what is active and how many downloads and uploads went through (see *Is it really fast?* below). **Experimental, off by default:** `STREAM_RENAME=1` uploads a big file while it is still downloading (see *Streaming renames*) |

## Deploy on Hugging Face (free CPU Docker Space)

1. Create a Space: **SDK = Docker (Blank)**, hardware **CPU basic**.
2. Upload every file of this project (keep `README.md`, `Dockerfile`, `requirements.txt`, the `bot/` folder).
3. **Settings → Variables and secrets → New secret**, add at least:

   | Secret | Value |
   |---|---|
   | `API_ID`, `API_HASH` | from https://my.telegram.org |
   | `BOT_TOKEN` | from @BotFather |
   | `OWNER_ID` | your numeric Telegram id |
   | `DATABASE_URL` | free Postgres URL (Neon / Supabase). **Strongly recommended**: the Space disk is wiped on every restart, and the settings, formats, thumbnails, `/start` media and Telegraph login live in this database |

4. Wait for the build, then open **Logs**. You should see `@yourbot is online (TDLib 1.8.65 · pytdbot 0.10.1)`, with `, DC n` added once the bot knows its data center. The first start logs in with the bot token and saves TDLib's state in the database, so later starts need no new login.
5. Keep it awake with your cron pinger: `GET https://<user>-<space>.hf.space/health` every few minutes
   (a private Space needs the header `Authorization: Bearer <your HF token>`).
6. Open the bot and send `/start`. As the owner, send `/smedia` to put an image or a GIF on it.

All settings (see `.env.example`): `ADMIN_IDS`, `LOG_CHANNEL`, `MAX_CONCURRENT_JOBS` (1), `MAX_FILE_MB` (2000), `PROGRESS_INTERVAL` (4),
`BOT_DC` (the bot's data center, if you know it), `TDLIB_DIR` (where TDLib keeps its files, `/tmp/tdlib`), `TDLIB_LOG_LEVEL` (1),
`STREAM_RENAME` (0, experimental) and, for the Database Channel, `DB_CHANNEL`, `COMMUNICATION_CHAT` and `CHANNEL_POST_BOT_ID` (8950016484).
The old `START_IMAGE` setting is gone: use `/smedia`. `SESSION_STRING`, `TRANSFER_CONNECTIONS` and `FAST_TRANSFER` belonged to the Telethon
version and do nothing now (the log says so when one of them is still set).

**Which DC?** Telegram decides a bot's data center when it is created. For a Space in Virginia (US East), a bot on **DC1 (Miami)** is the closest. The bot learns its own DC from the first file it sends (or from `BOT_DC`, if you set it), and `/status` shows it. A file is stored on one DC, and a download connects to that one: the progress message of a rename job shows it beside the stage (`⬇️ Downloading · DC 4`; a streaming rename shows where it goes too, `⬇️⬆️ Streaming · DC 4 → DC 1`, the DC the file comes from and the bot's own), and `/dc` tells it for any file you reply to. A bot's uploads go to its own DC, and TDLib opens twice as many upload connections to DC1, DC3 and DC5 as to DC2 and DC4, so DC1 is also the best DC for upload speed. A bot on the same DC as a file does **not** download it faster: every download goes to the DC where the file is stored, whichever DC the bot is on.

**Is it really fast?** The network and TDLib set the transfer speed; the bot reports what it can see:

* The start-up notice in the log chat (and the Space log) ends with a line like `⚡ TDLib ✅ · uvloop ✅ · no streaming ✅`. A `⚠️` marks the one that is off: `no uvloop` (the bot's own code then runs on the slower standard event loop; TDLib itself is not affected). `streaming ✅` and `no streaming ✅` only say how `STREAM_RENAME` is set, neither is a problem.
* `/status` has a **Speed** section with the same checks plus, since the bot started, how many downloads and uploads there were (and how many renames were streamed).
* When a file is done, the message shows how fast each way went (`⬇ 41.20 MB/s · ⬆ 35.00 MB/s`; transfers under a second show nothing).
* TDLib decides how a file is moved, and there is no setting for it. For a bot (an account without Premium) it uses **2 download connections** per DC with at most 2 MB in flight, and **8 upload connections** with at most 4 MB in flight (4 connections when the bot's own DC is DC2 or DC4). So a download is slower the farther the file's DC is from the Space (the speed is about 2 MB divided by the round-trip time to that DC), and an upload is faster than a download. Only the server can raise these numbers (they are higher for Premium accounts), so the speed you see in the Done message is what this setup gets.

**Streaming renames (experimental, off by default).** Normally a file is downloaded to the disk, then uploaded, so the two times add up. With `STREAM_RENAME=1`, a **big file (over 10 MB) that only needs a new name and caption** is instead uploaded while it is still being downloaded: TDLib is told to upload a file that the bot *generates*, and the bot hands it the start of the original as it arrives. The upload follows the download, so the two take about as long as the slower one. The progress message says `Streaming` and the Done message shows one speed: `⬇⬆ 38.20 MB/s streamed`. `/status` counts them (`Streamed renames`).

It is switched off because this way of uploading has only been tried against a simulated TDLib, never against Telegram itself: turn it on, rename a few big files and watch the log chat before you rely on it. It also keeps **two copies** of the file on the disk while it runs (TDLib's download and the file being uploaded from), so it is tried only when there is room for them (about 2.2 times the file size, plus 64 MB, free in `TDLIB_DIR`).

What streaming does **not** do, because it never has the file to look at (these go the usual way, with no error and no warning):

* a rename with **metadata or Keep Audio / Keep Subtitle** (ffmpeg has to change the file),
* **small files** (10 MB or less: nothing to gain), and a file that comes while **the disk is too full** for the two copies,
* a file whose **video / audio details Telegram does not have** (for example an `.mkv` that was sent as a file while the file type is set to video) and a caption that uses `{duration}` when Telegram does not know the duration,
* a video with **no thumbnail to reuse**: streaming sends your own thumbnail (or the format's), else the preview Telegram already has for the original file. The usual way cuts a frame out of the file instead, so a video that has neither is not streamed.

If a stream fails (a connection drops for good, Telegram refuses the parts), the log chat gets the error with its traceback, the Done message says `Streaming didn't work for this file`, and the file is saved and uploaded the usual way, so nothing is lost. `/status` counts those too.

## Commands

| Command | Description |
|---|---|
| `/start` `/help` | Main menu / how to use |
| `/format` `/formats` | Choose **Auto rename** or **Manual rename**, then create, view, edit, copy, share and delete the formats of that kind (12 of each) |
| `/rename` | Pick a **manual** format, then send **one** file to rename with it. Send `/rename` again for the next file. (The auto format is chosen in the `/start` menu) |
| `/sequence` `/sort` | Collect files, then get them back in episode order (see below) |
| `/mode [filename\|caption]` | Where the details are read from |
| `/removewords a, b` | Words/tags to strip from names (`off` clears) |
| `/filetype [same\|document\|video\|audio]` | How files are sent back |
| `/metadata [on\|off]` | Default metadata panel |
| `/settitle` `/setauthor` `/setartist` `/setaudio` `/setsubtitle` `/setvideo` | Default metadata fields (`off` clears). `{lang}` in audio/subtitle titles becomes each track's language |
| `/keepaudio …` `/keepsubtitle …` | Keep (and order) tracks, see below |
| `/tracks` | Reply to a file to list its audio / subtitle track numbers |
| `/check` | Reply to a file to see what the bot reads from its name and caption, and the new name that gives |
| `/mediainfo` | Reply to a file (or send a file with `/mediainfo` as its caption): all its details on a Telegraph page |
| `/dc` | Reply to a file or picture: the data center (DC) it is stored on, and the one the bot is connected to |
| `/ethumb` | Send a file or video (or reply to one with the command): get its thumbnail |
| `/size` | Reply to a picture: how many pixels wide and high it is |
| `/resize <width> <height>` | Reply to a picture, like `/resize 720 1080`: it comes back at exactly that size |
| `/setchannel -100…` | Send renamed files to a channel (add the bot as admin first). `off` returns to the chat |
| `/queue` `/cancel [id\|all]` | Manage jobs. `/cancel` also stops a waiting `/rename`, `/sequence` and the like |
| `/speed` | Ookla Speedtest of the bot's own connection |
| `/status` | Basic bot uptime, ping and version information |
| `/stats` | Users, files renamed, queue, CPU, RAM, disk and network usage |
| `/addadmin id` `/deladmin id` `/admins` `/smedia` | Owner only |

Every menu setting can also be changed with the buttons: tap a field, then send the value. *Back / Home / Close* edit the same message.
The template, caption and thumbnail have no commands and no buttons in the `/start` menu: they are edited on the screen of each format in
`/format` (**Formats** in the `/start` menu). The **Rename** screen only shows the template of your auto format.

### Captions keep the formatting you give them

Write a caption the way you write any Telegram message: select some words and make them **bold**, *italic*, underlined, struck through,
monospace, a quote or a **link**, then send it (the **Caption** button on a format's screen). The bot keeps
exactly that formatting on those words, no HTML needed. For example, with *Watch* in bold, *episode {episode}* in italic and *our channel*
linked to your channel:

> **Watch** {title} *episode {episode}* on [our channel](https://t.me/yourchannel)

Every file then gets that caption with the variables filled in, and the formatting stays on the same words.

* A `{variable}` is always formatted as a whole. Double-tapping `filename` inside `{filename}` selects the word without the braces; the
  formatting still covers the whole variable.
* Besides the template variables, a caption can use `{original} {size} {duration}` (`{1episode}` … `{3episode}` work like in templates).
* A format's screen shows the caption with its formatting, so you can see what was kept.
* HTML typed by hand still works when you don't use Telegram's formatting (`<b>{filename}</b> {size}`). If you do format words, the
  whole message is taken as you see it, so tags typed in the same message are just text.
* Spoilers and custom emoji are not kept: those words come through as plain text. Links, @mentions and #hashtags typed as text
  are found by Telegram itself when the caption is sent.
* The 800-character limit counts what the formatting adds (a link's address counts too).

### Auto formats, manual formats and `/rename`

`/format` (also `/formats`, or **Formats** in the `/start` menu) offers two choices:

* **🤖 Auto rename**: your 12 auto formats. The one in use renames **every file you send me**.
* **✋ Manual rename**: your 12 manual formats. You pick one with `/rename` and it renames **one file**.

Tap a choice to see, create, edit, copy, share and delete the formats of that kind. They are two separate lists: each has its own limit
of 12, and a name only has to be free within its own kind.

A format holds a **template**, a **caption**, a **thumbnail**, the **Title / Author / Artist / Audio / Subtitle / Video** metadata titles
and the **Keep Audio / Keep Subtitle** lists. Everything else (rename mode, file type, remove words, target channel) still comes from your
normal settings.

**Choosing the auto format.** In the `/start` menu tap **Auto Format**, then the format to use from now on (the one in use is ticked;
**Edit the one in use** opens it). The first auto format you create is used straight away. If you delete the one in use, the oldest left
takes over; with none, the built-in template `[S{season}E{episode}] {title} [{quality}]` and the bold file name as caption are used.
`/rename` cannot choose an auto format.

**`/rename`** lists your manual formats. Pick one and send a file: it is renamed with that format. **One file per `/rename`**: send
`/rename` again and pick a format for the next one. Files you send without `/rename` go through your auto format.

**Template, caption and thumbnail are edited here and nowhere else:** open the format and tap **Template**, **Caption** or **Thumbnail**
(for the thumbnail, then send the photo). The `/start` menu only chooses which auto format is in use, and its **Rename** screen shows that
format's template. A photo you send on its own is ignored. With no auto format yet, create one (**Auto rename → ➕ New format**).

**Copy between kinds.** On a format's screen tap **📋 Copy to manual formats** (or **Copy to auto formats**): you get an independent copy,
thumbnail included, in the other list. A name that is taken there gets `(2)` after it, and that list's limit of 12 applies.

A format's **caption** is its own: tap **Caption** and send a template (the same variables and formatting as the caption of your auto format, up to 800 characters).
A format that has no caption of its own sends the **new file name in bold** (`<b>{filename}</b>`), so one format can say
`<b>{filename}</b> · {size}` while another stays plain. **↩ Reset Caption** (or sending `reset`) goes back to the bold file name.

#### Uploading to the Database Channel

Off until **both** `DB_CHANNEL` and `COMMUNICATION_CHAT` are set (the numeric ids, like `-1001234567890`; the names may be written in any
capitals, so `DB_Channel` and `Communication_chat` work too). Then, after `/rename` has sent you the renamed episode, the bot asks:

> 📤 **Upload this episode to the Database Channel?** (episode number and channel shown) **[✅ Yes] [❌ No]**

* **Yes** → the message becomes **Processing…** and the episode is **copied into the Database Channel on Telegram's side** (forwarded
  without the "Forwarded from" line, so nothing is downloaded and nothing is uploaded again). If Telegram refuses that, the same file is
  sent again by its id, which is just as quick. If the upload fails, the message says why and has **🔁 Try again**.
* Then the bot writes this in the **communication chat**, and the **channel post bot** (which is in that chat too) answers it there:

  ```
  File uploaded to the Database Channel
  Episode: 05
  Channel: -1001234567890
  File: [S01E05] Tokyo Revengers [1080p].mkv
  Post: 4821
  Ref: a1b2c3
  ```

  `Episode` is the episode number of the **renamed** file, `Channel` is the **Channel option of the format** that renamed it, `Post` is the
  message of the copy in the Database Channel and `Ref` lets the answers find their way back.
* The channel post bot replies *message received*, waits for the **🔁 Share URL** button to appear on the post in the Database Channel
  (at least 10 seconds), publishes the post in the channel from the notice and replies *post published successfully*, or *publication
  failed* with the reason. This bot reads those replies and **edits the Processing message** with the result: the channel and the link of the
  new post, or the reason it failed.
* If the channel post bot does not answer at all (it is down, or bot-to-bot messages are not enabled), the Processing message says
  **the channel post bot is down or not responding** after 60 seconds. If it answered *received* but never said how it went (15 minutes),
  the message says so too. In both cases the episode is already in the Database Channel.

**No → only the Share URL.** Answering **No** to the upload question does not end it: the bot asks *Upload it to the Database Channel and get the
Share URL?* (**Yes** / **No**). **Yes** copies the episode into the Database Channel, waits for the file store's **🔁 Share URL** button on the
copy (about 15 seconds) and shows the file link in monospace, so you can tap it to copy. If this bot cannot see the button (another bot's
keyboards are not always shown to a bot), it asks the **channel post bot** in the communication chat (`Get the share URL`, with the post id): its
userbot reads the button and replies with the link. **Nothing is published** - the Channel of the format is not used, and the channel post bot makes
no post. If no link can be found the message says why and has **🔁 Look for the URL again** (it does not upload again).

**🗑 Delete post.** The finished *Post published successfully* message has a **🗑 Delete post** button. It first asks, in a message of its own,
*Are you sure you want to delete this post?* (**Yes** / **No**; No just closes the question). On **Yes** the bot writes this in the communication chat:

```
Delete the published post
Channel: -1001234567890
Message: 57
Ref: d4e5f6
```

The channel post bot deletes the post in its channel and from **My posts**, and answers *post deleted successfully* or *deletion failed* with the
reason. **Only after that answer** does this bot delete the episode's copy in the Database Channel (it needs the right to delete messages there),
and the button is removed from the published message (its text stays). If anything fails - the channel post bot does not answer, refuses, or the
copy cannot be deleted - the question message says why and offers **🔁 Try again**; the copy in the Database Channel is never deleted before
the post is gone, and asking again is harmless (a post that is already gone counts as deleted). The buttons carry everything needed, so a
Delete post button keeps working after a restart. The channel post bot only deletes posts it published by itself.

**The Channel option.** A **manual** format has an optional **Channel**: open it in `/format`, tap **Channel** and send the channel id
(`-1001234567890`; a bare `1234567890` or a `t.me/c/…` link works too), **🗑 Clear Channel** or `off` removes it. It is shown only while
uploading is set up, a format that is copied to another user (or to the auto formats) does not carry it, and a format with no Channel
is **only uploaded**: no notice is written, nothing is published. Auto renames are never offered for upload.

**Set-up.**

1. Add this bot to the Database Channel as an **admin who can post**, and to the communication chat (a group, not a channel).
2. In @BotFather → each of the two bots → **Bot Settings → Bot-to-Bot Communication Mode → Turn on**. Without it Telegram does not
   reliably deliver one bot's messages to the other, and the Processing message may end with *the channel post bot is down*.
3. `CHANNEL_POST_BOT_ID` is the channel post bot's id (the default is `8950016484`); only its answers in the communication chat are read.
4. Set the same `DB_CHANNEL` and `COMMUNICATION_CHAT` in the channel post bot (see its README).

Notes: the owner and the admins all get the offer. A restart while a Processing message is waiting leaves it as it was (the post, if the
channel post bot publishes it, still appears). Two episodes can be in flight at once: every notice has its own `Ref`.

#### Default metadata

What you set in `/metadata` (the six titles, **Keep Audio / Keep Subtitle** and the On/Off switch) is your **default metadata**. Its
commands (`/metadata`, `/settitle` …, `/keepaudio`, `/keepsubtitle`) work as before, and:

* **🏷 Apply default metadata** is on the screen of every format, auto or manual, new or old. One tap copies all of your default metadata
  into it, replacing what the format had. It is a copy: changing the default metadata later does not change the format. If your default
  metadata is empty the bot says so and changes nothing.
* An **auto format with no metadata of its own** uses your default metadata while its switch is **On**, as the metadata of auto renames
  always did. Once the format has any metadata of its own, that is what gets written.
* A **manual format** writes only its own metadata (a format with none leaves the file alone). It never uses the default metadata unless you apply it.

#### Sharing a format

Open a format and tap **📤 Share format**: Telegram lets you pick a chat, and the bot puts a card for the format there (a message
"via @yourbot" with a **👁 View format** button). Whoever taps it opens the bot and sees what the format holds (template, caption, thumbnail,
titles, Keep lists) with a **➕ Add to my formats** button: that puts their own copy, thumbnail included, in their own formats of the **same kind**
(an auto format is added as an auto format, a manual one as a manual format). A name they already use gets `(2)` after it, and the
limit of 12 of that kind still applies.

* **Turn on inline mode once:** @BotFather → `/mybots` → your bot → **Bot Settings → Inline Mode → Turn on**. Until then the Share button
  says so instead of opening the chat picker (the bot notices on its own within a minute, or when you tap the button again).
* The page only opens for people the bot serves (the owner and the allowed users). Anyone else is ignored like always, and you get the usual
  "Ignored a message from …" notice with the `/addadmin` hint in the log channel. Add a friend with `/addadmin` first.
* The link shows the format **as it is when opened**, so later edits are included. Deleting the format makes its link stop working;
  copies people already added stay theirs.

### `/sequence` and `/sort`

```
/sequence          start collecting; send the files in any order (up to 1000)
/sort              they come back in ascending episode order
/cancel            throw the collected files away
```

After the last file the bot lists the **missing episodes** (per season, the gaps between the lowest and highest episode found,
e.g. `E04, E07–E09`) and the **episode numbers that appear more than once**. Files with no readable episode number are sent at the
end, in the order you sent them, and listed too. The episode comes from the file name, or from the caption when your `/mode` is Caption.
`/cancel` while it is sending stops the sort.

The first 200 files go out with only a short pause between them; after that the bot sends roughly one a second, which is about what
Telegram allows in a single chat without making the bot wait. A full batch of 1000 therefore takes roughly 15–20 minutes. The bot
looks the files up 100 at a time while it sends, so the first one arrives straight away.

### `/check`

Reply to a file with `/check`. The bot shows, for the file name and for the caption, the Title, Season, Episode and Quality it
found (`None` when nothing was found; Year and Codec only when there is one), the current rename mode, and the new name your
template would give.

### `/mediainfo`

Reply to a video, audio file or any document with `/mediainfo` (or send the file with `/mediainfo` as its caption). The bot
reads it with **MediaInfo** (General, Video, Audio, Text, Menu/chapters …, falling back to ffprobe if MediaInfo is not
available), adds what Telegram itself knows (name, size, MIME type, how it was sent, date, data center, caption), puts it all on a
**Telegraph** page and sends you the link with an *Open* button.

* Only the first 12 MB are downloaded. If a video keeps its index at the end of the file (many mp4s) and nothing can be read from the
  start, the bot tells you it needs the whole file and downloads it (up to 700 MB). The file size shown is always the real one; a bit
  rate that cannot be worked out from a partial read is left out instead of guessed.
* A very long report is shortened step by step to fit Telegraph's size limit.
* If Telegraph cannot be reached, the same report is sent as a `.txt` file instead.
* Telegraph pages are **public to anyone who has the link**. The page shows the file name, so share the link with care.
* The bot keeps one Telegraph account for its pages (stored in the database); the pages show the bot as their author.

### `/dc`

Reply to a file, video or picture with `/dc` and the bot tells which Telegram **data center** it is stored on, with where that is, and
below it the DC the bot itself is connected to:

```
📡 Data center
◈ File ▸ DC 4 · Amsterdam
◈ Bot ▸ DC 1 · Miami
```

The places are the usual ones (DC1 and DC3 Miami, DC2 and DC4 Amsterdam, DC5 Singapore). A download connects to the DC the file is stored
on; an upload goes to the bot's own DC. A reply with no file in it (a text, a poll, a location) gets a short hint or `There's no file in that.`
The same DC is shown beside `Downloading` in the progress message of a rename job.

### `/ethumb`, `/size` and `/resize`

`/ethumb` gives you the thumbnail of a file or video: the preview Telegram stores with it, or else a frame grabbed from the video
(from the first 12 MB, or from the whole file when needed). Send `/ethumb` and then the file, or reply to a file with `/ethumb`.
It handles one file per command.

`/size` and `/resize` work on a picture you **reply to**:

* **`/size`** answers `Dimension Of This Image Is` and the width and height in pixels, like `1920 x 1080`. A photo, and a picture sent as
  a file, are measured as they are. A video, GIF, sticker or other file with no picture of its own is measured by the largest preview
  Telegram keeps with it (that one is small).
* **`/resize 720 1080`** sends the picture back at **exactly** 720 pixels wide and 1080 high, whatever shape it had: it is stretched, not
  cropped or padded. It comes back as a PNG, as a reply to your command, and the *Processing…* message disappears. Give two whole
  numbers with a space between them (1 to 4096 each; `720x1080` is not accepted). Without them the bot says
  `Give Some Size To Resize, Like /resize 720 1080` and removes that message by itself after 5 seconds.
* The result is sent as a photo. When Telegram would shrink or refuse a photo (the longer side over 1280 px, one side more than 20 times
  the other, or over 10 MB) it is sent as a file instead, so you always get the size you asked for.
* Pictures up to 20 MB (and up to 80 million pixels for `/resize`). Nothing waits for a picture: reply to one every time.

### `/speed`

Runs an Ookla Speedtest from the machine the bot runs on and shows download, upload, ping, jitter, ISP and server. The official
Ookla CLI is installed by the Dockerfile; if it is missing the `speedtest-cli` package (also Ookla) is used. Only one test runs at a time.
The result page of the test (speedtest.net) is shown as a link preview under the figures. Telegram previews the *first* link of a message and
also turns words like an ISP called `Amazon.com` into links, so the message starts with an invisible link to the result page to make sure
that one is previewed. If a test returns no result page, or fails, there is no preview.
On Hugging Face the test needs outbound access to Ookla's servers (ports 8080 / 5060); if the Space blocks them the command says so.

### `/smedia` (owner only)

Sends the menu with a picture or an animation above it on every `/start`. Send `/smedia`, choose **Image** or **GIF**, then send
it. The bot saves a copy in the database (up to 10 MB), shows you `/start` with it straight away, and uses Telegram's own file when it
can. If Telegram stops accepting the saved file, the copy is uploaded again by itself, so `/start` never loses its picture. Choose
**Remove** to go back to the plain menu.

### Keeping and re-ordering tracks

```
/keepaudio English Japanese     keep English then Japanese, in that order, drop the rest
/keepaudio 2 1                  keep audio track 2 then track 1, drop the rest
/keepsubtitle English           keep only the English subtitles
/keepaudio off                  keep every audio track again
```

* Language names, codes (`eng`, `jpn`, `hin`…) and 1-based numbers all work; reply to a file with `/tracks` to see the numbers.
* The first kept track of each kind becomes the default one.
* If **none** of the listed audio tracks exist in a file, its audio is left untouched (a warning is shown). A subtitle list that matches nothing removes the subtitles.
* Setting a keep list switches Metadata **On** automatically. Chapters and attached fonts are preserved.
* Works for `mkv, mka, mp4, m4v, mov, webm`. For `mp3, m4a, flac, ogg, opus` only Title and Artist/Author are written.

### Naming templates

`{title} {season} {episode} {1episode} {2episode} {3episode} {quality} {year} {codec} {filename} {ext}`. A `[bracket]` or `(paren)` group disappears when a variable inside it is empty:

| Original | Template `[S{season}E{episode}] {title} [{quality}]` |
|---|---|
| `[SubsPlease] Tokyo Revengers - 05 (1080p) [ABCD1234].mkv` | `[S01E05] Tokyo Revengers [1080p].mkv` |
| `Naruto Shippuden S02E14 720p HEVC.mp4` | `[S02E14] Naruto Shippuden [720p].mp4` |
| `Oppenheimer.2023.1080p.BluRay.x265.mkv` | `Oppenheimer [1080p].mkv` |
| `1147 - One Piece [Dual] 720p @Anime_X_Universe.mkv` | `[S01E1147] One Piece [720p].mkv` |

If only an episode number is found the season defaults to `01`.

**Episode numbers with zeros in front.** `{episode}` is always at least two digits (`5` → `05`). The numbered ones add zeros in front:

* `{1episode}` makes the episode **2 digits** (`4` → `04`),
* `{2episode}` makes it **3 digits** (`4` → `004`),
* `{3episode}` makes it **4 digits** (`4` → `0004`).

A number that already has that many digits (or more) is left exactly as it is, so nothing is ever cut:

| Episode found | `{episode}` | `{1episode}` | `{2episode}` | `{3episode}` |
|---|---|---|---|---|
| `4`, `04` or `004` | `04` | `04` | `004` | `0004` |
| `12` | `12` | `12` | `012` | `0012` |
| `321` | `321` | `321` | `321` | `0321` |
| `1147` | `1147` | `1147` | `1147` | `1147` |
| `1321` | `1321` | `1321` | `1321` | `1321` |

The result does not depend on how the file wrote the number (`E4`, `E04` and `E004` are the same episode). They work in templates and in
captions. With no episode found they are empty, so a `[bracket]` group holding one disappears, like `{episode}`.

**Episode layouts that are recognised:** `S01E05`, `1x05`, `E05`, `Ep 05`, `Episode 05`, ` - 05`, an episode written first
(`1147 - Title`, `5 - Title`), a bracketed one (`Title [1147]`) and a bare 3-4 digit number (`Title 1147 720p`).
Years (`2023`), resolutions (`1080`) and numbers that belong to a title (`3-gatsu`, `86-Eighty Six`, `Blade Runner 2049`) are left alone.
A bare 1-2 digit number (`Title 05 720p`) is not treated as an episode, because it is too often part of the title.

If your template uses `{episode}` (or `{1episode}` … `{3episode}`), `{season}`, `{quality}`, `{year}` or `{codec}` and the bot cannot find it, the **Queued** and **Done**
messages show a ⚠️ line saying so (the value is left empty), so a missing number never goes unnoticed.

## Notes and limits

* Telegram's limit for bot uploads is 2000 MB (`MAX_FILE_MB`). Free Spaces have 50 GB of temporary disk; a file needs up to ~2.2× its size while metadata is being applied.
* The bot only reacts in private chats (the one exception: with the Database Channel set up, it reads the channel post bot's answers in the communication chat). Unknown users are ignored; the owner sees them in the log channel with a ready `/addadmin <id>` hint.
* **Log channel (`LOG_CHANNEL`)**: the bot posts *Bot started* when it comes online, the unknown-user notices and **every error**, one message
  each: what it was doing (the message or button press, or the file being renamed), then the full traceback in a Python code block with a copy
  button. A long traceback keeps the bot's own lines and the error and folds the library frames into one line (`[… 25 library frames …]`).
  The same error is sent once per two minutes (the next report says how many times it came back), at most 10 reports go out per
  minute, and the bot token, API hash and database password are masked first. Finished jobs are not posted there.
* The bot only uses its own tables in the database: `users`, `admins`, `kv`, `rename_formats` and `rename_format_shares`. It can share a Postgres with other bots;
  tables of other apps are never read or changed.
* One-file commands (`/rename`, `/ethumb`, `/smedia`) wait 10 minutes for the file. Files that arrive together right after one are skipped with a hint instead of being auto-renamed.
* **TDLib's state lives in the database too.** TDLib keeps its login and what it has learnt about the chats the bot is in in a small folder (`TDLIB_DIR/database`, a few tens of KB). The bot saves it to the `kv` table every 10 minutes when it changed and once more when it stops, and puts it back before TDLib starts, so a restart needs no new login. Without `DATABASE_URL` it is lost with the disk and the bot logs in again with its token; Telegram limits how often a bot may do that, so do not restart it in a loop. Half-downloaded files that a crash leaves in `TDLIB_DIR` are removed at start-up.
* **A chat has to be "seen".** TDLib lets a bot use a channel or a group only after it has received something from it: the bot being added as an admin, or a message posted there. If the log says `cannot access LOG_CHANNEL …` (or `DB_CHANNEL`, `COMMUNICATION_CHAT`), or `/setchannel` says it cannot reach a channel, add the bot as an admin and post any message in that chat; no restart is needed. The saved state above keeps what the bot has seen, so this is normally needed once per chat.
* The old "reply to a file with `/rename New Name`" is replaced by saved formats. Use `/format` for the names and templates you reuse.
* **Upgrading from an earlier version** changes nothing you have to redo, once, at start-up: your template, caption and thumbnail become an auto
  format called **Default**, which is the one in use (nothing is made if you never changed them); your saved formats become manual formats, and
  one that had no thumbnail of its own keeps using your old one. Your metadata stays as your default metadata, and the Default auto format
  has none of its own, so it keeps using it while it is On: files are tagged exactly as before, and changes you make in `/metadata` still count.
  The move is one way: the previous version does not know auto formats, so keep a database backup if you may want to go back.
* **Moving from the Telethon version.** The database tables, formats, commands and settings are the same, so the same `DATABASE_URL` works and nothing has to be converted. Do not run the two versions with the same bot token at the same time: both stay logged in, and the same message can be answered twice. The first start of this version logs in with the bot token; `SESSION_STRING`, `TRANSFER_CONNECTIONS` and `FAST_TRANSFER` are not used any more (remove them), and streaming renames are now off by default (`STREAM_RENAME=1`).

## Troubleshooting

| Symptom | Check |
|---|---|
| Bot never answers | **Logs** tab: is it `online`? `OWNER_ID` must be *your* numeric id. Strangers are ignored silently |
| The log says `Telegram did not accept the login` or `the bot was not logged in within 90 seconds` | `BOT_TOKEN` (from @BotFather), `API_ID` and `API_HASH` (from my.telegram.org) must be exactly right, without spaces or quotes. If they are, the Space cannot reach Telegram: restart / rebuild it (it may land on another host). The bot stops after this message |
| Logs repeat connection errors | The Space cannot reach Telegram. Restart / rebuild the Space (it may land on another host). `TDLIB_LOG_LEVEL=2` shows more of what TDLib is doing |
| Settings, formats or admins vanish after a restart | Set `DATABASE_URL` (Postgres). Without it SQLite lives on the temporary disk, and TDLib's saved login (and the chats it has seen) goes with it |
| Slow transfers | `/status` → **Speed**: the lines should have ✅ (a `no uvloop` ⚠️ only slows the bot's own code). The Done message shows ⬇ / ⬆ MB/s for each file, and `/dc` tells where a file is stored: one on a DC far from the Space is slower, because TDLib keeps only 2 MB in flight per DC for a bot (see *Is it really fast?*). There is nothing to tune in TDLib itself. For a batch, `MAX_CONCURRENT_JOBS=2` overlaps one file's upload with the next one's download, but then files can come back out of order (a small one can overtake a big one), so keep it at 1 when the order matters |
| An upload or download sits still for a while, then goes on | TDLib reconnects and retries by itself, and Telegram can also ask a bot to wait (a *flood wait*; short ones are waited out automatically). Keep `MAX_CONCURRENT_JOBS` low when several files run together. If it keeps happening, send the log, with `TDLIB_LOG_LEVEL=2` set |
| `FloodWaitError` in a failed job | Telegram asked the bot to slow down; retry after the stated time |
| Nothing arrives in the log chat | `LOG_CHANNEL` must be the numeric id (like `-1001234567890`) and the bot must be an admin there. The Space log says `could not send an error report` if Telegram refuses |
| The log says `cannot access LOG_CHANNEL …`, `cannot access DB_CHANNEL …` or `cannot access COMMUNICATION_CHAT …` | TDLib lets a bot use a chat only after it has seen something from it. Add the bot to that chat as an admin and post any message there (see *A chat has to be "seen"* above). The same goes for a format's **Channel** and `/setchannel` |
| Share format says "Sharing needs inline mode" | @BotFather → `/mybots` → your bot → Bot Settings → Inline Mode → Turn on, then tap the button again |
| Captions are not bold | Only captions that were still the old plain default were changed. For the others set the caption `<b>{filename}</b>` (`/format` → the format → **Caption**) |
| A manual format sends a different caption than my auto format | By design: every format has its own caption (the new file name in bold until you set one). Open the format → **Caption** |
| Where did my old template, caption and thumbnail go? | Into the auto format called **Default**: `/start` → **Auto Format** → **Edit the one in use**. That screen is the only place to change them |
| I sent a photo and nothing happened | By design: a photo is a thumbnail only when you asked for one. Open the format in `/format`, tap **Thumbnail**, then send the photo |
| `/resize` shows `Give Some Size To Resize…` | Reply to the picture and give two whole numbers with a space between them, like `/resize 720 1080` (not `720x1080`) |
| `/rename` does not list my auto formats | By design: `/rename` is for manual formats. Choose the auto format in the `/start` menu, or copy an auto format to the manual ones |
| Caption rejected | The caption was too long once the variables were filled in, or Telegram refused its formatting; the file is sent without caption and a warning is shown |
| `/rename` never offers the upload | Both `DB_CHANNEL` and `COMMUNICATION_CHAT` must be set (the log says `uploading to the Database Channel is off` and which one is missing). Only files renamed with a **manual** format are offered |
| The format has no **Channel** button | The option is for manual formats and appears only when both `DB_CHANNEL` and `COMMUNICATION_CHAT` are set |
| *The upload to the Database Channel failed* | The bot must be an admin of the Database Channel and allowed to post there; the message shows Telegram's reason. **Try again** repeats it |
| *The channel post bot is down or not responding* | Is it running and in the communication chat? In @BotFather turn on **Bot-to-Bot Communication Mode** for both bots, and check `CHANNEL_POST_BOT_ID`. The episode is already in the Database Channel |
| `/mediainfo` sends a `.txt` file | Telegraph could not be reached or refused the page; the message says why. The file has the same report |
| The Space build fails at `pip install` | TDLib comes as the `tdjson` wheel (a prebuilt library) that `Pytdbot` uses; both are pinned in `requirements.txt`. The image (Python 3.12) has no compiler, so every dependency needs a `cp312` `manylinux` wheel: if pip says there is no matching distribution, or starts building something, check on PyPI that the pin you changed lists one. The Dockerfile installs with `--prefer-binary`, so a release that ships only source never wins over an older one with wheels |
| The bot stops at start-up with an error about `libtdjson` or `libstdc++` | TDLib needs the system library `libstdc++6`; the Dockerfile installs it. If you run another image, install it there |
| `/speed` fails | The message names the reason. The Space may block Ookla's ports; the log shows which client was tried |
| `/start` has no picture | `/smedia` shows what is set. Set it again if it says "nothing"; the log shows why a saved file could not be sent |

## Run locally / tests

```bash
pip install -r requirements.txt
export API_ID=… API_HASH=… BOT_TOKEN=… OWNER_ID=…     # needs ffmpeg + ffprobe on PATH
python -m bot

pip install -r requirements-dev.txt && pytest          # 1300+ tests, no network needed
```

`pymediainfo` brings its own MediaInfo library. For `/speed` locally, install Ookla's CLI as `ookla-speedtest` (or set
`SPEEDTEST_BIN`); without it the `speedtest-cli` package is used.
