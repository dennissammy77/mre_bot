import requests
from bs4 import BeautifulSoup
import json
import os
import time
import logging
import schedule
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError
import re

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/train_monitor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("TrainMonitor")

class ConfigManager:
    """Manage configuration from file and environment variables"""
    
    def __init__(self, config_path='config.json'):
        self.config_path = config_path
        self.config = self._load_config()
        self._validate_config()

    def _load_config(self):
        try:
            with open(self.config_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error(f"Config file {self.config_path} not found.")
            return {}
        except json.JSONDecodeError:
            logger.error(f"Error decoding {self.config_path}.")
            return {}

    def _validate_config(self):
        # Ensure essential keys exist
        defaults = {
            "dates": [],
            "train_types": ["express", "inter_county"],
            "classes": ["first", "economy"],
            "check_interval": 60,
            "route": {
                "terminal_id": 3,
                "destination_id": 2
            },
            "departure_times": ["3.00", "10.00"]
        }
        for key, value in defaults.items():
            if key not in self.config:
                self.config[key] = value

    def get(self, key, default=None):
        return self.config.get(key, default)

    @property
    def telegram_token(self):
        return os.getenv('TELEGRAM_BOT_TOKEN') or self.config.get('telegram', {}).get('bot_token')

    @property
    def telegram_chat_id(self):
        return os.getenv('TELEGRAM_CHAT_ID') or self.config.get('telegram', {}).get('chat_id')

    @property
    def telegram_channel_id(self):
        return os.getenv('TELEGRAM_CHANNEL_ID') or self.config.get('telegram', {}).get('channel_id')

class CSRFHandler:
    """Handle CSRF token extraction and management"""
    
    def __init__(self):
        self.base_url = "https://metickets.krc.co.ke"
        self.session = requests.Session()
        self.csrf_token = None
        
    def extract_csrf_token(self):
        """Extract CSRF token from the index page"""
        try:
            logger.info("Fetching CSRF token...")
            response = self.session.get(
                f"{self.base_url}/index.php",
                headers={
                    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8'
                },
                timeout=30
            )
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            csrf_input = soup.find('input', {'name': 'csrf_token'})
            
            if csrf_input and csrf_input.get('value'):
                self.csrf_token = csrf_input.get('value')
                logger.debug(f"CSRF token extracted: {self.csrf_token[:10]}...")
                return self.csrf_token
            else:
                logger.error("CSRF token not found in HTML")
                return None
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching CSRF token: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in CSRF extraction: {e}")
            return None
    
    def get_session(self):
        return self.session

class TrainScraper:
    """Handle train availability requests and HTML retrieval"""
    
    def __init__(self, csrf_handler):
        self.csrf_handler = csrf_handler
        self.base_url = "https://metickets.krc.co.ke"
        
    def search_trains(self, schedule_type, travel_date, terminal_id, destination_id, departure_time="10.00"):
        try:
            csrf_token = self.csrf_handler.extract_csrf_token()
            if not csrf_token:
                return None
            
            form_data = {
                'csrf_token': csrf_token,
                'schedule_type': schedule_type,
                'terminal_id': str(terminal_id),
                'destination_id': str(destination_id),
                'travel-date': travel_date,
                'depature_time': departure_time
            }
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': self.base_url,
                'Referer': f'{self.base_url}/index.php',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8'
            }
            
            # logger.info(f"Searching {schedule_type} trains for {travel_date} at {departure_time}...")
            
            response = self.csrf_handler.get_session().post(
                f"{self.base_url}/search-view-results.php",
                data=form_data,
                headers=headers,
                timeout=30
            )
            response.raise_for_status()
            return response.text
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error searching trains: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in train search: {e}")
            return None

