import pandas as pd
from playwright.sync_api import sync_playwright
import time
import re
import threading
from datetime import datetime
import sys

# Load CSV
df = pd.read_csv("Cts (1-100) - ian - Sheet1.csv")
df = df.reset_index(drop=True)

# Ensure the new column exists
if "New Upcoming Auctions" not in df.columns:
    df["New Upcoming Auctions"] = ""
    # Save immediately to ensure the column is present in the CSV
    try:
        df.to_csv("Cts (1-100) - ian - Sheet1.csv", index=False)
    except PermissionError:
        print("Permission denied: Please close the CSV file if it is open in Excel or another program.")

def get_next_auction_date(page, url):
    page.goto(url, timeout=60000)
    # TODO: Customize selectors for each auction site
    # Example: Try to find a date pattern in the page text
    text = page.content()
    match = re.search(r'(\w+\s\d{1,2},\s\d{4})', text)
    if match:
        return match.group(1)
    return "Not found"

def extract_auction_dates(text):
    """
    Extracts a wide variety of auction date formats and ranges from the given text.
    Also extracts lines near 'Next Auction', 'Upcoming Auction', or similar (case-insensitive).
    """
    patterns = [
        r'([A-Za-z]{3,9}\s\d{1,2},\s\d{4})',  # June 6, 2024
        r'([A-Za-z]{3,9}\s\d{1,2}(?:st|nd|rd|th)?,\s\d{4})',  # June 6th, 2024
        r'([A-Za-z]{3,9}\s\d{1,2}\s?@\s?\d{1,2}:\d{2}(?:am|pm)?\s?[A-Z]{2,4}\s?\(Start\))',  # Jun 6 @ 10:00am EDT (Start)
        r'([A-Za-z]{3,9}\s\d{1,2}\s?@\s?\d{1,2}:\d{2}(?:am|pm)?\s?[A-Z]{2,4}\s?\(End\))',  # Jun 6 @ 10:00am EDT (End)
        r'(\d{1,2}/\d{1,2}/\d{2,4})',  # 06/04/2025
        r'(\d{1,2}-\d{1,2}-\d{2,4})',  # 6-4-2025
        r'(\d{1,2}/\d{1,2}/\d{2,4}\s+to\s+\d{1,2}/\d{1,2}/\d{2,4})',  # 06/6/24 to 06/20/24
        r'(\d{1,2}-\d{1,2}-\d{2,4}\s+to\s+\d{1,2}-\d{1,2}-\d{2,4})',  # 6-6-24 to 6-20-24
        r'Closes\s+(\d{1,2}/\d{1,2}/\d{2,4})',  # Closes 06/04/2025
        r'Closing on (\d{1,2}-\d{1,2}-\d{2,4})',  # Closing on 7-15-25
        r'(\d{1,2}h\s\d{1,2}m)',  # 13h 37m
        r'(\d{1,2}/\d{1,2}/\d{2,4}\s*-\s*\d{1,2}/\d{1,2}/\d{2,4})',  # 06/6/24 - 06/20/24
        r'([A-Za-z]{3,9}\s\d{1,2}(?:st|nd|rd|th)?,\s\d{4}\s*-\s*[A-Za-z]{3,9}\s\d{1,2}(?:st|nd|rd|th)?,\s\d{4})',  # June 6th, 2024 - June 20th, 2024
    ]
    found = set()
    for pat in patterns:
        found.update(re.findall(pat, text))
    # Also look for lines containing 'next auction', 'upcoming auction', etc. (case-insensitive)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.search(r'(next|upcoming)\s+auction', line, re.IGNORECASE):
            # Try to extract a date from this line and the next 2 lines
            context = ' '.join(lines[i:i+3])
            for pat in patterns:
                found.update(re.findall(pat, context))
    return found

# Keep track of auctions already in the CSV
existing_auctions = set(str(x).strip() for x in df['Next Auction'] if pd.notna(x))

# Store new upcoming auctions found in this run
new_upcoming_auctions = []

