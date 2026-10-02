# 📚 Telegram AI Audiobook Bot

<p align="center">
  <strong>Turn PDF books into chapter-by-chapter audiobooks directly inside Telegram.</strong>
</p>

<p align="center">
  Built with
  <strong>Python</strong> ·
  <strong>Aiogram</strong> ·
  <strong>Google Gemini</strong> ·
  <strong>Deepgram Aura</strong> ·
  <strong>Microsoft Edge-TTS</strong> ·
  <strong>FFmpeg</strong> ·
  <strong>PyMuPDF</strong>
</p>

---

## 🧠 About

**Telegram AI Audiobook Bot** is a Python-based Telegram bot that converts PDF books into MP3 audiobooks.

The bot extracts text from PDF files, detects chapters, splits long chapters into manageable chunks, generates speech using multiple text-to-speech engines, combines the resulting audio, and sends the finished audiobook back through Telegram.

The system is designed around a **multi-engine fallback architecture**, allowing it to continue processing when one TTS provider or API key becomes unavailable.

---

## ✨ Features

### 🎙️ Multi-Engine Text-to-Speech

The bot supports multiple TTS engines:

* **Google Gemini TTS**

  * Natural-sounding AI narration.
  * Supports configurable voice selection.
  * Can be used with narration/style instructions where supported by the selected Gemini model.
  * Multiple API keys can be configured.

* **Deepgram Aura**

  * Fast neural text-to-speech generation.
  * Supports Aura-2 voices such as `aura-2-thalia-en`.
  * Multiple API keys can be configured for fallback.

* **Microsoft Edge-TTS**

  * Used as an additional fallback engine.
  * Provides access to Microsoft neural voices.
  * Does not require a paid API key.

The exact capabilities of each engine depend on the model, API, account, and configuration being used.

---

## 🔄 Multi-Key Failover

The bot can use multiple API keys for supported providers.

For example:

```text
Gemini Key 1
     ↓
Gemini Key 2
     ↓
Gemini Key 3
     ↓
Deepgram Key 1
     ↓
Deepgram Key 2
     ↓
Edge-TTS
```

If an API request fails because of a supported quota, rate-limit, or provider error, the bot can move to another configured key or TTS engine according to the application's fallback logic.

> **Important:** Multiple API keys do not automatically create unlimited quota. Each key's actual limits depend on the provider and the projects/accounts associated with those keys.

---

## 📖 Smart PDF Processing

The bot uses **PyMuPDF** for PDF processing and text extraction.

It can:

* 📄 Extract text from PDF pages.
* 🧹 Clean extracted text.
* 🔢 Remove unnecessary page-number patterns where detected.
* 📑 Detect chapters using configured chapter-heading patterns.
* 🔖 Use PDF document structure/bookmarks where supported by the implementation.
* ✂️ Split large chapters into smaller speech-generation chunks.
* 📝 Preserve the logical order of chapter content.

PyMuPDF is a Python library for PDF/document extraction and manipulation.

---

## ⚡ Parallel Audio Processing

Long chapters can be divided into smaller chunks.

Example:

```text
Chapter 1
   │
   ├── Chunk 1 ──► TTS
   ├── Chunk 2 ──► TTS
   ├── Chunk 3 ──► TTS
   └── Chunk 4 ──► TTS
                  │
                  ▼
             FFmpeg Merge
                  │
                  ▼
            Chapter 1.mp3
```

This allows large chapters to be processed without sending one enormous request to a TTS provider.

The resulting audio chunks can then be combined using **FFmpeg**.

---

## 💾 Resume & Progress Tracking

The project uses SQLite to keep track of audiobook processing.

This allows the bot to maintain information such as:

* 📚 Current book
* 📖 Current chapter
* 🎧 Completed chapters
* ⏳ Processing state
* ❌ Failed processing jobs
* 🔄 Resume information

If processing is interrupted, the bot can use the stored state to continue according to the application's resume logic rather than starting the entire book from the beginning.

---

# 🛠️ Technology Stack

| Technology                | Purpose                         |
| ------------------------- | ------------------------------- |
| 🐍 **Python**             | Main programming language       |
| 🤖 **Aiogram 3**          | Telegram bot framework          |
| ✨ **Google Gemini**       | AI text-to-speech               |
| 🎙️ **Deepgram Aura**     | Neural text-to-speech           |
| 🔊 **Microsoft Edge-TTS** | Additional TTS fallback         |
| 🎬 **FFmpeg**             | Audio conversion and merging    |
| 📄 **PyMuPDF**            | PDF parsing and text extraction |
| 🗄️ **SQLite**            | Persistent job/progress storage |