class AvailabilityChecker:
    """Determine seat availability by class"""
    
    @staticmethod
    def check_availability(html_content):
        if not html_content:
            return False, []
        
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Check for "Fully Booked" message
        fully_booked = soup.find('h4', class_='main-message')
        if fully_booked and 'Fully Booked' in fully_booked.text:
            return False, []
        
        # Check for available trains
        form_tags = soup.find('div', id='form-tags')
        if not form_tags:
            return False, []
            
        trains = []
        forms = form_tags.find_all('form', {'action': 'booking-details.php'})
        
        for form in forms:
            try:
                train_data = {}
                
                # Extract Departure and Arrival (New Structure)
                # <small class="resulttime">Departure: <span class="span">04:30 pm</span></small>
                times = form.find_all('small', class_='resulttime')
                if len(times) >= 2:
                    dep_span = times[0].find('span', class_='span')
                    arr_span = times[1].find('span', class_='span')
                    if dep_span:
                        train_data['departure'] = dep_span.text.strip()
                    if arr_span:
                        train_data['arrival'] = arr_span.text.strip()
                
                # Fallback for old structure if new one fails
                if 'departure' not in train_data:
                    time_divs = form.find_all('div', class_='time')
                    if len(time_divs) >= 2:
                        train_data['departure'] = time_divs[0].text.strip()
                        train_data['arrival'] = time_divs[1].text.strip()

                # Train Name
                # In the new snippet, there is no explicit train name. 
                # We'll construct one or look for h3 if it exists (old structure).
                train_name_elem = form.find('h3')
                if train_name_elem:
                    train_data['name'] = train_name_elem.text.strip()
                else:
                    # Construct name from departure
                    train_data['name'] = f"Train {train_data.get('departure', 'Unknown')}"

                # Extract Class Information (New Structure)
                # Look for columns containing h4.box-title
                cols = form.find_all('div', class_=['col-md-6', 'col-sm-6'])
                found_classes = False
                
                for col in cols:
                    title = col.find('h4', class_='box-title')
                    if not title:
                        continue
                    
                    found_classes = True
                    title_text = title.text.strip().upper()
                    
                    # Extract seats: "FIRST CLASS - 0 SEATS OPEN"
                    seats = 0
                    seat_match = re.search(r'(\d+)\s+SEATS', title_text)
                    if seat_match:
                        seats = int(seat_match.group(1))
                    
                    # Extract prices
                    adult_price = "N/A"
                    child_price = "N/A"
                    
                    details = col.find_all('dl', class_='details')
                    for dl in details:
                        dt = dl.find('dt')
                        dd = dl.find('dd')
                        if dt and dd:
                            label = dt.text.strip().lower()
                            price = dd.text.strip()
                            if 'adult' in label:
                                adult_price = price
                            elif 'children' in label and '3 - 11' in label:
                                child_price = price
                    
                    if 'FIRST CLASS' in title_text:
                        train_data['first_class_seats'] = seats
                        train_data['first_class_adult'] = adult_price
                        train_data['first_class_child'] = child_price
                    elif 'ECONOMY' in title_text or 'SECOND CLASS' in title_text:
                        train_data['economy_seats'] = seats
                        train_data['economy_adult'] = adult_price
                        train_data['economy_child'] = child_price

                # Fallback for old structure (buttons) if no classes found above
                if not found_classes:
                    class_buttons = form.find_all('button', class_='class-btn')
                    for button in class_buttons:
                        class_text = button.text.strip()
                        if 'FIRST CLASS' in class_text:
                            seats = ''.join(filter(str.isdigit, class_text.split('-')[1] if '-' in class_text else '0'))
                            train_data['first_class_seats'] = int(seats) if seats else 0
                            
                            price_section = button.find_next('div', class_='price-section')
                            if price_section:
                                prices = price_section.find_all('span', class_='price')
                                if len(prices) >= 2:
                                    train_data['first_class_adult'] = prices[0].text.strip()
                                    train_data['first_class_child'] = prices[1].text.strip()
                        
                        elif 'ECONOMY' in class_text or 'SECOND CLASS' in class_text:
                            seats = ''.join(filter(str.isdigit, class_text.split('-')[1] if '-' in class_text else '0'))
                            train_data['economy_seats'] = int(seats) if seats else 0
                            
                            price_section = button.find_next('div', class_='price-section')
                            if price_section:
                                prices = price_section.find_all('span', class_='price')
                                if len(prices) >= 2:
                                    train_data['economy_adult'] = prices[0].text.strip()
                                    train_data['economy_child'] = prices[1].text.strip()
                
                trains.append(train_data)
                
            except Exception as e:
                logger.error(f"Error parsing train data: {e}")
                continue
                
        return True, trains

