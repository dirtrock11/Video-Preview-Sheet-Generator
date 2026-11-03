# ✨Video Preview Sheet Generator

**Video Preview Sheet Generator** is a fast, desktop-based application for creating contact sheets and video frame previews from your video files. Drag and drop single or multiple videos to generate high-quality grid previews with customizable timestamps, borders, font styles, and layouts.

## Features

* **Drag & Drop Interface:** Easily process single files or entire video directories.

* **Custom Layouts:** Adjust grid dimensions (columns/rows), overall resolution, and border styles.

* **Timestamp Overlay:** Burn-in frame timestamps with custom positioning, font sizes, colors, and strokes.

* **FFmpeg Integration:** Automated frame extraction powered by FFmpeg for fast and accurate processing.

* **Built-in Install Assistant:** Detects missing FFmpeg installations and offers a quick command to install it automatically.

* **Dark & Light Themes:** Toggleable UI styles with custom themes.

## 🚀 Quick Start (Pre-Compiled Release)

If you just want to run the application on Windows without installing Python or setting up an environment:

1. Download the latest executable (`Video Preview Sheet Generator-Setup.exe`) from the **[Releases](../../releases)** page.
2. Launch `Video Preview Sheet Generator-Setup.exe` to automatically install/run the program.
3. If FFmpeg is missing from your system, the application will prompt you with a 1-click **"Install now"** option to set up FFmpeg automatically via `winget`.

## System Requirements & Prerequisites

### 1. Python & Dependencies

* **Python 3.10+**

* Python libraries: `PySide6`, `Pillow`, and `pyinstaller` (specified in `requirements.txt`).

To install Python dependencies:

```
pip install -r requirements.txt

```

### 2. FFmpeg Requirement

This application requires **FFmpeg** and **FFprobe** on your system `PATH` to extract video frames and retrieve metadata.

#### Built-in Automatic FFmpeg Installation (Windows)

If FFmpeg is not detected when you launch the application, a pop-up dialog will notify you.

The dialog includes:

* An **"Install now"** button that automatically opens a prompt and executes the command via `winget`:

  ```
  winget install Gyan.FFmpeg
  
  ```

* A **"Copy command"** button so you can manually paste the command into PowerShell or Command Prompt.

#### Manual FFmpeg Installation

If you prefer to install FFmpeg manually:

* **Windows (via Winget):** Run `winget install Gyan.FFmpeg` in Terminal.

* **macOS (via Homebrew):** Run `brew install ffmpeg` in Terminal.

* **Linux (Debian/Ubuntu):** Run `sudo apt update && sudo apt install ffmpeg` in Terminal.

## Running & Building the Application

### Running as a Python Script

```
python VideoPreviewSheet.py

```

