# Jarvis — a personal AI assistant for Windows

Jarvis is a voice and chat assistant that lives on your Windows PC. Say **"Hey Jarvis"** (or hold
the **Right Ctrl** key and talk), or type in its chat window, and it answers out loud. It can
search the web, make pictures, set reminders, work with files on your Desktop, look at your
screen, control your PC and more.

It is a ready-to-use setup of the open-source
[OpenJarvis](https://github.com/open-jarvis/OpenJarvis) assistant. A one-click installer adds a
voice bar, extra tools and a glowing golden "speaking orb". Jarvis's brain is NVIDIA's free AI
models, so all you need is a free NVIDIA account.

> **Windows 10/11 only.** Not affiliated with NVIDIA or the OpenJarvis authors.

---

## What Jarvis can do

| Area | Examples |
|---|---|
| **Talk** | "Hey Jarvis, what's the weather?" — or hold **Right Ctrl**, speak, let go |
| **Web** | "What's the latest news about space?" · "Summarise this article: https://…" · "What's this YouTube video about? <link>" |
| **Pictures** | "Make a picture of a sunset over the sea, oil-painting style" (made with NVIDIA's free FLUX image model) |
| **Photo editing** | "Remove the background from me.jpg on my Desktop" · "Turn my photo into a pencil sketch" |
| **Your screen** | "What's on my screen?" · "Explain this error" — or click the eye button in the chat |
| **Files** | "What's on my Desktop?" · "Summarise the PDF called notes" · "Make a folder called Trips" |
| **Reminders & timers** | "Remind me at 5:30 pm to call Mom" · "Set a timer for 10 minutes" |
| **Daily tasks** | "Every day at 8 AM, summarise the top 3 tech news stories" |
| **PC controls** | "Volume to 30" · "Pause the music" · "Brightness to 50" · "Lock my PC" · "Open Spotify" |
| **Copied text** | "Translate what I copied into Hindi" · "Fix the grammar and copy it back" |
| **Weather & briefing** | "Will it rain tomorrow?" · "Morning briefing" |
| **Gmail & Calendar** *(optional, read-only)* | "Any important emails today?" · "What's on my calendar tomorrow?" |
| **Memory** | "Remember that my exam is on the 20th" — Jarvis remembers it in every chat |

**Also included:**
- **"Hey Jarvis" voice bar** at the bottom of the screen. Drag it anywhere; double-click to put it back.
- **Hold-to-talk** with Right Ctrl, which works even next to a noisy fan or AC.
- **A golden animated orb** that moves with Jarvis's voice while he speaks.
- **A chat window** that opens like a normal app, with a dark violet theme.
- **A light sense of humour.** Say "be serious" to switch it off.
- **Starts by itself** when you sign in to Windows.
- **Faster voice on NVIDIA graphics cards:** if you have one, listening and speaking run on it automatically.

---

## What you need

- Windows 10 or 11 (64-bit)
- About **12 GB** of free disk space
- An internet connection (the install downloads about 3 GB; Jarvis needs the internet to think)
- A free **NVIDIA account** for Jarvis's brain — the installer shows you how to get the key
- Optional: an NVIDIA graphics card (makes the voice faster; not required)

---

## Install

1. **Download:** click the green **Code** button on this page, then **Download ZIP**
   (or download the latest file from **Releases**, if there is one).
2. **Unzip:** right-click the downloaded file → **Extract All** → **Extract**.
3. **Run:** open the extracted folder and double-click **`Install Jarvis`**.
   - If Windows says *"Windows protected your PC"*: click **More info**, then **Run anyway**.
   - If it asks *"Do you want to run this file?"*: click **Run**.
4. **Wait:** a window shows the progress (about 15–40 minutes, mostly downloading).
   Type your city when asked (used for the weather).
5. **Add your free NVIDIA key:** near the end, a web page and Notepad open.
   - Sign in to [build.nvidia.com](https://build.nvidia.com) (free) and click **Generate API Key**.
   - Copy the key (it starts with `nvapi-`) and paste it into Notepad.
   - Press **Ctrl+S** to save, close Notepad, and press **Enter** in the setup window.
6. **Done!** Jarvis opens. A **JARVIS** folder on your Desktop has the shortcuts and a full
   guide, **"How to use Jarvis"**.

If the setup stops with a message, fix what it says (usually the internet connection) and
double-click **Install Jarvis** again — it carries on where it stopped.

---

## Using Jarvis

- **Chat window:** double-click the **Jarvis** icon on your Desktop. To have replies read
  aloud, click the sound-wave button next to the microphone in the message box.
- **Voice bar:** say **"Hey Jarvis"**, or **hold Right Ctrl**, say your request and let go.
  Jarvis answers out loud, and for a few seconds afterwards you can just keep talking.
- **Stop him talking:** click the golden orb, or press **Esc**.
- **Shortcuts** in the Desktop **JARVIS** folder: *Jarvis*, *Restart Jarvis*, *Stop Jarvis*,
  *Save Google setting*, *Uninstall Jarvis*.

### Optional: Gmail and Google Calendar (read-only)
1. Create a Google **app password** at <https://myaccount.google.com/apppasswords>
   (needs 2-Step Verification).
2. Copy your calendar's **"Secret address in iCal format"** (Google Calendar → Settings →
   your calendar → *Integrate calendar*).
3. Copy each of these, one at a time, and double-click the **"Save Google setting (copy it
   first)"** shortcut after each copy:
   - the secret calendar address
   - the 16-letter app password
   - your Gmail address

Jarvis can read your email and calendar, but it can't send, delete or change anything.

### Settings
Edit `JARVIS-SETTINGS.txt` in the Jarvis folder (your city, and whether to use the graphics
card for voice), save, then double-click **Restart Jarvis**.

---

## Privacy and safety

- Jarvis runs on **your** PC. Your chats, memories and settings stay on your PC.
- What you ask is sent to **NVIDIA's AI** to be answered. A screenshot or an email is only
  sent when you ask about it.
- The "Hey Jarvis" wake word is detected **on your PC**. Nothing is sent until you say it or
  hold Right Ctrl.
- **Files:** Jarvis can only see and change files in your **Desktop** folder.
- **Commands:** it can open apps, folders, websites and settings, but it **cannot** run
  programs or commands.
- **The chat window** is only reachable from your own PC.
- **Keep private:** never share or upload your `NVIDIA-API-KEY.txt` or `GOOGLE-ACCOUNT.txt` files.

---

## Uninstall

Double-click **Uninstall Jarvis** in the Desktop **JARVIS** folder. It asks before deleting
your chats and memories. Your pictures, screenshots and Desktop files are never touched.

---

## Troubleshooting

- **Jarvis doesn't open or answer:** double-click **Restart Jarvis**. If it can't start, a
  message says why (for example, a missing NVIDIA key).
- **"Hey Jarvis" doesn't react:** check that desktop apps may use the microphone (Windows
  Settings → Privacy & security → Microphone). In a noisy room, use **hold Right Ctrl**, or
  earphones with a microphone.
