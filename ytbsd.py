"""
YT Bulk Subtitles Downloader (YTBSD)
CLI wizard to download subtitles from single videos, playlists, or entire channels.
Output in markdown format (for LLM RAG) or SRT format (subtitle files).
Features: Multi-threading, proxy rotation, progress saving, resume capability.
"""

import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# Platform-specific keyboard input
if sys.platform == 'win32':
    import msvcrt
else:
    import tty
    import termios

try:
    import yt_dlp
except ImportError:
    print("Error: yt-dlp is not installed. Install it with: pip install yt-dlp")
    sys.exit(1)

try:
    from youtube_transcript_api import (
        YouTubeTranscriptApi,
        NoTranscriptFound,
        TranscriptsDisabled,
        CouldNotRetrieveTranscript,
    )
    from youtube_transcript_api.proxies import GenericProxyConfig
    from youtube_transcript_api.formatters import SRTFormatter
except ImportError:
    print("Error: youtube-transcript-api is not installed. Install it with: pip install youtube-transcript-api")
    sys.exit(1)

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.service import Service as ChromeService
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    print("Error: selenium or webdriver-manager is not installed. Install with: pip install selenium webdriver-manager")
    sys.exit(1)

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
except ImportError:
    print("Error: rich is not installed. Install it with: pip install rich")
    sys.exit(1)


# Custom exception for when video has no transcript (don't retry with different proxy)
class NoTranscriptError(Exception):
    """Video has no transcript available - not a proxy issue."""
    pass


# ============================================================================
# Configuration - Paths (needed before functions that use them)
# ============================================================================

# Paths - detect if running as PyInstaller exe
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROGRESS_DIR = os.path.join(SCRIPT_DIR, "subtitles")
PROGRESS_FILE = os.path.join(SCRIPT_DIR, ".progress.json")
PROXY_FILE = os.path.join(SCRIPT_DIR, "proxies.txt")


# ============================================================================
# Proxy Scraping
# ============================================================================

