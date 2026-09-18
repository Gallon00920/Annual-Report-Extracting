from selenium import webdriver
import requests
import time
import csv
import os
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

SAVE_DIR = "/Users/jialunxu/Documents/personal/projects/annual_report/table_extraction/reports"
os.makedirs(SAVE_DIR, exist_ok=True)

# Configure Chrome to automatically download files natively (bypasses 7KB / SSL errors)
chrome_options = Options()
chrome_options.add_experimental_option("detach", True)
prefs = {
    "download.default_directory": SAVE_DIR,
    "download.prompt_for_download": False,
    "download.directory_upgrade": True,
    "plugins.always_open_pdf_externally": True  # Prevents Chrome from opening PDFs in a tab
}
chrome_options.add_experimental_option("prefs", prefs)

driver = webdriver.Chrome(options=chrome_options)
driver.get('http://www.sse.com.cn/disclosure/listedinfo/regular/')

# Wait for page initialization
time.sleep(5)

# Locate and enter stock code
search = driver.find_element(By.XPATH, "//*[@id='inputCode']")
search.send_keys("600690")
search.send_keys(Keys.RETURN)
time.sleep(2)

link = driver.find_element(By.XPATH, '/html/body/div[9]/div/div[1]/div/div[4]/div[2]/div/button/div')
link.click()
link = driver.find_element(By.XPATH, '/html/body/div[9]/div/div[1]/div/div[4]/div[2]/div/div/div/ul/li[2]/a')
link.click()

# Loop over years 2007 to 2026
for year in range(2007, 2027):
    print(f"\n=== Processing Year: {year} ===")
    
    element = driver.find_element(By.XPATH, "/html/body/div[9]/div/div[1]/div/div[5]/div[2]/input")
    driver.execute_script("arguments[0].removeAttribute('readonly');", element)
    element.clear()
    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"
    element.send_keys(f"{start_date} - {end_date}")
    time.sleep(1)
    element.send_keys(Keys.RETURN)

    
    time.sleep(3)  # Wait for table to reload filtered results

    # Locate report links
    report_links = driver.find_elements(By.CSS_SELECTOR, "a.table_titlewrap")

    if not report_links:
        print(f"No reports found for year {year}.")
        continue

    for link in report_links:
        try:
            span_element = link.find_element(By.CSS_SELECTOR, "span[onclick*='clickRegularPdf']")
            title_text = span_element.text.strip()
        except Exception:
            continue

        # Filter out unwanted reports
        if "摘要" in title_text or "英文" in title_text:
            print(f"Skipping: {title_text}")
            continue

        print(f"Triggering download for: {title_text}")

        # Scroll to element center to minimize visual occlusion
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", span_element)
        time.sleep(0.5)
        
        # JS click ignores overlapping headers/sticky containers
        driver.execute_script("arguments[0].click();", span_element)
        
        time.sleep(2)  # Throttle downloads to prevent rate limits

    time.sleep(2)

print("\nFinished triggering downloads. Waiting for final transfers to complete...")
time.sleep(10)
driver.quit()