Aiogram is an asynchronous Python framework for building Telegram bots using `asyncio` and `aiohttp`.

Google's Gemini API provides the API interface used to access Gemini models.

Deepgram's current documentation identifies **Aura-2** as its broad-language TTS model family, with model identifiers such as `aura-2-thalia-en`.

---

# 🧰 Prerequisites

Before running the bot, install the following.

## 1. Python

Python **3.10 or newer** is recommended.

Check your version:

```bash
python --version
```

---

## 2. FFmpeg

FFmpeg is required for audio processing and merging.

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install ffmpeg
```

### macOS

Using Homebrew:

```bash
brew install ffmpeg
```

### Windows

Using Winget:

```powershell
winget install Gyan.FFmpeg
```

Verify the installation:

```bash
ffmpeg -version
```

---

## 3. Telegram Bot

Create a Telegram bot using **@BotFather** and obtain your bot token.

Your token should be kept private and should **never be committed to GitHub**.

---

# 🚀 Installation

## 1. Clone the repository

```bash
git clone https://github.com/your-username/telegram-audiobook-bot.git
cd telegram-audiobook-bot
```

---

## 2. Create a virtual environment

### Windows

```bash
python -m venv venv
venv\Scripts\activate
```

### Linux / macOS

```bash
python3 -m venv venv
source venv/bin/activate
```

---

## 3. Install dependencies

```bash
pip install -r requirements.txt
```

---

# 🔐 Environment Configuration

Create a `.env` file in the project root.

Example:

```env
# =========================================================
# Telegram
# =========================================================

BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN


# =========================================================
# Google Gemini
# =========================================================

# Multiple Gemini API keys can be separated by commas.
GEMINI_API_KEYS=YOUR_GEMINI_KEY_1,YOUR_GEMINI_KEY_2,YOUR_GEMINI_KEY_3

# Gemini TTS model configured by the project.
GEMINI_TTS_MODEL=YOUR_GEMINI_TTS_MODEL

# Gemini voice configured by the project.
GEMINI_VOICE=YOUR_GEMINI_VOICE

# Optional narration/style instruction.
GEMINI_STYLE=calm, clear audiobook narrator


# =========================================================
# Deepgram
# =========================================================

# Multiple Deepgram API keys can be separated by commas.
DEEPGRAM_API_KEYS=YOUR_DEEPGRAM_KEY_1,YOUR_DEEPGRAM_KEY_2

# Example Aura-2 model:
DEEPGRAM_MODEL=aura-2-thalia-en


# =========================================================
# Microsoft Edge-TTS
# =========================================================

EDGE_VOICE=en-US-AriaNeural


# =========================================================
# Optional Access Control
# =========================================================

# Restrict the bot to specific Telegram user IDs.
# Leave empty if the bot should be publicly accessible.

ALLOWED_USER_IDS=
```

### ⚠️ Keep your `.env` private

Never upload your real API keys to GitHub.

Add this to `.gitignore`:

```gitignore
# Environment variables
.env
.env.*
!.env.example

# Python
__pycache__/
*.py[cod]
*.pyo
*.pyd

# Virtual environment
venv/
.venv/

# Local database
*.db
*.sqlite
*.sqlite3

# Generated audio
*.mp3
*.wav
*.ogg
*.m4a

# Temporary files
tmp/
temp/
cache/
output/
```

---

# ▶️ Running the Bot

After configuring `.env`:

```bash
python bot.py
```

If your system uses `python3`:

```bash
python3 bot.py
```

The bot should then connect to Telegram and begin listening for updates.

---

# 🎮 Bot Commands

| Command   | Description                                                         |
| --------- | ------------------------------------------------------------------- |
| `/start`  | Start the bot and display the main menu                             |
| `/engine` | Select the preferred TTS engine                                     |
| `/style`  | Configure the Gemini narration/style instruction                    |
| `/sample` | Generate a short audio sample using the current voice configuration |
| `/status` | View the current audiobook processing status                        |
| `/cancel` | Stop the active audiobook conversion                                |
| `/resume` | Continue a previously interrupted conversion                        |

> The exact commands and behavior depend on the implementation in `bot.py`.

---

# 🎙️ TTS Engine Modes

The bot can be configured to use different processing strategies.

### Auto

```text
Gemini
   ↓
Deepgram
   ↓
