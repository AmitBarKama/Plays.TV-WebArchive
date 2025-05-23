# Plays.TV-archive-downloader
a script that download your lost and  precious gaming moments from plays.tv
# Plays.tv Video Downloader

A tool to download videos from archived Plays.tv profiles using the Wayback Machine.

## Description

This script allows you to recover videos from Plays.tv profiles that have been archived on the Internet Archive's Wayback Machine. It navigates to a Plays.tv profile, extracts links to individual videos, and then downloads them.

## Features

- Scrolls through profile pages to find all videos
- Extracts video URLs from archived pages
- Downloads videos with automatic retry on connection errors
- Test mode to limit to 10 videos for testing purposes

## Requirements

- Python 3.6+
- Firefox browser
- Firefox WebDriver (geckodriver)

## Installation

1. Clone this repository or download the script
2. Install required packages:
    ```bash
    pip install -r requirements.txt
    ```
3. Download [geckodriver](https://github.com/mozilla/geckodriver/releases) and make sure it's in your PATH

## Usage

1. Edit the configuration at the top of the script:
   ```python
   # === CONFIGURATION ===
   PROFILE_URL = "https://web.archive.org/web/20190101000000/https://plays.tv/u/YourUsername"
   VIDEO_DIR = "playstv/videos"
   TEST_MODE = True  # Set to False for full download
   # ======================

