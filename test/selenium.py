import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

def download_hd(tiktok_url):
    options = Options()
    options.add_experimental_option("detach", True)

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )

    wait = WebDriverWait(driver, 60)

    try:
        driver.get("https://ssstik.io/")

        # Input TikTok URL
        input_box = wait.until(
            EC.presence_of_element_located((By.NAME, "id"))
        )
        input_box.clear()
        input_box.send_keys(tiktok_url)

        # Submit
        submit_btn = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))
        )
        submit_btn.click()

        print("Waiting for ad/timer...")
        time.sleep(35)

        # Locate HD or watermark-free button
        hd_button = wait.until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//a[contains(., 'Without watermark') or contains(., 'Download HD')]"
            ))
        )

        hd_link = hd_button.get_attribute("href")
        print("HD Link:", hd_link)

        driver.get(hd_link)

        print("Download triggered.")
        time.sleep(10)

    finally:
        driver.quit()

if __name__ == "__main__":
    url = input("Enter TikTok URL: ")
    download_hd(url)