def download_fresh_proxies(proxy_file: str = PROXY_FILE) -> int:
    """
    Download fresh proxy list from free-proxy-list.net and save to file.
    Returns the number of proxies downloaded.
    """
    print("\nDownloading fresh proxy list...")

    driver = None
    try:
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--log-level=3")  # Suppress Chrome logs
        driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)

        url = "https://free-proxy-list.net/en/"
        driver.get(url)

        wait = WebDriverWait(driver, 10)
        button_selector = 'a[title="Get raw list"]'
        button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, button_selector)))
        button.click()

        textarea_selector = 'textarea.form-control'
        textarea = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, textarea_selector)))

        content = textarea.get_attribute("value")
        proxies = re.findall(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+', content)
        clean_content = "\n".join(proxies)

        with open(proxy_file, "w", encoding="utf-8") as f:
            f.write(clean_content)

        proxy_count = len(proxies)
        print(f"✓ Downloaded {proxy_count} proxies\n")

        return proxy_count

    except Exception as e:
        print(f"⚠ Failed to download proxies: {e}\n")
        return 0
    finally:
        if driver:
            driver.quit()


# ============================================================================
# Configuration
# ============================================================================

# Threading settings (reduced to avoid rate limiting)
DEFAULT_THREADS = 2
MAX_THREADS = 1000
MIN_THREADS = 1
THREAD_DELAY_MIN = 1.5
THREAD_DELAY_MAX = 3.0

# Proxy settings
MAX_PROXY_RETRIES = 20  # Reduced to avoid long waits
PROXY_TIMEOUT = 15  # Timeout for proxy validation

# Video fetch timeout (seconds) - if a video takes longer, skip it
VIDEO_FETCH_TIMEOUT = 15

# Video info extraction timeout (seconds) - timeout for fetching playlist/channel info via proxy
VIDEO_INFO_TIMEOUT = 15

# Availability check timeout (seconds) - timeout for checking if transcript is available via proxy
AVAILABILITY_CHECK_TIMEOUT = 15


# ============================================================================
# Proxy Pool (Thread-safe)
# ============================================================================

class ProxyPool:
    """Thread-safe proxy pool with rotation and failure tracking."""

    def __init__(self, proxy_file: str = PROXY_FILE, validate: bool = False):
        self.proxies = []
        self.failed_proxies = set()
        self.lock = threading.Lock()
        self.index = 0
        self._load_proxies_from_file(proxy_file)
        # Shuffle proxies to distribute load
        if self.proxies:
            random.shuffle(self.proxies)

    def _load_proxies_from_file(self, proxy_file: str):
        """Load proxies from file."""
        if not os.path.exists(proxy_file):
            print(f"Warning: Proxy file not found: {proxy_file}")
            return

        with open(proxy_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    if ':' in line:
                        self.proxies.append(line)

        print(f"Loaded {len(self.proxies)} proxies from {os.path.basename(proxy_file)}")

    def _validate_proxies(self, test_count: int = 10, timeout: int = 5):
        """Quick validation of a sample of proxies."""
        import requests

        if len(self.proxies) == 0:
            return

        print(f"Testing {min(test_count, len(self.proxies))} proxies...")
        test_proxies = random.sample(self.proxies, min(test_count, len(self.proxies)))
        working = 0

        for proxy in test_proxies:
            try:
                proxy_dict = {'http': f'http://{proxy}', 'https': f'http://{proxy}'}
                resp = requests.get('https://www.youtube.com', proxies=proxy_dict, timeout=timeout)
                if resp.status_code == 200:
                    working += 1
                    print(f"  {proxy} - OK")
                else:
                    self.failed_proxies.add(proxy)
            except Exception:
                self.failed_proxies.add(proxy)

        if working == 0:
            print(f"WARNING: No working proxies found in sample! Will use direct connection.")
        else:
            print(f"Proxy test: {working}/{len(test_proxies)} working")

    def get_proxy(self) -> str | None:
        """Get next working proxy (round-robin)."""
        with self.lock:
            if not self.proxies:
                return None

            # Try to find a working proxy
            attempts = 0
            while attempts < len(self.proxies):
                proxy = self.proxies[self.index % len(self.proxies)]
                self.index += 1
                if proxy not in self.failed_proxies:
                    return proxy
                attempts += 1

            # All proxies have failed - return None (don't auto-reset)
            return None

    def get_random_proxy(self, exclude: set = None) -> str | None:
        """Get a random proxy not in exclude set and not globally failed."""
        with self.lock:
            available = [p for p in self.proxies
                         if p not in self.failed_proxies
                         and (exclude is None or p not in exclude)]
            return random.choice(available) if available else None

    def get_available_count(self) -> int:
        """Return count of non-failed proxies."""
        with self.lock:
            return len(self.proxies) - len(self.failed_proxies)

    def reload_proxies(self, proxy_file: str = None):
        """Reload proxies from file (replacing existing) and clear failed set."""
        with self.lock:
            if proxy_file:
                # Clear existing proxies before loading new ones
                self.proxies = []
                self._load_proxies_from_file(proxy_file)
            self.failed_proxies.clear()
            self.index = 0
            if self.proxies:
                random.shuffle(self.proxies)

    def mark_failed(self, proxy: str):
        """Mark a proxy as failed."""
        with self.lock:
            self.failed_proxies.add(proxy)

    def get_stats(self) -> tuple[int, int]:
        """Return (total, failed) proxy counts."""
        with self.lock:
            return len(self.proxies), len(self.failed_proxies)

    def has_proxies(self) -> bool:
        """Check if any proxies are available."""
        return len(self.proxies) > 0


# ============================================================================
# Video Work Queue (Collaborative Multi-Proxy Download)
# ============================================================================

class VideoWorkQueue:
    """
    Thread-safe work queue that allows multiple threads to work on the same video
    with different proxies simultaneously. Implements collaborative work-stealing pattern.
    """

    MAX_PROXY_REFRESHES = 3
    NO_TRANSCRIPT_VOTES_REQUIRED = 2

    def __init__(self, videos: list[dict], proxy_pool: ProxyPool, status_callback=None):
        self.videos = {v['id']: v for v in videos}  # Map id -> video dict
        self.video_list = list(videos)  # Keep order for iteration
        self.proxy_pool = proxy_pool
        self.status_callback = status_callback
        self.lock = threading.Lock()
        self.proxy_refresh_count = 0
        self.refresh_in_progress = False  # Prevent simultaneous refreshes

        # Video states: 'pending', 'in_progress', 'completed', 'no_transcript', 'failed'
        self.video_states = {v['id']: 'pending' for v in videos}

        # Track which proxies have been tried for each video (per-video, not global)
        self.video_proxy_attempts = {v['id']: set() for v in videos}

        # Track "no transcript" votes (need 2 confirmations)
        self.no_transcript_votes = {v['id']: 0 for v in videos}

        # Track active workers per video
        self.active_workers = {v['id']: 0 for v in videos}

        # Results storage
        self.results = {}

        # Statistics
        self.completed_count = 0
        self.success_count = 0
        self.no_transcript_count = 0
        self.failed_count = 0

        # Track total proxy attempts for logging
        self.total_proxy_attempts = 0

    def get_work(self) -> tuple[dict, str] | None:
        """
        Get a video and proxy to work on.

        Strategy:
        1. First, try to get a 'pending' video (not started by anyone)
        2. If none, join an 'in_progress' video with a new proxy
        3. If all proxies tried for all videos, refresh proxy list
        4. Return None if all videos completed or no proxies left after 3 refreshes

        NOTE: We do NOT globally blacklist proxies. A proxy that fails on video A
        might work on video B. We only track which proxies have been tried per-video.
        """
        should_retry = False

        with self.lock:
            all_proxies = set(self.proxy_pool.proxies)  # All available proxies

            # Strategy 1: Find a pending video
            for video in self.video_list:
                vid = video['id']
                if self.video_states[vid] == 'pending':
                    # Get a random proxy not yet tried for this video
                    available = all_proxies - self.video_proxy_attempts[vid]
                    if available:
                        proxy = random.choice(list(available))
                        self.video_states[vid] = 'in_progress'
                        self.video_proxy_attempts[vid].add(proxy)
                        self.active_workers[vid] += 1
                        self.total_proxy_attempts += 1
                        return (video, proxy)

            # Strategy 2: Join an in_progress video with a different proxy
            # Prefer videos with fewer active workers AND fewer attempted proxies
            in_progress = []
            for vid, state in self.video_states.items():
                if state == 'in_progress':
                    available_count = len(all_proxies - self.video_proxy_attempts[vid])
                    if available_count > 0:
                        in_progress.append((vid, self.active_workers[vid], available_count))

            # Sort by: fewest workers first, then most available proxies
            in_progress.sort(key=lambda x: (x[1], -x[2]))

            for vid, _, _ in in_progress:
                available = all_proxies - self.video_proxy_attempts[vid]
                if available:
                    proxy = random.choice(list(available))
                    self.video_proxy_attempts[vid].add(proxy)
                    self.active_workers[vid] += 1
                    self.total_proxy_attempts += 1
                    return (self.videos[vid], proxy)

            # Strategy 3: Check if we need to refresh proxies
            # All in-progress videos have exhausted all proxies
            pending_or_in_progress = [vid for vid, state in self.video_states.items()
                                       if state in ('pending', 'in_progress')]

            if not pending_or_in_progress:
                # All videos completed/failed/no_transcript
                return None

            # Check if ANY video still has untried proxies
            any_available = False
            for vid in pending_or_in_progress:
                if len(all_proxies - self.video_proxy_attempts[vid]) > 0:
                    any_available = True
                    break

            if any_available:
                # Shouldn't reach here, but retry just in case
                should_retry = True
            elif self.refresh_in_progress:
                # Another thread is refreshing, wait and retry
                self.lock.release()
                time.sleep(0.5)
                self.lock.acquire()
                should_retry = True
            else:
                # All proxies exhausted for all remaining videos - try to refresh
                if self._refresh_proxies_internal():
                    # Proxies refreshed successfully
                    should_retry = True
                else:
                    # Max refreshes reached - mark remaining as failed
                    for vid in pending_or_in_progress:
                        if self.video_states[vid] not in ('completed', 'no_transcript'):
                            self.video_states[vid] = 'failed'
                            self.failed_count += 1
                            self.completed_count += 1
                    return None

        # Retry after refresh (outside lock)
        if should_retry:
            # Check if all work was completed while we were waiting
            if self.is_all_work_done():
                return None
            return self.get_work()

        return None

    def _refresh_proxies_internal(self) -> bool:
        """
        Download fresh proxy list. Returns True if successful.
        Must be called with lock held.
        """
        if self.proxy_refresh_count >= self.MAX_PROXY_REFRESHES:
            return False

        if self.refresh_in_progress:
            return False  # Another thread is already refreshing

        self.refresh_in_progress = True
        self.proxy_refresh_count += 1
        refresh_num = self.proxy_refresh_count

        if self.status_callback:
            self.status_callback(f"Refreshing proxy list (attempt {refresh_num}/{self.MAX_PROXY_REFRESHES})...")

        # Release lock during download (I/O operation)
        self.lock.release()
        try:
            print(f"\n*** Refreshing proxy list (attempt {refresh_num}/{self.MAX_PROXY_REFRESHES}) ***")
            count = download_fresh_proxies(PROXY_FILE)
        finally:
            self.lock.acquire()
            self.refresh_in_progress = False

        if count > 0:
            self.proxy_pool.reload_proxies(PROXY_FILE)
            # Clear per-video proxy attempts so new proxies can be tried
            for vid in self.video_proxy_attempts:
                self.video_proxy_attempts[vid].clear()
            if self.status_callback:
                self.status_callback(f"Loaded {count} fresh proxies")
            print(f"*** Loaded {count} fresh proxies, cleared per-video attempt history ***")
            return True

        return False

    def mark_completed(self, video_id: str, result: dict):
        """Mark video as successfully completed."""
        with self.lock:
            if self.video_states[video_id] not in ('completed', 'no_transcript', 'failed'):
                self.video_states[video_id] = 'completed'
                self.results[video_id] = result
                self.completed_count += 1
                self.success_count += 1

    def mark_no_transcript(self, video_id: str, result: dict):
        """
        Vote that video has no transcript.
        After 2 votes from different proxies, mark as definitively no transcript.
        """
        with self.lock:
            if self.video_states[video_id] in ('completed', 'no_transcript', 'failed'):
                return  # Already finalized

            self.no_transcript_votes[video_id] += 1

            if self.no_transcript_votes[video_id] >= self.NO_TRANSCRIPT_VOTES_REQUIRED:
                self.video_states[video_id] = 'no_transcript'
                self.results[video_id] = result
                self.completed_count += 1
                self.no_transcript_count += 1

    def mark_proxy_failed(self, video_id: str, proxy: str):
        """
        Note: We intentionally do NOT globally blacklist proxies.
        A proxy that fails on video A might work on video B.
        The per-video tracking in video_proxy_attempts is sufficient.
        This method exists for interface compatibility.
        """
        # Don't globally blacklist - proxy might work for other videos
        pass

    def is_video_done(self, video_id: str) -> bool:
        """Check if video was completed/no_transcript/failed by another thread."""
        with self.lock:
            return self.video_states[video_id] in ('completed', 'no_transcript', 'failed')

    def release_work(self, video_id: str):
        """Called when a thread stops working on a video."""
        with self.lock:
            self.active_workers[video_id] = max(0, self.active_workers[video_id] - 1)

    def get_progress(self) -> tuple[int, int, int, int, int]:
        """Return (completed, success, no_transcript, failed, total)."""
        with self.lock:
            return (self.completed_count, self.success_count, self.no_transcript_count,
                    self.failed_count, len(self.videos))

    def is_all_work_done(self) -> bool:
        """Check if all videos are completed (success, no_transcript, or failed)."""
        with self.lock:
            return self.completed_count >= len(self.videos)

    def get_all_results(self) -> list[dict]:
        """Get all results as a list."""
        with self.lock:
            results = []
            for video in self.video_list:
                vid = video['id']
                if vid in self.results:
                    results.append(self.results[vid])
                else:
                    # Video wasn't completed - create error result
                    results.append({
                        'id': vid,
                        'title': video['title'],
                        'transcript': None,
                        'language': None,
                        'method': 'failed',
                        'error_detail': f"Failed after {self.proxy_refresh_count} proxy refresh cycles"
                    })
            return results


# ============================================================================
# Utility Functions
# ============================================================================

def sanitize_filename(name: str) -> str:
    """Remove invalid characters from filename."""
    sanitized = re.sub(r'[<>:"/\\|?*]', '', name)
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    return sanitized[:100] if len(sanitized) > 100 else sanitized


def extract_video_id(url: str) -> str:
    """Extract video ID from various YouTube URL formats."""
    patterns = [
        r'(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return url


def extract_video_info_with_timeout(url: str, ydl_opts: dict, timeout: int = VIDEO_INFO_TIMEOUT):
    """
    Extract video info using yt-dlp with a timeout.
    Returns info dict or raises exception.
    DOES NOT BLOCK if the underlying extraction hangs.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(_extract)
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        executor.shutdown(wait=False, cancel_futures=True)
        raise TimeoutError(f"Video info extraction timed out after {timeout}s")
    except Exception:
        executor.shutdown(wait=False)
        raise


def get_video_ids_from_url(url: str, mode: str, proxy_pool: ProxyPool = None) -> tuple[list[dict], str, str]:
    """Extract video information from URL using yt-dlp with proxy support.
    Tries direct connection first, then falls back to proxies if needed."""
    console = Console()
    videos = []
    source_name = ""
    source_type = mode
    
    use_proxies = proxy_pool is not None and proxy_pool.has_proxies()
    max_proxy_attempts = 200 if use_proxies else 0
    
    # Create a status text that will update in place
    status_text = Text("Fetching video info...", style="yellow")
    
    with Live(status_text, refresh_per_second=4, console=console):
        # Step 1: Try direct connection first
        status_text.plain = "Fetching video info... Attempting direct connection..."
        
        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'no_warnings': True,
        }
        
        try:
            # Try direct connection first
            info = extract_video_info_with_timeout(url, ydl_opts, timeout=VIDEO_INFO_TIMEOUT)

            if mode == "single":
                videos = [{'id': info.get('id'), 'title': info.get('title', 'Unknown')}]
                source_name = sanitize_filename(info.get('title', 'video'))
            else:
                source_name = sanitize_filename(info.get('title', info.get('uploader', 'unknown')))
                entries = info.get('entries', [])

                for entry in entries:
                    if entry:
                        videos.append({
                            'id': entry.get('id'),
                            'title': entry.get('title', 'Unknown')
                        })

            # Success with direct connection
            status_text.plain = "✓ Successfully fetched (direct connection)"
            time.sleep(0.5)  # Brief pause to show success message
            return videos, source_name, source_type
            
        except (TimeoutError, Exception) as e:
            # Direct connection failed - try proxies if available
            if not use_proxies:
                # No proxies available and direct failed - raise error
                error_msg = str(e).split('\n')[0][:50]
                status_text.plain = f"✗ Direct connection failed: {error_msg}..."
                time.sleep(0.5)
                raise
            
            # Fall back to proxies
            error_msg = str(e).split('\n')[0][:50] if str(e) else "unknown error"
            status_text.plain = f"✗ Direct connection failed ({error_msg[:30]}...), trying proxies..."
            time.sleep(0.5)
        
        # Step 2: Try proxies if direct connection failed
        proxy_attempts_made = 0
        
        while proxy_attempts_made < max_proxy_attempts:
            current_proxy = proxy_pool.get_proxy()
            if not current_proxy:
                break
            
            proxy_ip = current_proxy.split(':')[0]
            status_text.plain = f"Fetching video info... Attempt {proxy_attempts_made + 1}: Trying proxy {proxy_ip}..."
            
            ydl_opts = {
                'extract_flat': True,
                'quiet': True,
                'no_warnings': True,
                'proxy': f"http://{current_proxy}",
            }
            
            try:
                # Use timeout wrapper to prevent hanging on slow/bad proxies
                info = extract_video_info_with_timeout(url, ydl_opts, timeout=VIDEO_INFO_TIMEOUT)

                if mode == "single":
                    videos = [{'id': info.get('id'), 'title': info.get('title', 'Unknown')}]
                    source_name = sanitize_filename(info.get('title', 'video'))
                else:
                    source_name = sanitize_filename(info.get('title', info.get('uploader', 'unknown')))
                    entries = info.get('entries', [])

                    for entry in entries:
                        if entry:
                            videos.append({
                                'id': entry.get('id'),
                                'title': entry.get('title', 'Unknown')
                            })

                # Success with proxy
                status_text.plain = f"✓ Successfully fetched via proxy: {proxy_ip}"
                time.sleep(0.5)  # Brief pause to show success message
                return videos, source_name, source_type
                
            except TimeoutError:
                # Timeout - mark proxy as failed and try next
                proxy_pool.mark_failed(current_proxy)
                status_text.plain = f"✗ Proxy {proxy_ip} timed out after {VIDEO_INFO_TIMEOUT}s (attempt {proxy_attempts_made + 1})"
            except Exception as e:
                # Mark proxy as failed and try next
                proxy_pool.mark_failed(current_proxy)
                error_msg = str(e).split('\n')[0][:50]  # Get first line, truncate
                status_text.plain = f"✗ Proxy {proxy_ip} failed: {error_msg}... (attempt {proxy_attempts_made + 1})"
            
            proxy_attempts_made += 1
        
        # All attempts exhausted
        status_text.plain = f"✗ Failed to fetch video info: Direct failed, {proxy_attempts_made} proxy attempts failed"
        time.sleep(0.5)
        raise Exception(f"Failed to fetch video info: Direct connection failed, and {proxy_attempts_made} proxy attempts failed")


# ============================================================================
# Transcript Fetching
# ============================================================================

def check_transcript_availability(video_id: str, proxy: str = None) -> tuple:
    """
    Check what transcripts are available for a video.
    Returns (transcript_obj, language_info) or raises NoTranscriptError.
    """
    if proxy:
        proxy_url = f"http://{proxy}"
        proxy_config = GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
        ytt_api = YouTubeTranscriptApi(proxy_config=proxy_config)
    else:
        ytt_api = YouTubeTranscriptApi()

    try:
        transcript_list = ytt_api.list(video_id)

        # Try English first
        try:
            transcript = transcript_list.find_transcript(["en"])
            lang_type = "auto-generated" if transcript.is_generated else "manual"
            return (transcript, f"English ({lang_type})")
        except NoTranscriptFound:
            pass

        # Try translatable transcript
        for available_transcript in transcript_list:
            if available_transcript.is_translatable:
                translation_codes = [
                    lang.get('language_code', '')
                    for lang in available_transcript.translation_languages
                ]
                if 'en' in translation_codes:
                    translated = available_transcript.translate('en')
                    return (translated, f"Translated from {available_transcript.language}")

        # Fall back to any available
        available = list(transcript_list)
        if available:
            return (available[0], f"{available[0].language} (no English available)")

        raise NoTranscriptError("No transcript available in any language")

    except TranscriptsDisabled:
        raise NoTranscriptError("Transcripts are disabled for this video")
    except NoTranscriptFound:
        raise NoTranscriptError("No transcript found for this video")






# ============================================================================
# Progress Management (Thread-safe)
# ============================================================================

progress_lock = threading.Lock()


def save_progress(progress_data: dict):
    """Save progress to file (thread-safe)."""
    with progress_lock:
        os.makedirs(PROGRESS_DIR, exist_ok=True)
        # Create a copy to avoid modifying original data
        data_to_save = dict(progress_data)
        # Strip non-serializable fields from videos_data (e.g., FetchedTranscript objects)
        if 'videos_data' in data_to_save:
            data_to_save['videos_data'] = [
                {k: v for k, v in video.items() if k != 'fetched_transcript'}
                for video in data_to_save['videos_data']
            ]
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, indent=2, ensure_ascii=False)


def load_progress() -> dict | None:
    """Load progress from file if exists."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def clear_progress():
    """Delete progress file."""
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)


# ============================================================================
# Markdown Output
# ============================================================================

def create_markdown_output(videos_data: list[dict], source_name: str, source_type: str) -> str:
    """Create markdown formatted output optimized for LLM RAG."""
    lines = []

    lines.append(f"# {source_name}")
    lines.append("")
    lines.append(f"**Source Type:** {source_type.title()}")
    lines.append(f"**Total Videos:** {len(videos_data)}")
    videos_with_transcript = sum(1 for v in videos_data if v.get('transcript'))
    lines.append(f"**Videos with Transcripts:** {videos_with_transcript}")
    lines.append(f"**Downloaded:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("---")
    lines.append("")

    lines.append("## Table of Contents")
    lines.append("")
    for i, video in enumerate(videos_data, 1):
        if video.get('transcript'):
            anchor = re.sub(r'[^a-z0-9\s-]', '', video['title'].lower())
            anchor = re.sub(r'\s+', '-', anchor)
            lines.append(f"{i}. [{video['title']}](#{anchor})")
    lines.append("")
    lines.append("---")
    lines.append("")

    for i, video in enumerate(videos_data, 1):
        lines.append(f"## {i}. {video['title']}")
        lines.append("")
        lines.append(f"**Video ID:** {video['id']}")
        lines.append(f"**URL:** https://www.youtube.com/watch?v={video['id']}")
        if video.get('language'):
            lines.append(f"**Transcript Language:** {video['language']}")
        lines.append("")

        if video.get('transcript'):
            lines.append("### Transcript")
            lines.append("")
            lines.append(video['transcript'])
        else:
            lines.append("*No transcript available for this video.*")

        lines.append("")
        lines.append("---")
        lines.append("")

    return '\n'.join(lines)


def count_words(text: str) -> int:
    """Count words in text."""
    return len(text.split())


def create_split_markdown_output(videos_data: list[dict], source_name: str,
                                  source_type: str, max_words: int) -> list[tuple[str, str]]:
    """
    Create split markdown files based on word limit.
    Returns list of (filename, content) tuples.
    Never splits in the middle of a video transcript.
    """
    files = []
    current_videos = []
    current_word_count = 0
    part_number = 1

    for video in videos_data:
        # Calculate words in this video's transcript
        video_words = 0
        if video.get('transcript'):
            video_words = count_words(video['transcript'])

        # Check if adding this video would exceed limit
        # (but always include at least one video per file)
        if current_videos and (current_word_count + video_words) > max_words:
            # Save current batch
            content = create_markdown_output(current_videos, source_name, source_type)
            filename = f"{source_name}_part{part_number}"
            files.append((filename, content))

            # Start new batch
            part_number += 1
            current_videos = [video]
            current_word_count = video_words
        else:
            current_videos.append(video)
            current_word_count += video_words

    # Save remaining videos
    if current_videos:
        content = create_markdown_output(current_videos, source_name, source_type)
        filename = f"{source_name}_part{part_number}"
        files.append((filename, content))

    return files


def create_srt_output(videos_data: list[dict], source_name: str, output_dir: str) -> list[str]:
    """
    Create SRT files for each video with transcripts.
    Creates a folder with channel/playlist name and saves each video as separate .srt file.
    Returns list of created file paths.
    """
    formatter = SRTFormatter()
    filepaths = []

    # Create folder with source name
    folder_name = sanitize_filename(source_name)
    srt_dir = os.path.join(output_dir, folder_name)
    os.makedirs(srt_dir, exist_ok=True)

    for video in videos_data:
        # Skip videos without transcripts or without raw data
        if not video.get('fetched_transcript'):
            continue

        # Create filename from video title
        video_title = sanitize_filename(video['title'])
        filename = f"{video_title}.srt"
        filepath = os.path.join(srt_dir, filename)

        # Format transcript as SRT
        try:
            srt_content = formatter.format_transcript(video['fetched_transcript'])
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(srt_content)
            filepaths.append(filepath)
        except Exception as e:
            print(f"Warning: Could not create SRT for '{video['title']}': {e}")

    return filepaths


# ============================================================================
# Thread Status Display Manager
# ============================================================================

class ThreadStatusManager:
    """Manages thread status display using rich for static console updates."""

    MAX_VISIBLE_THREADS = 20  # Limit visible thread rows to fit screen

    def __init__(self, total_threads: int, total_videos: int):
        self.total_threads = total_threads
        self.total_videos = total_videos
        self.statuses = {}
        self.lock = threading.Lock()
        self.console = Console()
        self.live = None
        # Progress tracking
        self.completed = 0
        self.success = 0
        self.failed = 0
        self.no_transcript = 0

    def update_status(self, thread_id: int, status: str):
        """Update status for a specific thread."""
        with self.lock:
            self.statuses[thread_id] = status

    def update_progress(self, completed: int, success: int, failed: int = 0, no_transcript: int = 0):
        """Update overall progress counters."""
        with self.lock:
            self.completed = completed
            self.success = success
            self.failed = failed
            self.no_transcript = no_transcript

    def __rich__(self) -> Group:
        """Make this class a rich renderable - called on each Live refresh."""
        with self.lock:
            completed = self.completed
            success = self.success
            failed = self.failed
            no_transcript = self.no_transcript
            total = self.total_videos
            remaining = total - completed

            # Progress summary panel
            if total > 0:
                pct = (completed / total) * 100
            else:
                pct = 0

            progress_bar = f"[{'█' * int(pct // 2.5)}{' ' * (40 - int(pct // 2.5))}]"
            progress_text = (
                f"Progress: {progress_bar} {pct:.1f}%\n"
                f"Completed: [green]{completed}[/green]/{total}  |  "
                f"Success: [green]{success}[/green]  |  "
                f"No Transcript: [yellow]{no_transcript}[/yellow]  |  "
                f"Failed: [red]{failed}[/red]  |  "
                f"Remaining: [cyan]{remaining}[/cyan]"
            )
            summary_panel = Panel(progress_text, title="Download Progress", border_style="green")

            # Thread status table - only show active threads (not waiting, limit count)
            table = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta")
            table.add_column("Thread", style="cyan", width=8)
            table.add_column("Status", style="yellow", width=80)

            # Filter to only show threads with actual activity (not "Waiting...")
            active_threads = []
            for thread_id in range(self.total_threads):
                status = self.statuses.get(thread_id, "Waiting...")
                if status != "Waiting...":
                    active_threads.append((thread_id, status))

            # Show up to MAX_VISIBLE_THREADS most recent active threads
            visible_threads = active_threads[-self.MAX_VISIBLE_THREADS:]

            for thread_id, status in visible_threads:
                table.add_row(f"#{thread_id+1}", status)

            if len(active_threads) > self.MAX_VISIBLE_THREADS:
                table.add_row("...", f"[dim]({len(active_threads) - self.MAX_VISIBLE_THREADS} more threads active)[/dim]")

            if not visible_threads:
                table.add_row("-", "Initializing...")

        return Group(summary_panel, table)

    def get_live_context(self):
        """Return a Live context manager for use in the main thread."""
        # Pass self as renderable - Live will call __rich__() on each refresh
        self.live = Live(self, console=self.console, refresh_per_second=4)
        return self.live

    def stop_display(self):
        """Stop the live display."""
        self.live = None


# ============================================================================
# Parallel Download (Multi-threaded Mode)
# ============================================================================

def fetch_content_with_timeout(transcript_obj, timeout: int = VIDEO_FETCH_TIMEOUT):
    """
    Fetch transcript content with a timeout.
    Returns (text, fetched_transcript) or raises exception.
    DOES NOT BLOCK if the underlying thread hangs.

    The fetched_transcript is the raw FetchedTranscript object with timestamps,
    which can be used with formatters like SRTFormatter.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

    def _fetch():
        fetched = transcript_obj.fetch()
        text = ' '.join(snippet.text for snippet in fetched)
        return text, fetched

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(_fetch)
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        # shutdown(wait=False) prevents waiting for the stuck thread
        executor.shutdown(wait=False, cancel_futures=True)
        raise TimeoutError(f"Fetch timed out after {timeout}s")
    except Exception:
        executor.shutdown(wait=False)
        raise
    finally:
        # If we succeeded, we can wait (it's instant)
        # If we failed/timed out, we already called shutdown(wait=False) above or don't care
        pass


def check_availability_with_timeout(video_id: str, proxy: str = None, timeout: int = 10):
    """
    Check transcript availability with a timeout.
    DOES NOT BLOCK if the underlying thread hangs.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(check_transcript_availability, video_id, proxy)
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        executor.shutdown(wait=False, cancel_futures=True)
        raise TimeoutError(f"Availability check timed out after {timeout}s")
    except Exception:
        executor.shutdown(wait=False)
        raise




def download_single_video_with_proxy(video: dict, proxy: str, status_callback=None) -> dict:
    """
    Try to download a video transcript using ONE specific proxy.
    Returns result dict with success/failure info.

    This is used by the collaborative work queue where each thread
    tries a different proxy for the same video.
    """
    video_id = video['id']
    video_title = video['title']
    result_data = {
        'id': video_id,
        'title': video_title,
        'transcript': None,
        'language': None,
        'method': None,
        'error_detail': None
    }

    proxy_ip = proxy.split(':')[0] if proxy else "Direct"
    strategy_name = f"Proxy:{proxy_ip}" if proxy else "Direct"

    if status_callback:
        status_callback(f"Checking via {proxy_ip}...")

    try:
        # Step 1: Check Availability
        try:
            transcript_obj, lang_info = check_availability_with_timeout(
                video_id, proxy=proxy, timeout=AVAILABILITY_CHECK_TIMEOUT
            )
            if status_callback:
                status_callback(f"✓ Found: {lang_info}")
        except TimeoutError:
            raise Exception("Availability check timed out")
        except NoTranscriptError:
            # Definitive - no transcript exists
            result_data['method'] = "no_transcript"
            if status_callback:
                status_callback("No transcript available")
            return result_data

        # Step 2: Download Content
        try:
            if status_callback:
                status_callback("Downloading content...")
            text, fetched_transcript = fetch_content_with_timeout(transcript_obj, timeout=VIDEO_FETCH_TIMEOUT)
            if status_callback:
                status_callback(f"✓ OK ({len(text)} chars) via {proxy_ip}")

            # Success!
            result_data['transcript'] = text
            result_data['fetched_transcript'] = fetched_transcript  # Raw data for SRT
            result_data['language'] = lang_info
            result_data['method'] = strategy_name.lower()
            return result_data

        except Exception as e:
            raise Exception(f"Download failed: {e}")

    except Exception as e:
        error_msg = str(e)
        if status_callback:
            status_callback(f"✗ Failed via {proxy_ip}: {error_msg[:40]}")
        result_data['method'] = "error"
        result_data['error_detail'] = f"{strategy_name} error: {error_msg}"
        return result_data


def download_single_video(video: dict, proxy_pool: ProxyPool, status_callback=None, debug: bool = False) -> dict:
    """
    Download transcript for a single video using smart proxy rotation.

    Strategy:
    1. Try direct connection first (check + download).
    2. If direct fails (network/geo block), rotate through proxies.
    3. Ensure the SAME proxy is used for both checking and downloading.

    NOTE: This function is kept for backward compatibility.
    The new collaborative system uses download_single_video_with_proxy instead.
    """
    video_id = video['id']
    video_title = video['title']
    result_data = {
        'id': video_id,
        'title': video_title,
        'transcript': None,
        'language': None,
        'method': None,
        'error_detail': None
    }

    # Strategy list: If proxies exist, use them immediately and skip direct connection
    # This prevents blocking on IP bans/timeouts when user knows they are blocked
    use_proxies_only = proxy_pool.has_proxies()
    
    max_proxy_attempts = 20
    attempts_made = 0
    
    # If using proxies, we loop through them. If not, we try Direct once.
    # Increased to 50 to handle lists with many dead proxies
    total_attempts = 200 if use_proxies_only else 1

    while attempts_made < total_attempts:
        current_proxy = None
        strategy_name = "Direct"

        if use_proxies_only:
            current_proxy = proxy_pool.get_proxy()
            if not current_proxy:
                break
            strategy_name = f"Proxy:{current_proxy.split(':')[0]}"
        elif attempts_made > 0:
             # Should not happen if use_proxies_only is False (max attempts = 1)
             break

        # Update status for progress tracking
        proxy_ip = current_proxy.split(':')[0] if current_proxy else "Direct"
        if status_callback:
            status_callback(f"Attempt {attempts_made+1}: Checking via {proxy_ip}...")

        try:
            # --- Step 1: Check Availability ---
            # We use a timeout for availability checks to fail fast on bad proxies
            try:
                # Use query-based check with timeout (supports both direct and proxy)
                transcript_obj, lang_info = check_availability_with_timeout(video_id, proxy=current_proxy, timeout=AVAILABILITY_CHECK_TIMEOUT)
                if status_callback:
                    status_callback(f"✓ Transcript found: {lang_info}")

                    
            except TimeoutError:
                raise Exception("Availability check timed out")
            except NoTranscriptError:
                # This is definitive - no transcript exists. Don't retry with other proxies.
                result_data['method'] = "no_transcript"
                if status_callback:
                    status_callback("No transcript available")
                return result_data
            
            # --- Step 2: Download Content ---
            try:
                # Fetch content with timeout protection
                if status_callback:
                    status_callback("Downloading transcript content...")
                text, fetched_transcript = fetch_content_with_timeout(transcript_obj, timeout=VIDEO_FETCH_TIMEOUT)
                if status_callback:
                    status_callback(f"✓ Downloaded ({len(text)} chars) via {proxy_ip}")

                # Success!
                result_data['transcript'] = text
                result_data['fetched_transcript'] = fetched_transcript  # Raw data for SRT
                result_data['language'] = lang_info
                result_data['method'] = strategy_name.lower()
                return result_data

            except Exception as e:
                raise Exception(f"Download failed: {e}")

        except Exception as e:
            # If it's a proxy error, mark it failed
            if current_proxy:
                proxy_pool.mark_failed(current_proxy)
            
            error_msg = str(e)
            if status_callback:
                status_callback(f"✗ Failed via {proxy_ip}: {error_msg[:50]}")
            
            # Keep the error detail from the last attempt
            result_data['error_detail'] = f"{strategy_name} error: {error_msg}"
            
        attempts_made += 1

    # If we get here, all attempts failed
    result_data['method'] = "error"
    if not result_data['error_detail']:
        result_data['error_detail'] = "All connection attempts exhausted"
    else:
        # Prepend attempt count to clarify it wasn't just one try
        result_data['error_detail'] = f"Failed after {attempts_made} attempts. Last: {result_data['error_detail']}"
        
    return result_data


def download_transcripts_parallel(videos: list[dict], source_name: str, source_type: str,
                                   num_threads: int = DEFAULT_THREADS,
                                   start_index: int = 0, existing_data: list[dict] = None,
                                   completed_ids: set = None, output_config: dict = None):
    """
    Download transcripts in parallel using collaborative multi-proxy pattern.

    Key features:
    - Multiple threads can work on the SAME video with DIFFERENT proxies
    - When any thread completes a video, others working on it move on
    - Failed proxies are blacklisted globally
    - Auto-refreshes proxy list when all proxies exhausted (up to 3 times)
    """
    videos_data = existing_data if existing_data else []
    completed_ids = completed_ids if completed_ids else set(v['id'] for v in videos_data)
    total = len(videos)
    was_interrupted = False

    # Filter out already completed videos
    remaining_videos = [v for v in videos if v['id'] not in completed_ids]

    if not remaining_videos:
        print("All videos already processed!")
        return videos_data, sum(1 for v in videos_data if v.get('transcript')), False

    # Initialize proxy pool
    proxy_pool = ProxyPool(PROXY_FILE, validate=False)

    # NOTE: We no longer cap threads to remaining videos!
    # Multiple threads can now collaborate on the same video with different proxies
    print(f"\nDownloading transcripts for {total} video(s) [Collaborative Multi-Proxy Mode]")
    print(f"Already completed: {len(completed_ids)}, Remaining: {len(remaining_videos)}")
    print(f"Using {num_threads} threads (can collaborate on same video)")
    print(f"Loaded {len(proxy_pool.proxies)} proxies, auto-refresh enabled (up to 3x)")
    print("-" * 60)

    # Initialize thread status manager
    status_manager = ThreadStatusManager(num_threads, total)

    # Initialize progress with already completed items
    already_success = sum(1 for v in videos_data if v.get('transcript'))
    already_no_transcript = sum(1 for v in videos_data if v.get('method') == 'no_transcript')
    already_failed = len(videos_data) - already_success - already_no_transcript
    status_manager.update_progress(len(completed_ids), already_success, already_failed, already_no_transcript)

    # Create work queue for collaborative downloading
    work_queue = VideoWorkQueue(remaining_videos, proxy_pool)

    results_lock = threading.Lock()
    worker_to_slot = {}
    next_slot_id = [0]  # Use list to allow modification in nested function
    stop_flag = threading.Event()

    def worker_thread():
        """Worker that continuously processes videos until queue is empty."""
        # Assign display slot for this worker
        worker_name = threading.current_thread().name
        with results_lock:
            if worker_name not in worker_to_slot:
                worker_to_slot[worker_name] = next_slot_id[0]
                next_slot_id[0] += 1
            thread_slot = worker_to_slot[worker_name]

        while not stop_flag.is_set():
            # Get work (video + proxy)
            work = work_queue.get_work()
            if work is None:
                break  # No more work available

            video, proxy = work
            video_id = video['id']

            # Check if another thread already completed this video
            if work_queue.is_video_done(video_id):
                work_queue.release_work(video_id)
                continue

            title_short = video['title'][:35] + "..." if len(video['title']) > 35 else video['title']
            proxy_ip = proxy.split(':')[0]

            # Create status callback
            def update_status(msg):
                status_manager.update_status(thread_slot, f"{title_short[:25]}... | {msg}")

            update_status(f"Trying {proxy_ip}...")

            # Small random delay to spread out requests
            time.sleep(random.uniform(0.5, 1.5))

            # Try to download with this specific proxy
            result = download_single_video_with_proxy(video, proxy, status_callback=update_status)

            # Check again if another thread completed while we were working
            if work_queue.is_video_done(video_id):
                work_queue.release_work(video_id)
                status_manager.update_status(thread_slot, f"✓ {title_short[:30]}... completed by another thread")
                continue

            # Process result
            if result['transcript']:
                # Success!
                work_queue.mark_completed(video_id, result)
                method_short = result.get('method', 'unknown').split(':')[-1]
                status_manager.update_status(thread_slot, f"✓ OK ({result['language']}) via {method_short}")
            elif result['method'] == 'no_transcript':
                # Vote for no transcript (needs 2 confirmations)
                work_queue.mark_no_transcript(video_id, result)
                if work_queue.is_video_done(video_id):
                    status_manager.update_status(thread_slot, f"✗ No transcript (confirmed)")
                else:
                    status_manager.update_status(thread_slot, f"? No transcript (needs verification)")
            else:
                # Proxy failed - mark it and continue
                work_queue.mark_proxy_failed(video_id, proxy)
                status_manager.update_status(thread_slot, f"✗ {proxy_ip} failed, trying next...")

            work_queue.release_work(video_id)

            # Update progress display
            completed, success, no_trans, failed, _ = work_queue.get_progress()
            status_manager.update_progress(
                len(completed_ids) + completed,
                already_success + success,
                already_failed + failed,
                already_no_transcript + no_trans
            )

            # Signal all workers to stop if all videos are done
            if work_queue.is_all_work_done():
                stop_flag.set()

        # Worker finished
        with results_lock:
            status_manager.update_status(thread_slot, "Done")

    # Launch workers
    errors = []
    try:
        with status_manager.get_live_context():
            with ThreadPoolExecutor(max_workers=num_threads) as executor:
                # Submit worker threads
                futures = [executor.submit(worker_thread) for _ in range(num_threads)]

                # Wait for all workers to complete
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        errors.append(f"Worker error: {e}")

    except KeyboardInterrupt:
        stop_flag.set()
        status_manager.stop_display()
        print("\n\n** Download interrupted by user **")
        was_interrupted = True

    # Stop status display
    status_manager.stop_display()

    # Collect results from work queue
    new_results = work_queue.get_all_results()
    videos_data.extend(new_results)

    # Update completed_ids
    for result in new_results:
        completed_ids.add(result['id'])

    # Calculate final counts
    final_success = sum(1 for v in videos_data if v.get('transcript'))
    final_no_transcript = sum(1 for v in videos_data if v.get('method') == 'no_transcript')
    final_failed = len(videos_data) - final_success - final_no_transcript

    # Print errors
    for error_msg in errors:
        print(error_msg)

    # Final progress save
    progress_data = {
        'source_name': source_name,
        'source_type': source_type,
        'videos': videos,
        'current_index': len(completed_ids),
        'videos_data': videos_data,
        'completed_ids': list(completed_ids),
        'started_at': datetime.now().isoformat(),
        'completed': not was_interrupted,
        'mode': 'parallel_collaborative',
        'proxy_refreshes': work_queue.proxy_refresh_count,
        'output_config': output_config
    }
    save_progress(progress_data)

    print("\n" + "-" * 60)
    print(f"Results: {final_success} success, {final_no_transcript} no transcript, {final_failed} failed")
    print(f"Total proxy attempts: {work_queue.total_proxy_attempts}")
    print(f"Proxy refreshes used: {work_queue.proxy_refresh_count}/{VideoWorkQueue.MAX_PROXY_REFRESHES}")
    print(f"Proxies in pool: {len(proxy_pool.proxies)}")

    return videos_data, final_success, was_interrupted


# ============================================================================
# CLI Interface
# ============================================================================

def wait_for_spacebar():
    """Wait for user to press spacebar to continue."""
    print("\n  Press SPACE to return to main menu, or Q to quit...")

    if sys.platform == 'win32':
        while True:
            key = msvcrt.getch()
            if key == b' ':  # Spacebar
                return True
            elif key.lower() == b'q':  # Q to quit
                return False
    else:
        # Unix-like systems
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            while True:
                key = sys.stdin.read(1)
                if key == ' ':  # Spacebar
                    return True
                elif key.lower() == 'q':  # Q to quit
                    return False
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def print_header():
    """Print CLI header with ASCII art."""
    print()
    print("▄▄▄   ▄▄▄ ▄▄▄▄▄▄▄▄▄ ▄▄▄▄▄▄▄    ▄▄▄▄▄▄▄ ▄▄▄▄▄▄   ")
    print("███   ███ ▀▀▀███▀▀▀ ███▀▀███▄ █████▀▀▀ ███▀▀██▄ ")
    print("▀███▄███▀    ███    ███▄▄███▀  ▀████▄  ███  ███ ")
    print("  ▀███▀      ███    ███  ███▄    ▀████ ███  ███ ")
    print("   ███       ███    ████████▀ ███████▀ ██████▀  ")
    print()
    print("  YT Bulk Subtitles Downloader")
    print("  Download subtitles in MD or SRT format")
    print("=" * 50 + "\n")


def get_user_choice(has_unfinished: bool) -> int:
    """Display menu and get user choice."""
    print("Select download mode:\n")
    print("  [1] Single video transcript")
    print("  [2] All videos from a playlist")
    print("  [3] All videos from a channel")
    if has_unfinished:
        print("  [4] Resume last unfinished job")
    print("  [0] Exit\n")

    max_choice = 4 if has_unfinished else 3

    while True:
        try:
            choice = int(input(f"Enter your choice (0-{max_choice}): "))
            if 0 <= choice <= max_choice:
                if choice == 4 and not has_unfinished:
                    print("Invalid choice.")
                    continue
                return choice
            print(f"Invalid choice. Please enter 0-{max_choice}.")
        except ValueError:
            print("Invalid input. Please enter a number.")


def get_threading_choice() -> int:
    """Ask user for number of threads. Always uses threading."""
    # Check if proxy file exists
    if not os.path.exists(PROXY_FILE):
        print(f"\nWarning: {PROXY_FILE} not found!")
        print("Create this file with proxy list (IP:PORT per line)")
        print("Proxies are required since your IP may be rate-limited by YouTube.")
        return DEFAULT_THREADS

    # Count proxies to set optimal default threads
    proxy_count = 0
    try:
        with open(PROXY_FILE, 'r') as f:
            proxy_count = sum(1 for line in f if line.strip() and not line.strip().startswith('#'))
    except Exception:
        pass
    
    suggested_threads = max(MIN_THREADS, min(proxy_count, MAX_THREADS)) if proxy_count > 0 else DEFAULT_THREADS

    print(f"\nMulti-threaded download mode (using proxies from proxies.txt)")
    print(f"Loaded {proxy_count} proxies - default {suggested_threads} threads")

    while True:
        try:
            user_input = input(f"Number of threads ({MIN_THREADS}-{MAX_THREADS}, default {suggested_threads}): ")
            threads = int(user_input) if user_input.strip() else suggested_threads
            
            if MIN_THREADS <= threads <= MAX_THREADS:
                return threads
            print(f"Please enter a number between {MIN_THREADS} and {MAX_THREADS}.")
        except ValueError:
            print("Invalid input. Please enter a number.")


def get_url(mode: str) -> str:
    """Get URL from user based on mode."""
    prompts = {
        "single": "Enter YouTube video URL or ID: ",
        "playlist": "Enter YouTube playlist URL: ",
        "channel": "Enter YouTube channel URL (e.g., https://www.youtube.com/@channelname): "
    }
    return input(prompts[mode]).strip()


def get_channel_content_type() -> str:
    """Ask user what type of content to download from channel."""
    print("\nWhat content would you like to download?\n")
    print("  [1] Videos only")
    print("  [2] Shorts only")
    print("  [3] Both videos and shorts\n")

    while True:
        try:
            choice = int(input("Enter your choice (1-3): "))
            if choice == 1:
                return "videos"
            elif choice == 2:
                return "shorts"
            elif choice == 3:
                return "both"
            print("Invalid choice. Please enter 1, 2, or 3.")
        except ValueError:
            print("Invalid input. Please enter a number.")


def get_output_config() -> dict:
    """Ask user about output file configuration."""
    # First ask for format type
    print("\nOutput format:\n")
    print("  [1] Markdown (.md) - All transcripts in single/split files (for LLM RAG)")
    print("  [2] SRT (.srt) - Separate file per video with timestamps (subtitle format)\n")

    format_type = "markdown"
    while True:
        try:
            choice = int(input("Enter your choice (1-2): "))
            if choice == 1:
                format_type = "markdown"
                break
            elif choice == 2:
                format_type = "srt"
                break
            print("Invalid choice. Please enter 1 or 2.")
        except ValueError:
            print("Invalid input. Please enter a number.")

    # For SRT format, we always create separate files per video
    if format_type == "srt":
        return {'format': 'srt', 'split': True, 'max_words': None}

    # For Markdown, ask about file splitting
    print("\nFile configuration:\n")
    print("  [1] Single file with all transcripts")
    print("  [2] Split into multiple files by word count\n")

    while True:
        try:
            choice = int(input("Enter your choice (1-2): "))
            if choice == 1:
                return {'format': 'markdown', 'split': False, 'max_words': None}
            elif choice == 2:
                # Ask for word limit
                while True:
                    try:
                        limit = input("Max words per file (in thousands, e.g., 100 for 100K words): ")
                        max_words = int(limit) * 1000
                        if max_words > 0:
                            return {'format': 'markdown', 'split': True, 'max_words': max_words}
                        print("Please enter a positive number.")
                    except ValueError:
                        print("Invalid input. Please enter a number.")
            print("Invalid choice. Please enter 1 or 2.")
        except ValueError:
            print("Invalid input. Please enter a number.")


def normalize_channel_url(url: str) -> str:
    """
    Normalize channel URL by removing trailing /videos, /shorts, /streams, etc.
    Returns the base channel URL.
    """
    # Remove trailing slashes
    url = url.rstrip('/')

    # Remove known tab suffixes
    suffixes = ['/videos', '/shorts', '/streams', '/playlists', '/community', '/channels', '/about', '/featured']
    for suffix in suffixes:
        if url.endswith(suffix):
            url = url[:-len(suffix)]
            break

    return url


def finalize_output(videos_data: list[dict], source_name: str, source_type: str,
                    success_count: int, was_interrupted: bool, output_config: dict = None) -> bool:
    """Generate the final output file(s) in the configured format. Returns True to continue to menu, False to exit."""
    if was_interrupted:
        print("\nPartial results will be saved...")
    else:
        print("\nGenerating output file(s)...")

    output_dir = PROGRESS_DIR
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filepaths = []

    # Check output format
    output_format = output_config.get('format', 'markdown') if output_config else 'markdown'

    if output_format == 'srt':
        # Generate SRT files - one per video in a folder
        filepaths = create_srt_output(videos_data, source_name, output_dir)
    elif output_config and output_config.get('split') and output_config.get('max_words'):
        # Markdown with split by word count
        folder_name = sanitize_filename(source_name)
        split_dir = os.path.join(output_dir, folder_name)
        os.makedirs(split_dir, exist_ok=True)

        # Generate split files
        split_files = create_split_markdown_output(
            videos_data, source_name, source_type, output_config['max_words']
        )

        for filename, content in split_files:
            filename = sanitize_filename(filename) + ".md"
            filepath = os.path.join(split_dir, filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
            filepaths.append(filepath)
    else:
        # Single markdown file output (original behavior)
        if source_type == "single":
            filename = f"{source_name}.md"
        elif source_type == "playlist":
            filename = f"playlist_{source_name}_{timestamp}.md"
        else:
            filename = f"channel_{source_name}_{timestamp}.md"

        filename = sanitize_filename(filename)
        filepath = os.path.join(output_dir, filename)

        content = create_markdown_output(videos_data, source_name, source_type)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        filepaths.append(filepath)

    if not was_interrupted:
        clear_progress()

    print("\n" + "="*60)
    if was_interrupted:
        print("  Download Paused (Progress Saved)")
    else:
        print("  Download Complete!")
    print("="*60)
    print(f"\n  Total videos:      {len(videos_data)}")
    print(f"  With transcripts:  {success_count}")
    print(f"  Without:           {len(videos_data) - success_count}")

    if output_format == 'srt':
        folder_name = sanitize_filename(source_name)
        srt_dir = os.path.join(output_dir, folder_name)
        print(f"\n  Output format: SRT (subtitles)")
        print(f"  Output folder: {srt_dir}")
        print(f"  SRT files created: {len(filepaths)}")
    elif len(filepaths) == 1:
        print(f"\n  Output file: {filepaths[0]}")
    else:
        print(f"\n  Output files ({len(filepaths)} parts):")
        for fp in filepaths[:5]:  # Show first 5
            print(f"    - {fp}")
        if len(filepaths) > 5:
            print(f"    ... and {len(filepaths) - 5} more")

    if was_interrupted:
        print(f"\n  To resume: Run the script again and select 'Resume last unfinished job'")

    # Wait for spacebar to return to menu or Q to quit
    return wait_for_spacebar()


def run_new_job(mode: str) -> bool:
    """Start a new download job. Returns True to continue to menu, False to exit."""
    url = get_url(mode)
    if not url:
        print("Error: No URL provided.")
        return True

    if mode == "single":
        url = f"https://www.youtube.com/watch?v={extract_video_id(url)}"

    # Initialize proxy pool early for video info fetching
    proxy_pool = ProxyPool(PROXY_FILE, validate=False)

    # Handle channel content type selection
    if mode == "channel":
        # Normalize the URL first (remove any existing /videos, /shorts, etc.)
        url = normalize_channel_url(url)
        content_type = get_channel_content_type()

        videos = []
        source_name = ""
        source_type = "channel"

        if content_type == "videos":
            print(f"\nFetching videos from channel...")
            try:
                videos, source_name, source_type = get_video_ids_from_url(url + "/videos", mode, proxy_pool)
            except Exception as e:
                print(f"Error fetching video information: {e}")
                return True

        elif content_type == "shorts":
            print(f"\nFetching shorts from channel...")
            try:
                videos, source_name, source_type = get_video_ids_from_url(url + "/shorts", mode, proxy_pool)
            except Exception as e:
                print(f"Error fetching shorts information: {e}")
                return True

        else:  # both
            print(f"\nFetching videos from channel...")
            try:
                videos_list, source_name, source_type = get_video_ids_from_url(url + "/videos", mode, proxy_pool)
                videos.extend(videos_list)
                print(f"Found {len(videos_list)} video(s)")
            except Exception as e:
                print(f"Warning: Could not fetch videos: {e}")

            print(f"\nFetching shorts from channel...")
            try:
                shorts_list, _, _ = get_video_ids_from_url(url + "/shorts", mode, proxy_pool)
                videos.extend(shorts_list)
                print(f"Found {len(shorts_list)} short(s)")
            except Exception as e:
                print(f"Warning: Could not fetch shorts: {e}")

            if not source_name:
                # Try to extract channel name from URL if we couldn't get it
                match = re.search(r'@([^/]+)', url)
                source_name = match.group(1) if match else "channel"

        if not videos:
            print("No videos found.")
            return True

        print(f"\nTotal: {len(videos)} video(s)")
        print(f"Source: {source_name}")

    else:
        # Single video or playlist mode
        print(f"\nFetching video information...")

        try:
            videos, source_name, source_type = get_video_ids_from_url(url, mode, proxy_pool)
        except Exception as e:
            print(f"Error fetching video information: {e}")
            return True

        if not videos:
            print("No videos found.")
            return True

        print(f"Found {len(videos)} video(s)")
        print(f"Source: {source_name}")

    # Always use threading mode - ask for thread count
    num_threads = get_threading_choice()

    # Ask for output file configuration
    output_config = get_output_config()

    # Download transcripts using multi-threaded mode with proxies
    videos_data, success_count, was_interrupted = download_transcripts_parallel(
        videos, source_name, source_type, num_threads,
        output_config=output_config
    )

    if videos_data:
        return finalize_output(videos_data, source_name, source_type, success_count, was_interrupted, output_config)
    return True  # Continue to menu if no data


def resume_job(progress: dict) -> bool:
    """Resume an interrupted job. Returns True to continue to menu, False to exit."""
    source_name = progress['source_name']
    source_type = progress['source_type']
    videos = progress['videos']
    existing_data = progress.get('videos_data', [])
    completed_ids = set(progress.get('completed_ids', [v['id'] for v in existing_data]))

    print(f"\nResuming job: {source_name}")
    print(f"Progress: {len(completed_ids)}/{len(videos)} videos completed")

    # Always use threading mode - ask for thread count
    num_threads = get_threading_choice()

    # Retrieve output config from progress data or ask user
    output_config = progress.get('output_config')
    if output_config is None:
        output_config = get_output_config()

    videos_data, success_count, was_interrupted = download_transcripts_parallel(
        videos, source_name, source_type, num_threads,
        existing_data=existing_data, completed_ids=completed_ids,
        output_config=output_config
    )

    if videos_data:
        return finalize_output(videos_data, source_name, source_type, success_count, was_interrupted, output_config)
    return True  # Continue to menu if no data


def main():
    # Ensure subtitles folder exists for saving downloaded transcripts
    os.makedirs(PROGRESS_DIR, exist_ok=True)

    # Download fresh proxies on startup
    download_fresh_proxies(PROXY_FILE)

    while True:
        print_header()

        progress = load_progress()
        has_unfinished = progress is not None and not progress.get('completed', True)

        if has_unfinished:
            completed = len(progress.get('completed_ids', progress.get('videos_data', [])))
            total = len(progress.get('videos', []))
            print(f"** Unfinished job found: {progress['source_name']} **")
            print(f"   Progress: {completed}/{total} videos\n")

        choice = get_user_choice(has_unfinished)

        if choice == 0:
            print("\nGoodbye!")
            return

        continue_to_menu = True
        if choice == 4:
            continue_to_menu = resume_job(progress)
        else:
            mode_map = {1: "single", 2: "playlist", 3: "channel"}
            mode = mode_map[choice]
            continue_to_menu = run_new_job(mode)

        # If user pressed Q or function returned False, exit
        if not continue_to_menu:
            print("\nGoodbye!")
            return


if __name__ == "__main__":
    main()
