"""
scraper.py — Headless Chrome scraper with User-Agent + retry
"""

import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

MAX_RETRIES = 2
WAIT_SECONDS = 10
EXTRA_RENDER_WAIT = 2


def scrape_url_content(url: str) -> str | None:
    """
    Scrapes visible text from a job posting URL using headless Chrome.
    Retries up to MAX_RETRIES times on failure.
    """
    for attempt in range(1, MAX_RETRIES + 2):
        result = _try_scrape(url, attempt)
        if result:
            return result
        if attempt <= MAX_RETRIES:
            print(f"  Retrying... (attempt {attempt + 1})")
            time.sleep(2)

    print("❌ All scraping attempts failed.")
    return None


def _try_scrape(url: str, attempt: int = 1) -> str | None:
    print(f"  [Attempt {attempt}] Scraping: {url}")

    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    # ← KEY FIX: realistic User-Agent prevents most bot-detection blocks
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    # Disable automation flags that sites detect
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = None
    try:
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)

        # Remove webdriver property (detected by some sites)
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"}
        )

        driver.get(url)

        WebDriverWait(driver, WAIT_SECONDS).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(EXTRA_RENDER_WAIT)

        content = driver.find_element(By.TAG_NAME, "body").text

        if len(content.strip()) < 200:
            print("  ⚠  Page content too short — may be blocked or dynamic.")
            return None

        print(f"  ✅ Scraped {len(content)} characters.")
        return content

    except Exception as e:
        print(f"  ❌ Scraping error: {e}")
        return None

    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    url = "https://careers.expediagroup.com/job/machine-learning-engineer-iii/bangalore-/R-99283/"
    content = scrape_url_content(url)
    if content:
        print(f"\nFirst 500 chars:\n{content[:500]}")