Edge-TTS
```

The bot attempts the configured primary provider first and falls back according to the application's failure-handling logic.

### Fast

Uses the configured fast TTS provider.

### Edge

Uses Microsoft Edge-TTS directly.

---

# 🔁 Fallback Architecture

A simplified view of the fallback system:

```text
                 ┌─────────────────┐
                 │   Text Chunk    │
                 └────────┬────────┘
                          │
                          ▼
                ┌───────────────────┐
                │   Gemini Key #1   │
                └─────────┬─────────┘
                          │
                   Failure / Limit
                          │
                          ▼
                ┌───────────────────┐
                │   Gemini Key #2   │
                └─────────┬─────────┘
                          │
                   Failure / Limit
                          │
                          ▼
                ┌───────────────────┐
                │   Gemini Key #3   │
                └─────────┬─────────┘
                          │
                   Failure / Limit
                          │
                          ▼
                ┌───────────────────┐
                │ Deepgram Key #1   │
                └─────────┬─────────┘
                          │
                   Failure / Limit
                          │
                          ▼
                ┌───────────────────┐
                │ Deepgram Key #2   │
                └─────────┬─────────┘
                          │
                   Failure / Limit
                          │
                          ▼
                ┌───────────────────┐
                │     Edge-TTS      │
                └─────────┬─────────┘
                          │
                          ▼
                   Generated Audio
```

Deepgram's Aura API accepts a model such as `aura-2-thalia-en` through its `/v1/speak` endpoint.

---

# 📚 Audiobook Processing Pipeline

The complete processing flow is approximately:

```text
        📄 PDF Book
             │
             ▼
      ┌──────────────┐
      │   PyMuPDF    │
      │ Text Extract │
      └──────┬───────┘
             │
             ▼
      🧹 Text Cleaning
             │
             ▼
      📖 Chapter Detection
             │
             ▼
       ✂️ Text Chunking
             │
             ▼
      🎙️ TTS Generation
             │
             ▼
      🎧 Audio Chunks
             │
             ▼
       🎬 FFmpeg Merge
             │
             ▼
        📚 Chapter MP3
             │
             ▼
       📤 Telegram Delivery
```

---

# 📦 Project Structure

```text
telegram-audiobook-bot/
│
├── bot.py
│   └── Aiogram handlers, Telegram interaction,
│       queues, commands, and audio delivery
│
├── config.py
│   └── Environment configuration, API keys,
│       directories, and application settings
│
├── db.py
│   └── SQLite database, progress tracking,
│       jobs, chapters, and resume state
│
├── pdf_utils.py
│   └── PDF extraction, text cleaning,
│       chapter detection, and chunk splitting
│
├── tts.py
│   └── TTS providers, API-key rotation,
│       fallback handling, audio generation,
│       and FFmpeg processing
│
├── requirements.txt
│   └── Python dependencies
│
├── .env
│   └── Private API keys and configuration
│
├── .gitignore
│   └── Files excluded from Git
│
└── README.md
    └── Project documentation
```

---

# 🔒 Security

API keys are sensitive credentials.

**Never commit these values to Git:**

```text
BOT_TOKEN
GEMINI_API_KEYS
DEEPGRAM_API_KEYS
```

If a key is accidentally exposed publicly:

1. Revoke the exposed key.
2. Generate a new key.
3. Replace it in `.env`.
4. Check your Git history if the key was committed.

---

# 🧪 Development

For development, it is recommended to use a virtual environment:

```bash
python -m venv venv
```

Activate it:

### Windows

```bash
venv\Scripts\activate
```

### Linux / macOS

```bash
source venv/bin/activate
```

Then install dependencies:

```bash
pip install -r requirements.txt
```

---

# 📝 Notes

### Gemini API Keys

Multiple Gemini keys can be configured, but each key is still subject to the quota and restrictions of the project/account associated with it. Google documents that Gemini API keys are associated with Google Cloud projects, which manage billing, permissions, and quota.

### Deepgram

The example configuration uses:

```text
aura-2-thalia-en
```

Deepgram currently documents Aura-2 as a TTS family with multiple voices and languages.

### PDF Extraction

PyMuPDF provides PDF text extraction and supports working with several document formats.

### Audio Processing

FFmpeg is used by the project for audio processing and merging.

---

# 📜 License

MIT License.

You are free to use, modify, distribute, and build upon this project according to the terms of the MIT License.

---

<p align="center">
  Made with 🐍 Python · 🤖 Telegram · ✨ Gemini · 🎙️ Deepgram · 🔊 Edge-TTS · 🎬 FFmpeg
</p>
