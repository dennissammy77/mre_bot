import requests
from bs4 import BeautifulSoup
import json
import os
import time
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/train_monitor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DATES = ["02/01/2026", "03/01/2026", "04/01/2026"]  # Multiple dates
TRAIN_TYPES = ["express", "inter_county"]  # Both types
CLASSES = ["first", "economy"]  # Monitor both classes
CHECK_INTERVAL = 60  # Seconds between checks
TERMINAL_ID = 3  # Mombasa Terminus
DESTINATION_ID = 2  # Nairobi Terminus
DEPARTURE_TIMES = ["3.00", "10.00"]  # For express trains

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
                logger.info(f"CSRF token extracted successfully: {self.csrf_token[:20]}...")
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
        """Return the session with cookies"""
        return self.session

class TrainScraper:
    """Handle train availability requests and HTML retrieval"""
    
    def __init__(self, csrf_handler):
        self.csrf_handler = csrf_handler
        self.base_url = "https://metickets.krc.co.ke"
        
    def search_trains(self, schedule_type, travel_date, terminal_id, destination_id, departure_time="10.00"):
        """
        Search for train availability
        
        Args:
            schedule_type: 'express' or 'inter_county'
            travel_date: Format DD/MM/YYYY
            terminal_id: Starting station ID
            destination_id: Destination station ID
            departure_time: '3.00', '4.30' or '10.00' for express trains
        """
        try:
            # Get fresh CSRF token
            csrf_token = self.csrf_handler.extract_csrf_token()
            if not csrf_token:
                logger.error("Failed to get CSRF token")
                return None
            
            # Prepare form data
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
            
            logger.info(f"Searching {schedule_type} trains for {travel_date}...")
            
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


def check_trains_available(html_content):
    """
    Check if trains are available or fully booked
    
    Returns:
        True if trains available, False if fully booked
    """
    if not html_content:
        return False
    
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Check for "Fully Booked" message
    fully_booked = soup.find('h4', class_='main-message')
    if fully_booked and 'Fully Booked' in fully_booked.text:
        logger.info("Status: Fully Booked")
        return False
    
    # Check for available trains (form-tags div)
    form_tags = soup.find('div', id='form-tags')
    if form_tags:
        logger.info("Status: Trains Available!")
        return True
    
    logger.warning("Status: Unknown (neither fully booked nor available forms found)")
    return False


def parse_available_trains(html_content):
    """
    Parse all available train information from HTML
    
    Returns:
        List of dictionaries containing train information
    """
    if not html_content:
        return []
    
    soup = BeautifulSoup(html_content, 'html.parser')
    trains = []
    
    # Find all train forms
    form_tags = soup.find('div', id='form-tags')
    if not form_tags:
        return []
    
    forms = form_tags.find_all('form', {'action': 'booking-details.php'})
    
    for form in forms:
        try:
            train_data = {}
            
            # Extract train name
            train_name_elem = form.find('h3')
            if train_name_elem:
                train_data['name'] = train_name_elem.text.strip()
            
            # Extract departure and arrival times
            time_divs = form.find_all('div', class_='time')
            if len(time_divs) >= 2:
                train_data['departure'] = time_divs[0].text.strip()
                train_data['arrival'] = time_divs[1].text.strip()
            
            # Extract class information
            class_buttons = form.find_all('button', class_='class-btn')
            
            for button in class_buttons:
                class_text = button.text.strip()
                
                if 'FIRST CLASS' in class_text:
                    # Extract first class seats
                    seats = ''.join(filter(str.isdigit, class_text.split('-')[1] if '-' in class_text else '0'))
                    train_data['first_class_seats'] = int(seats) if seats else 0
                    
                    # Extract first class prices
                    price_section = button.find_next('div', class_='price-section')
                    if price_section:
                        prices = price_section.find_all('span', class_='price')
                        if len(prices) >= 2:
                            train_data['first_class_adult'] = prices[0].text.strip()
                            train_data['first_class_child'] = prices[1].text.strip()
                
                elif 'ECONOMY' in class_text or 'SECOND CLASS' in class_text:
                    # Extract economy seats
                    seats = ''.join(filter(str.isdigit, class_text.split('-')[1] if '-' in class_text else '0'))
                    train_data['economy_seats'] = int(seats) if seats else 0
                    
                    # Extract economy prices
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
    
    return trains


def get_seat_availability(train_data, class_type):
    """
    Get seat count for specific class
    
    Args:
        train_data: Dictionary containing train information
        class_type: 'first' or 'economy'
    
    Returns:
        Number of available seats
    """
    if class_type == 'first':
        return train_data.get('first_class_seats', 0)
    elif class_type == 'economy':
        return train_data.get('economy_seats', 0)
    return 0


if __name__ == "__main__":
    # Test the scraper
    print("=" * 50)
    print("Madaraka Express Train Scraper Bot")
    print("=" * 50)
    
    # Initialize
    csrf_handler = CSRFHandler()
    scraper = TrainScraper(csrf_handler)
    
    # Test search
    find_dates = ["02/01/2026","03/01/2026","04/01/2026","02/01/2026"]
    interested_times = ['3.00','4.30','10.00']
    available_trains = []
    for date in find_dates:
        for time_selection in interested_times:
            html_response = scraper.search_trains(
                schedule_type='express',
                travel_date=date,
                terminal_id=3,
                destination_id=2,
                departure_time=time_selection
            )
            
            if html_response:
                print("\n✓ Successfully retrieved HTML response")
                
                # Check availability
                available = check_trains_available(html_response)
                print(f"✓ Trains available: {available}")
                
                if available:
                    available_trains.append({
                        "travel_date":date,
                        "schedule_type":'express',
                        "departure_time": time_selection
                    })
                    
                    # Parse trains
                    # trains = parse_available_trains(html_response)
                    # print(f"✓ Found {len(trains)} trains")
                    
                    # for i, train in enumerate(trains, 1):
                    #     print(f"\nTrain {i}:")
                    #     print(f"  Name: {train.get('name', 'N/A')}")
                    #     print(f"  Departure: {train.get('departure', 'N/A')}")
                    #     print(f"  Arrival: {train.get('arrival', 'N/A')}")
                    #     print(f"  First Class: {train.get('first_class_seats', 0)} seats")
                    #     print(f"  Economy: {train.get('economy_seats', 0)} seats")
            else:
                print("\n✗ Failed to retrieve HTML response")
    if len(available_trains) > 0:
        for i, train in enumerate(available_trains, 1):
            print(f"  Type: {train.get('schedule_type', 'N/A')}")
            print(f"  Arrival: {train.get('travel_date', 'N/A')}")
            print(f"  First Class: {train.get('first_class_seats', 0)} seats")
            print(f"  Economy: {train.get('economy_seats', 0)} seats")
    else:
        print("No available trains")