# Helper to check if a date string is in the future
def is_future_date(date_str):
    try:
        # Try parsing with multiple formats
        formats = [
            "%B %d, %Y", "%b %d, %Y",  # June 6, 2024; Jun 6, 2024
            "%B %d, %y", "%b %d, %y",  # June 6, 24; Jun 6, 24
            "%m/%d/%Y", "%m/%d/%y",    # 06/04/2025; 06/04/25
            "%m-%d-%Y", "%m-%d-%y",    # 6-4-2025; 6-4-25
            "%Y-%m-%d", "%y-%m-%d",    # 2025-06-04; 25-06-04
            "%b %d @ %I:%M%p %Z (Start)", "%b %d @ %I:%M%p %Z (End)", # Jul 1 @ 12:00pm EDT (Start)
            "%b %d @ %I:%M%p (Start)", "%b %d @ %I:%M%p (End)", # Jul 1 @ 12:00pm (Start)
        ]
        # Remove ordinal suffixes (st, nd, rd, th)
        date_str = re.sub(r'(\d{1,2})(st|nd|rd|th)', r'\1', date_str)
        # Remove extra spaces
        date_str = date_str.strip()
        # Handle '13h 37m' or similar as today
        if re.match(r'\d{1,2}h \d{1,2}m', date_str):
            return True
        # Handle 'Simulcast Begins Closing on 7-15-25' or 'Closing on 7-15-25'
        m = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})', date_str)
        if m:
            month, day, year = m.groups()
            if len(year) == 2:
                year = '20' + year if int(year) < 50 else '19' + year
            try:
                auction_date = datetime(int(year), int(month), int(day))
                return auction_date.date() > datetime.now().date()
            except Exception:
                pass
        # Try all formats
        for fmt in formats:
            try:
                auction_date = datetime.strptime(date_str, fmt)
                return auction_date.date() > datetime.now().date()
            except ValueError:
                continue
    except Exception:
        pass
    return False

# Thread flag for stopping
stop_flag = threading.Event()

def input_listener():
    while True:
        user_input = input()
        if user_input.strip().lower() == "stop":
            stop_flag.set()
            break

# Start input listener thread
threading.Thread(target=input_listener, daemon=True).start()

while not stop_flag.is_set():
    print("Starting new auction check cycle...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chrome")
        for idx, row in df.iterrows():
            if stop_flag.is_set():
                break
            url = str(row["Auction Link(s)"]).strip()
            if url.startswith("http"):
                print(f"Visiting {url} for {row['County']}, {row['State']}...")
                try:
                    page = browser.new_page()
                    page.goto(url, timeout=60000)
                    time.sleep(4)  # Wait for the page to fully load and render
                    text = page.content()
                    auctions = extract_auction_dates(text)
                    page.close()
                    new_auctions = auctions - existing_auctions
                    # Try to filter for future dates if possible
                    future_auctions = [a for a in new_auctions if is_future_date(a)]
                    if future_auctions:
                        # Save all valid future auction dates as dicts, one per line
                        auction_dicts = [
                            {
                                'County': row['County'],
                                'State': row['State'],
                                'Auction Link(s)': url,
                                'Next Auction': auction_date
                            }
                            for auction_date in future_auctions
                        ]
                        df.at[idx, "New Upcoming Auctions"] = "\n".join([str(ad) for ad in auction_dicts])
                        # Immediately save after updating the row
                        try:
                            df.to_csv("Cts (1-100) - ian - Sheet1.csv", index=False)
                        except PermissionError:
                            print("Permission denied: Please close the CSV file if it is open in Excel or another program.")
                        for auction_date in future_auctions:
                            print(f"New upcoming auction found for {row['County']}: {auction_date}")
                            new_upcoming_auctions.append({
                                'County': row['County'],
                                'State': row['State'],
                                'Auction Link(s)': url,
                                'Next Auction': auction_date
                            })
                        existing_auctions.update(future_auctions)
                except Exception as e:
                    print(f"Error for {row['County']}: {e}")
        browser.close()
    print("Cycle complete. Sleeping for 10 minutes before next check...")
    for _ in range(60*10):
        if stop_flag.is_set():
            break
        time.sleep(1)

# Final output of new upcoming auctions
print("New upcoming auctions found:")
for auction in new_upcoming_auctions:
    print(auction)