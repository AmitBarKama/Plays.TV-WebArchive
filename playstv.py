# === CONFIGURATION ===
PROFILE_URL = "https://web.archive.org/web/20190101000000/https://plays.tv/u/yourUsername"
VIDEO_DIR = "YOUR_DIR"
TEST_MODE = True  # Set to True to enable test mode (limit to 10 videos)
# ======================

from selenium.webdriver.common.by import By
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.firefox.options import Options
from time import sleep
import os
import requests

URL_LIST_FILE = 'video_urls.txt'


def scroll_down(driver, test_mode=False):
    """Scroll down the page to load more content. Stops early in test mode."""
    last_height = driver.execute_script('return document.body.scrollHeight')
    scroll_count = 0
    while True:
        driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        sleep(5)
        new_height = driver.execute_script('return document.body.scrollHeight')

        if new_height == last_height:
            print('[*] Finished scrolling.')
            break

        # In test mode, only scroll a few times
        if test_mode:
            scroll_count += 1
            if scroll_count >= 2:  # Adjust this number if needed
                print('[*] Test mode: limited scrolling.')
                break

        last_height = new_height


def grab_urls(driver):
    elements = driver.find_elements(By.CSS_SELECTOR, '.bd .video-list-container a.title')
    urls = [el.get_attribute('href') for el in elements]
    print(f'[*] Found {len(urls)} video page URLs.')
    return urls


def grab_video_urls(driver, urls, test_mode=False):
    video_urls = []
    for idx, url in enumerate(urls):
        # Check if we've reached the test mode limit
        if test_mode and len(video_urls) >= 10:
            print(f'[*] Test mode: reached 10 videos, stopping collection.')
            break

        # Extract title from the URL before the query parameters
        if '?' in url:
            title = url.split('?')[0].strip('/').split('/')[-1]
        else:
            title = url.strip('/').split('/')[-1]

        try:
            print(f'[*] Visiting: {url} ({idx + 1}/{len(urls)})')
            driver.get(f'https://web.archive.org/web/20190101000000/{url}')
            sleep(5)

            # Extract the Wayback Machine timestamp from the current URL
            current_url = driver.current_url
            timestamp = None
            if '/web/' in current_url:
                timestamp = current_url.split('/web/')[1].split('/')[0]
                print(f'[*] Found timestamp: {timestamp}')

            # Look for video element
            try:
                video = driver.find_element(By.CSS_SELECTOR, '.ui-player > video')
                poster_url = video.get_attribute('poster')

                if poster_url:
                    print(f'[DEBUG] Poster URL: {poster_url}')
                    # Extract the original URL path from the poster URL
                    if '/web/' in poster_url:
                        # Already has Wayback Machine format
                        poster_parts = poster_url.split('/')
                        # Replace the thumbnail filename with 720.mp4
                        video_url = '/'.join(poster_parts[:-1]) + '/720.mp4'
                    else:
                        # Direct CDN URL - needs Wayback Machine prefix
                        video_url = poster_url.rsplit('/', 1)[0] + '/720.mp4'
                        if timestamp:
                            video_url = f"https://web.archive.org/web/{timestamp}/{video_url}"

                    video_urls.append((title, video_url))
                    print(f'[+] Found video URL: {title} -> {video_url}')
                    continue
            except NoSuchElementException:
                print(f'[!] No video element found for {title}')
                pass

            # If we didn't find a video URL, report it
            if title not in [t for t, u in video_urls]:
                print(f'[!] No valid video URL found for {title}')

        except WebDriverException as e:
            print(f'[!] Error loading {url}: {str(e)}')
            continue

    return video_urls


def download_all(video_url_list, test_mode=False):
    if not os.path.exists(VIDEO_DIR):
        os.makedirs(VIDEO_DIR)
    else:
        print(f'[!] Directory "{VIDEO_DIR}" already exists. Please move/delete it first to prevent overwriting.')
        return

    # Limit number of downloads in test mode
    if test_mode and len(video_url_list) > 10:
        print(f'[*] Test mode: limiting downloads to 10 videos.')
        video_url_list = video_url_list[:10]

    for title, url in video_url_list:
        filename = os.path.join(VIDEO_DIR, f'{title}.mp4')
        print(f'[*] Downloading {title}...')
        while True:
            try:
                with requests.get(url, stream=True, allow_redirects=True) as r:
                    r.raise_for_status()
                    with open(filename, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=1024 * 1024):
                            f.write(chunk)
                print(f'[+] Saved: {filename}')
                break
            except (requests.exceptions.RequestException, ConnectionError):
                print('[!] Connection error. Retrying...')
                sleep(2)
                continue


def main():
    options = Options()
    options.headless = False  # Set True to run in background
    driver = webdriver.Firefox(options=options)

    if not os.path.exists(URL_LIST_FILE):
        print('[*] Loading profile and collecting video URLs...')
        driver.get(PROFILE_URL)

        # Apply test mode to scrolling
        scroll_down(driver, TEST_MODE)
        urls = grab_urls(driver)

        cleaned_urls = []
        for url in urls:
            if 'web.archive.org/web/' in url:
                parts = url.split('web.archive.org/web/')[1]
                if '/' in parts:
                    timestamp, actual_url = parts.split('/', 1)
                    actual_url = actual_url.split('?')[0]
                    cleaned_url = f"https://web.archive.org/web/{timestamp}/{actual_url}"
                else:
                    cleaned_url = url.split('?')[0]
            else:
                cleaned_url = url.split('?')[0]
            cleaned_urls.append(cleaned_url)

        # Apply test mode to video URL collection
        videos = grab_video_urls(driver, cleaned_urls, TEST_MODE)

        with open(URL_LIST_FILE, 'w') as f:
            for title, url in videos:
                f.write(f'{title}|{url}\n')
        print(f'[+] Saved {len(videos)} video URLs to {URL_LIST_FILE}')
    else:
        print(f'[*] URL list already exists: {URL_LIST_FILE}, skipping collection.')

    driver.quit()

    print('[*] Starting download...')
    with open(URL_LIST_FILE, 'r') as f:
        video_pairs = [line.strip().split('|') for line in f.readlines()]

    # Apply test mode to downloading
    download_all(video_pairs, TEST_MODE)


if __name__ == '__main__':
    main()