- **The first spoken reply is slow:** the voice downloads once (about 1 GB) the first time.
- **Windows Smart App Control:** supported. The installer uses Python's standard launchers,
  which Windows trusts, and Jarvis checks them again on every start.
- **Logs:** the `logs` folder inside the Jarvis folder (`%USERPROFILE%\OpenJarvis\logs`).

---

## How it works (for the curious)

| Part | What it is |
|---|---|
| Brain | [OpenJarvis](https://github.com/open-jarvis/OpenJarvis) server using NVIDIA NIM models (`nemotron-3.5-super` by default) |
| Tools | Small local add-ons for PC control, reminders, weather (Open-Meteo), pictures (NVIDIA FLUX), photo editing (Pillow, rembg), screen reading, web pages and YouTube, and read-only Gmail/Calendar |
| Desktop files | The official MCP filesystem server, limited to the Desktop |
| Voice | Listening with faster-whisper; speaking with Kokoro (British voice); wake word with openWakeWord — all on your PC |
| Chat window | The OpenJarvis web app, opened in its own Microsoft Edge app window |
| Installer | PowerShell script; downloads exact, tested versions of Python, uv, Node.js and all packages |

**This repository:**

```
Install Jarvis.cmd        double-click to install
READ ME FIRST.txt         short install steps
installer/
  install.ps1             the installer
  uninstall.ps1           copied into the Jarvis folder as "Uninstall Jarvis"
  config-template.toml    Jarvis's settings, filled in for each PC
  guide-template.txt      the "How to use Jarvis" guide
  requirements/           exact package versions for the add-ons
  source/                 OpenJarvis source (with the local changes) and its built web app
  wheels/                 ready-built part of OpenJarvis (so no compiler is needed)
  app/                    start/stop scripts, icons, voice bar and tool add-ons
  home/SOUL.md            Jarvis's personality
```

---

## Credits and licences

- **[OpenJarvis](https://github.com/open-jarvis/OpenJarvis)** — the assistant this is built on
  (Apache License 2.0; the full licence is inside `installer/source`).
- **[openWakeWord](https://github.com/dscripka/openWakeWord)** — the "Hey Jarvis" wake word.
  Its pre-trained models are licensed **CC BY-NC-SA 4.0: personal, non-commercial use only**.
- **[Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)** (voice),
  **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** (listening),
  **[rembg](https://github.com/danielgatis/rembg)** (background removal),
  **[Open-Meteo](https://open-meteo.com)** (weather), **[uv](https://github.com/astral-sh/uv)**,
  **[Node.js](https://nodejs.org)** and the
  **[MCP filesystem server](https://github.com/modelcontextprotocol/servers)**. They are
  downloaded during installation, each under its own licence.
- AI models are provided by **NVIDIA** through [build.nvidia.com](https://build.nvidia.com),
  under NVIDIA's terms of use.

*Not affiliated with NVIDIA, the OpenJarvis authors, or any of the projects above.*