class TelegramNotifier:
    """Send formatted alerts"""
    
    def __init__(self, token, chat_id, channel_id):
        self.token = token
        self.chat_id = chat_id
        self.channel_id = channel_id
        self.sent_alerts = set() # To avoid duplicate alerts for same train/date/time in short window

    async def send_notifications(self, message):
        """Send message to configured chat and channel"""
        bot = Bot(token=self.token)
        try:
            await bot.send_message(chat_id=self.chat_id, text=message, parse_mode='Markdown')
            logger.info("Telegram notification sent.")
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            
        if self.channel_id:
            try:
                await bot.send_message(chat_id=self.channel_id, text=message, parse_mode='Markdown')
                logger.info("Telegram notification sent to channel.")
            except Exception as e:
                logger.error(f"Failed to forward message to channel: {e}")

    def format_alert(self, train, date, schedule_type):
        return f"""
🚂 *TRAIN AVAILABLE ALERT!* 🚂

*Date:* {date}
*Train:* {train.get('name', 'Unknown')} ({schedule_type})
*Departure:* {train.get('departure', 'N/A')}
*Arrival:* {train.get('arrival', 'N/A')}

*First Class:* {train.get('first_class_seats', 0)} seats available

*Economy:* {train.get('economy_seats', 0)} seats available

*Book Now:* https://metickets.krc.co.ke
"""

class TrainMonitor:
    def __init__(self):
        self.config_manager = ConfigManager()
        self.csrf_handler = CSRFHandler()
        self.scraper = TrainScraper(self.csrf_handler)
        self.notifier = TelegramNotifier(
            self.config_manager.telegram_token,
            self.config_manager.telegram_chat_id,
            self.config_manager.telegram_channel_id
        )
        self.available_cache = set() # Store (date, time, train_name) to avoid spamming

    def check_job(self):
      try:
        dates = self.config_manager.get('dates')
        train_types = self.config_manager.get('train_types')
        route = self.config_manager.get('route')
        departure_times = self.config_manager.get('departure_times')

        logger.info(f"Starting check cycle for {len(dates)} dates...")

        for date in dates:
            for schedule_type in train_types:
                # For express, we might need to check specific times if required, 
                # but usually the search returns all for that type? 
                # The form has a departure_time field.
                # If schedule_type is express, we iterate times.
                # If inter_county, usually time doesn't matter as much or there's only one?
                # The user plan says: depature_time can be "3.00" or "10.00" for Express trains
                
                times_to_check = departure_times if schedule_type == 'express' else ["08.00"] # Default for inter_county? Or just one check.
                # Actually, let's just use the times from config for express, and maybe a default for others.
                # If inter_county, the time param might be ignored or standard.
                
                if schedule_type == 'inter_county':
                    times_to_check = ["08.00"] # Usually starts early

                for time_val in times_to_check:
                    html = self.scraper.search_trains(
                        schedule_type=schedule_type,
                        travel_date=date,
                        terminal_id=route['terminal_id'],
                        destination_id=route['destination_id'],
                        departure_time=time_val
                    )
                    
                    is_available, trains = AvailabilityChecker.check_availability(html)
                    
                    if is_available and trains:
                        for train in trains:
                            # Check if we should alert
                            # We alert if we haven't alerted for this specific train/date recently
                            # or if seats changed significantly? For now, just alert.
                            
                            # Simple de-duplication key
                            cache_key = f"{date}_{train.get('name')}_{train.get('departure')}_fclass{train.get('first_class_seats', 0)}_eco{train.get('economy_seats', 0)}"
                            
                            if cache_key not in self.available_cache:
                                message = self.notifier.format_alert(train, date, schedule_type)
                                asyncio.run(self.notifier.send_notifications(message))
                                self.available_cache.add(cache_key)
                                logger.info(f"Alert sent for {cache_key}")
                            else:
                                logger.info(f"Already alerted for {cache_key}, skipping.")
                    else:
                        # Clear from cache if it becomes unavailable? 
                        
                        # Or maybe we want to alert again if it reappears?
                        
                        # For now, let's keep it simple. If it was available and now isn't, remove from cache.
                        # But we don't know which specific train is unavailable if we get "Fully Booked".
                        # So maybe clear cache for this date/time query if fully booked.
                        pass
      except Exception as e:
          logger.error(f"Error processing train availability: {e}")

    def run(self):
        interval = self.config_manager.get('check_interval', 60)
        logger.info(f"Starting monitor with {interval}s interval")
        
        # Run immediately once
        self.check_job()
        
        schedule.every(interval).seconds.do(self.check_job)
        
        while True:
            schedule.run_pending()
            time.sleep(1)

if __name__ == "__main__":
    try:
        monitor = TrainMonitor()
        monitor.run()
    except KeyboardInterrupt:
        logger.info("Stopping monitor...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
