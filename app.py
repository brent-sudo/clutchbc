#!/usr/bin/env python3
"""
APV9T Form Filler - Web App
Upload APV250 PDFs and get filled APV9T forms back.
Supports both digital and scanned PDFs (via OCR).
"""

import os
import re
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from functools import wraps

from flask import Flask, render_template, request, send_file, jsonify, redirect, url_for, session
from pypdf import PdfReader, PdfWriter
from werkzeug.utils import secure_filename
from authlib.integrations.flask_client import OAuth

# OCR support for scanned PDFs and images
try:
    import pytesseract
    from pdf2image import convert_from_path
    from PIL import Image
    # Set Tesseract path based on platform
    import platform
    if platform.system() == 'Darwin':  # macOS
        pytesseract.pytesseract.tesseract_cmd = '/opt/homebrew/bin/tesseract'
        os.environ['PATH'] = '/opt/homebrew/bin:' + os.environ.get('PATH', '')
    else:  # Linux (production)
        pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# Fix for running behind a proxy (Render) - ensures https:// URLs are generated
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Google OAuth setup
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

# Use /tmp for uploads in production (writable), local folder for development
import tempfile
if os.environ.get('RENDER'):
    app.config['UPLOAD_FOLDER'] = Path(tempfile.gettempdir()) / 'apv9t_uploads'
    DB_PATH = Path(tempfile.gettempdir()) / 'apv9t_settings.db'
else:
    app.config['UPLOAD_FOLDER'] = Path(__file__).parent / 'uploads'
    DB_PATH = Path(__file__).parent / 'settings.db'
app.config['UPLOAD_FOLDER'].mkdir(exist_ok=True)

# Path to APV9T template
APV9T_TEMPLATE = Path(__file__).parent / 'APV9T Form.pdf'

# Default settings (used if database is empty)
DEFAULT_SETTINGS = {
    "company_name": "Clutch Technologies Inc",
    "street": "1735-4311 Hazelbridge Way",
    "city": "Richmond",
    "province": "BC",
    "postal_code": "V6X 3L7",
    "dealer_reg": "D50035",
    "allowed_domain": "clutch.ca",
}


def init_db():
    """Initialize SQLite database for settings."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)''')
    conn.commit()
    conn.close()


def get_settings():
    """Get all settings from database."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT key, value FROM settings')
    rows = c.fetchall()
    conn.close()
    settings = DEFAULT_SETTINGS.copy()
    for key, value in rows:
        settings[key] = value
    return settings


def save_settings(new_settings):
    """Save settings to database."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for key, value in new_settings.items():
        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()


def get_purchaser():
    """Get purchaser info from settings."""
    settings = get_settings()
    return {
        "name": settings.get("company_name", ""),
        "street": settings.get("street", ""),
        "city": settings.get("city", ""),
        "province": settings.get("province", ""),
        "postal_code": settings.get("postal_code", ""),
        "dealer_reg": settings.get("dealer_reg", ""),
    }


def login_required(f):
    """Decorator to require login for routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# Legacy PURCHASER variable for compatibility (now uses database)
PURCHASER = DEFAULT_SETTINGS


def extract_apv250_data(file_path: str) -> dict:
    """Extract vehicle and owner data from APV250 PDF or image."""
    import gc  # For memory management
    file_lower = file_path.lower()

    # Handle image files directly with OCR
    if file_lower.endswith(('.jpg', '.jpeg', '.png')):
        if not OCR_AVAILABLE:
            return {}
        from PIL import ImageOps
        image = Image.open(file_path)
        # Fix EXIF orientation (phone photos are often stored rotated)
        image = ImageOps.exif_transpose(image)
        # Resize large images to save memory (max 1500px wide)
        if image.width > 1500:
            ratio = 1500 / image.width
            image = image.resize((1500, int(image.height * ratio)), Image.LANCZOS)

        # Auto-rotation: try different angles and pick the one that finds key fields
        best_text = ""
        for rotation in [0, 90, 270, 180]:  # Try most common rotations first
            rotated = image.rotate(rotation, expand=True) if rotation != 0 else image
            text = pytesseract.image_to_string(rotated)
            # Check if we found key vehicle document fields
            if 'VIN' in text.upper() or 'REGISTRATION' in text.upper() or 'VEHICLE' in text.upper():
                # Found good orientation - do full OCR
                text_psm6 = pytesseract.image_to_string(rotated, config='--psm 6')
                best_text = text + "\n" + text_psm6
                break
            # Keep track of text with most content as fallback
            if len(text) > len(best_text):
                best_text = text
            if rotation != 0:
                del rotated

        text = best_text
        del image  # Free memory
        gc.collect()
    else:
        # Handle PDF files
        reader = PdfReader(file_path)
        text = ""
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"

        # If no text extracted (scanned PDF), try OCR with lower DPI to save memory
        if len(text.strip()) < 50 and OCR_AVAILABLE:
            text = ""
            # Use 150 DPI instead of 300 - still readable, uses 75% less memory
            # Only process first page (registrations are single page)
            images = convert_from_path(file_path, dpi=150, first_page=1, last_page=1)
            for image in images:
                text += pytesseract.image_to_string(image) + "\n"
                text += pytesseract.image_to_string(image, config='--psm 6') + "\n"
                del image  # Free memory immediately
            del images
            gc.collect()

    data = {}

    # Clean up OCR text - normalize whitespace
    text_clean = re.sub(r'[|\[\]{}]', '', text)  # Remove OCR artifacts

    # Registration Number - try multiple patterns
    match = re.search(r'Registration Number[:\s]+(\d{7,8})', text_clean)
    if not match:
        match = re.search(r'Registration Number[:\s]*(\d{7,8})', text_clean)
    if match:
        data['registration_number'] = match.group(1)

    # VIN - be very flexible with OCR errors
    # Look for 17-character sequences that look like VINs
    match = re.search(r'VIN[:\s]*([A-HJ-NPR-Z0-9IOSl]{17})', text_clean, re.IGNORECASE)
    if not match:
        # Try finding any 17-char alphanumeric after VIN
        match = re.search(r'VIN[:\s]*([A-Z0-9]{17})', text_clean, re.IGNORECASE)
    if match:
        vin = match.group(1).upper()
        # Fix common OCR errors
        vin = vin.replace('I', '1').replace('O', '0').replace('S', '5').replace('l', '1')
        data['vin'] = vin

    # Year - look for 4-digit year near "Year" label or in 201x/202x range
    match = re.search(r'Year[:\s]*(\d{4})', text_clean)
    if match:
        data['year'] = match.group(1)

    # Make - be flexible with OCR variations
    make_patterns = [
        r'Make[:\s]*([A-Za-z]+)',
        r'Make\s+([A-Za-z]+)',
        r'[Mm]ake[:\s]*([A-Za-z]{3,})',
    ]
    for pattern in make_patterns:
        match = re.search(pattern, text_clean)
        if match:
            make = match.group(1).strip()
            # Fix common OCR errors for makes
            make_fixes = {
                'Cadlhae': 'CADILLAC', 'Cadllac': 'CADILLAC', 'Cadlllac': 'CADILLAC',
                'Chevroiet': 'CHEVROLET', 'Toyola': 'TOYOTA', 'Honds': 'HONDA',
            }
            make = make_fixes.get(make, make).upper()
            if len(make) >= 2:
                data['make'] = make
                break

    # Model - handle alphanumeric models
    match = re.search(r'Model[:\s]*([A-Za-z0-9\-]+)', text_clean)
    if match:
        data['model'] = match.group(1).upper()

    # Body Style - try multiple patterns (handle OCR errors like "Body Sle" for "Body Style")
    body_patterns = [
        r'Body Styl?e?[:\s]*([A-Za-z0-9 ]+?)(?:\n|VIC|Colour|$)',
        r'Body Sl[ea]?[:\s]*(\d+ ?Door ?[A-Za-z]+)',  # "Body Sle 4 Door Sea"
        r'(\d+ ?Door ?(?:Sedan|Coupe|Hatchback|SUV|Truck|Van|Wagon|Conv|Sea|Sed))',
    ]
    for pattern in body_patterns:
        match = re.search(pattern, text_clean, re.IGNORECASE)
        if match:
            body = match.group(1).strip().upper()
            # Normalize to standard body types
            body_lower = body.lower()
            if any(x in body_lower for x in ['sedan', 'sea', 'sed', '4 door', '4door']):
                data['body_style'] = 'SEDAN'
            elif any(x in body_lower for x in ['suv', 'sport util', 'crossover']):
                data['body_style'] = 'SUV'
            elif any(x in body_lower for x in ['truck', 'pickup', 'pick up', 'pick-up']):
                data['body_style'] = 'TRUCK'
            elif any(x in body_lower for x in ['coupe', 'cpe', '2 door', '2door']):
                data['body_style'] = 'COUPE'
            elif any(x in body_lower for x in ['wagon', 'wgn', 'estate']):
                data['body_style'] = 'WAGON'
            elif any(x in body_lower for x in ['convert', 'conv', 'cabriolet', 'roadster']):
                data['body_style'] = 'CONVERTIBLE'
            elif any(x in body_lower for x in ['hatch', 'hatchback', '5 door', '5door']):
                data['body_style'] = 'HATCHBACK'
            elif any(x in body_lower for x in ['van', 'minivan']):
                data['body_style'] = 'VAN'
            else:
                data['body_style'] = body  # Keep original if no match
            break
    # Also try VIC code as fallback
    if 'body_style' not in data:
        match = re.search(r'VIC[:\s]*([A-Z0-9]{4,8})', text_clean)
        if match:
            data['body_style'] = match.group(1).strip().upper()

    # Colour - be flexible
    match = re.search(r'Colour[:\s]*([A-Za-z]+)', text_clean)
    if match:
        data['colour'] = match.group(1).upper()

    # Fuel Type
    match = re.search(r'Fuel Type[:\s]*([A-Za-z]+)', text_clean)
    if match:
        fuel = match.group(1).upper()
        fuel_codes = {
            'GASOLINE': 'G', 'GAS': 'G', 'DIESEL': 'D', 'ELECTRIC': 'E',
            'HYBRID': 'L', 'PROPANE': 'P', 'NATURAL': 'N',
        }
        data['fuel_code'] = fuel_codes.get(fuel, 'G')
        data['fuel_type'] = fuel
    else:
        data['fuel_code'] = 'G'

    # Net Weight
    match = re.search(r'Net Weight[:\s\(kg\)]*([0-9,]+)', text_clean)
    if match:
        data['net_weight'] = match.group(1).replace(',', '')

    # Number of owners
    num_owners_match = re.search(r'Number of Owners[:\s]*(\d+)', text_clean)
    num_owners = int(num_owners_match.group(1)) if num_owners_match else 1

    # Owner Names - look for LASTNAME FIRSTNAME patterns after "Registered Owner" or "Owner"
    # Pattern for names in ALL CAPS
    owner_section = re.search(r'Registered Owner.*?(?=This Certificate|Number of Owners|$)', text_clean, re.DOTALL | re.IGNORECASE)
    if owner_section:
        section = owner_section.group(0)
        # Find names: ALL CAPS words that look like names (LASTNAME FIRSTNAME)
        name_matches = re.findall(r'\n([A-Z]{2,}(?:\s+[A-Z]{2,})+)', section)
        if name_matches:
            data['owner_name'] = name_matches[0].strip()
            if num_owners > 1 and len(name_matches) > 1:
                data['owner_name_2'] = name_matches[1].strip()

    # If no owner found, try alternative pattern
    if 'owner_name' not in data:
        matches = re.findall(r'(?:SACHDEV|SINGH|KUMAR|KAUR|GILL|DHILLON|GREWAL|SANDHU|SIDHU|BRAR|MANN|CHEEMA|DHALIWAL|BAJWA|JOHAL|KHANNA|SHARMA|PATEL|WONG|CHEN|LEE|WANG|LI|ZHANG|LIU|YANG|HUANG|WU|ZHOU|XU|MA|ZHU|HU|LIN|GUO|SMITH|JOHNSON|WILLIAMS|BROWN|JONES|MILLER|DAVIS|WILSON|ANDERSON|TAYLOR|THOMAS|MOORE|MARTIN|JACKSON|WHITE|HARRIS|CLARK|LEWIS|WALKER|HALL|YOUNG|KING|WRIGHT|HILL|SCOTT|GREEN|ADAMS|BAKER|NELSON|CARTER|MITCHELL|ROBERTS|TURNER|PHILLIPS|CAMPBELL|PARKER|EVANS|EDWARDS|COLLINS|STEWART|MORRIS|ROGERS|REED|COOK|MORGAN|BELL|MURPHY|BAILEY|RIVERA|COOPER|RICHARDSON|COX|HOWARD|WARD|TORRES|PETERSON|GRAY|RAMIREZ|JAMES|WATSON|BROOKS|KELLY|SANDERS|PRICE|BENNETT|WOOD|BARNES|ROSS|HENDERSON|COLEMAN|JENKINS|PERRY|POWELL|LONG|PATTERSON|HUGHES|FLORES|WASHINGTON|BUTLER|SIMMONS|FOSTER|GONZALES|BRYANT|ALEXANDER|RUSSELL|GRIFFIN|DIAZ|HAYES)[A-Z\s]+', text_clean)
        if matches:
            data['owner_name'] = matches[0].strip()
            if num_owners > 1 and len(matches) > 1:
                data['owner_name_2'] = matches[1].strip()

    # Owner Address - try multiple patterns
    # First try "Location Address" format from digital PDFs: "Location Address 1: 305-3142 ST. JOHNS ST, PORT MOODY"
    loc_match = re.search(r'Location Address \d?:?\s*(\d+[- ]\d+[^,\n]+|[^,\n]+(?:ST|AVE|RD|DR|BLVD|WAY|CRES|PL|CT))', text_clean, re.IGNORECASE)
    if loc_match:
        street = loc_match.group(1).strip().upper()
        if len(street) > 5 and len(street) < 60 and re.search(r'\d', street):
            data['owner_street'] = street

    # If not found, try other patterns (single line only - no \s, use space)
    if 'owner_street' not in data:
        addr_patterns = [
            # Unit-Number format with periods: 305-3142 ST. JOHNS ST
            r'(\d{1,5}[- ]\d{1,5} [A-Z\.]+[A-Z\. ]* (?:ST|AVE|RD|DR|BLVD|WAY|CRES|PL|CT|LANE|CRT))',
            # Standard with periods: 1234 ST. JAMES ST
            r'(\d{1,5} [A-Z\.]+[A-Z\. ]* (?:ST|AVE|RD|DR|BLVD|WAY|CRES|PL|CT|LANE|CRT|STREET|AVENUE|ROAD|DRIVE))',
            # Unit-Number format: 1234-5678 STREET NAME ST
            r'(\d{1,5}[- ]\d{1,5} [A-Z]+ (?:ST|AVE|RD|DR|BLVD|WAY|CRES|PL|CT|LANE|CRT|STREET|AVENUE|ROAD|DRIVE))',
            # Standard: 1234 STREET NAME ST
            r'(\d{1,5} [A-Z]+[A-Z ]* (?:ST|AVE|RD|DR|BLVD|WAY|CRES|PL|CT|LANE|CRT|STREET|AVENUE|ROAD|DRIVE))',
        ]
        for pattern in addr_patterns:
            addr_match = re.search(pattern, text_clean, re.IGNORECASE)
            if addr_match:
                street = addr_match.group(1).strip().upper()
                # Clean up any OCR junk at the end (random letters, but not valid suffixes)
                street = re.sub(r' (?!ST|RD|DR|PL|CT|AVE|WAY)[A-Z]{1,2}$', '', street)
                if len(street) > 5 and len(street) < 60:  # Valid street address
                    data['owner_street'] = street
                    break

    # City and postal - look for CITY BC POSTAL pattern (allow OCR errors in postal)
    bc_cities = r'(ABBOTSFORD|VANCOUVER|RICHMOND|BURNABY|SURREY|COQUITLAM|LANGLEY|VICTORIA|KELOWNA|KAMLOOPS|NANAIMO|CHILLIWACK|MAPLE RIDGE|NEW WESTMINSTER|NORTH VANCOUVER|WEST VANCOUVER|DELTA|PORT COQUITLAM|MISSION|WHITE ROCK|PENTICTON|VERNON|COURTENAY|PORT MOODY|PITT MEADOWS)'
    postal_match = re.search(bc_cities + r'\s+BC\s+([A-Z0-9]{3}\s*[A-Z0-9]{3})', text_clean, re.IGNORECASE)
    if postal_match:
        data['owner_city'] = postal_match.group(1).strip().upper()
        data['owner_province'] = 'BC'
        # Fix postal code OCR errors: numbers that should be letters and vice versa
        postal = postal_match.group(2).strip().upper().replace(' ', '')
        # BC postal codes are: Letter-Digit-Letter Digit-Letter-Digit (V#X #X#)
        # Fix common OCR substitutions
        if len(postal) == 6:
            fixed = ''
            for i, c in enumerate(postal):
                if i in [0, 2, 4]:  # Should be letter
                    if c == '2': fixed += 'Z'
                    elif c == '5': fixed += 'S'
                    elif c == '0': fixed += 'O'
                    elif c == '1': fixed += 'I'
                    else: fixed += c
                else:  # Should be digit
                    if c == 'Z': fixed += '2'
                    elif c == 'S': fixed += '5'
                    elif c == 'O': fixed += '0'
                    elif c == 'I': fixed += '1'
                    elif c == 'l': fixed += '1'
                    else: fixed += c
            postal = fixed[:3] + ' ' + fixed[3:]
        data['owner_postal'] = postal

    return data


def fill_apv9t(vehicle_data: dict, output_path: str, sale_date: str = None, form_data: dict = None) -> None:
    """Fill APV9T form with extracted vehicle data."""
    # Get purchaser info from database settings
    purchaser = get_purchaser()

    reader = PdfReader(str(APV9T_TEMPLATE))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)

    if sale_date:
        # Convert from YYYY-MM-DD (HTML date input) to DD-MM-YYYY
        parts = sale_date.split('-')
        today = f"{parts[2]}-{parts[1]}-{parts[0]}"
    else:
        today = datetime.now().strftime("%d-%m-%Y")

    field_values = {
        # Vehicle Description
        'registrationNumber': vehicle_data.get('registration_number', ''),
        'colour': vehicle_data.get('colour', ''),
        'fuel': vehicle_data.get('fuel_code', 'G'),
        'modelYear': vehicle_data.get('year', ''),
        'make': vehicle_data.get('make', ''),
        'model': vehicle_data.get('model', ''),
        'bodyStyle': vehicle_data.get('body_style', ''),
        'vin': vehicle_data.get('vin', ''),
        'netWeight': vehicle_data.get('net_weight', ''),
        'dateOfSale': today,

        # Seller Information
        'sellerNameLine1': vehicle_data.get('owner_name', ''),
        'sellerNameLine2': vehicle_data.get('owner_name_2', ''),
        'sellerAddressLine1': vehicle_data.get('owner_street', ''),
        'sellerAddressLine2': '',
        'sellerAddressLine3': vehicle_data.get('owner_city', ''),
        'province1': vehicle_data.get('owner_province', 'BC'),
        'sellerPostalcode': vehicle_data.get('owner_postal', ''),

        # Purchaser Information (from settings)
        'purchaserNameLine1': purchaser['name'],
        'purchaserAddressLine1': purchaser['street'],
        'purchaserAddressLine2': purchaser['city'],
        'province2': purchaser['province'],
        'purchaserPostalcode': purchaser['postal_code'],
        'dealerRegNo': purchaser['dealer_reg'],

        # Duplicate fields for other copies
        'registrationNumberA': vehicle_data.get('registration_number', ''),
        'colourA': vehicle_data.get('colour', ''),
        'fuelA': vehicle_data.get('fuel_code', 'G'),
        'modelYearA': vehicle_data.get('year', ''),
        'makeA': vehicle_data.get('make', ''),
        'modelA': vehicle_data.get('model', ''),
        'bodyStyleA': vehicle_data.get('body_style', ''),
        'vinA': vehicle_data.get('vin', ''),
        'netWeightA': vehicle_data.get('net_weight', ''),
        'dateOfSaleA': today,
        'sellerNameLine1A': vehicle_data.get('owner_name', ''),
        'sellerNameLine2A': vehicle_data.get('owner_name_2', ''),
        'sellerAddressLine1A': vehicle_data.get('owner_street', ''),
        'sellerAddressLine2A': '',
        'sellerAddressLine3A': vehicle_data.get('owner_city', ''),
        'province1A': vehicle_data.get('owner_province', 'BC'),
        'sellerPostalcodeA': vehicle_data.get('owner_postal', ''),
        'purchaserNameLine1A': purchaser['name'],
        'purchaserAddressLine1A': purchaser['street'],
        'purchaserAddressLine2A': purchaser['city'],
        'province2A': purchaser['province'],
        'purchaserPostalcodeA': purchaser['postal_code'],
        'dealerRegNoA': purchaser['dealer_reg'],
    }

    # Add optional form fields if provided
    if form_data:
        # Selling price
        if form_data.get('selling_price'):
            field_values['sellingPrice'] = form_data['selling_price']
            field_values['sellingPriceA'] = form_data['selling_price']

        # Odometer reading
        if form_data.get('odometer'):
            field_values['odometerReading'] = form_data['odometer']
            field_values['odometerReadingA'] = form_data['odometer']

        # Km/Miles radio - pypdf uses /Yes for checked
        if form_data.get('odometer_unit') == 'miles':
            field_values['kmMiles'] = '/1'  # miles option
            field_values['kmMilesA'] = '/1'
        elif form_data.get('odometer'):
            field_values['kmMiles'] = '/0'  # km option
            field_values['kmMilesA'] = '/0'

        # Previous vehicle history checkboxes
        prev_history = form_data.getlist('prev_history') if hasattr(form_data, 'getlist') else form_data.get('prev_history', [])
        if 'none' in prev_history:
            field_values['previousVehicleNoneCheck'] = '/Yes'
            field_values['previousVehicleNoneCheckA'] = '/Yes'
        if 'rebuilt' in prev_history:
            field_values['previousVehicleCheck1'] = '/Yes'
            field_values['previousVehicleCheck1A'] = '/Yes'
        if 'salvage' in prev_history:
            field_values['previousVehicleCheck2'] = '/Yes'
            field_values['previousVehicleCheck2A'] = '/Yes'
        if 'nonrepairable' in prev_history:
            field_values['previousVehicleCheck3'] = '/Yes'
            field_values['previousVehicleCheck3A'] = '/Yes'
        if 'irreparable' in prev_history:
            field_values['previousVehicleCheck4'] = '/Yes'
            field_values['previousVehicleCheck4A'] = '/Yes'

        # Previously registered outside BC
        if form_data.get('outside_bc') == 'yes':
            field_values['vehiclePreviouslyRegisteredOutsideRadio'] = '/0'
            field_values['vehiclePreviouslyRegisteredOutsideRadioA'] = '/0'
        elif form_data.get('outside_bc') == 'no':
            field_values['vehiclePreviouslyRegisteredOutsideRadio'] = '/1'
            field_values['vehiclePreviouslyRegisteredOutsideRadioA'] = '/1'

        # New vehicle damage exceeds 20%
        if form_data.get('new_damage_20') == 'yes':
            field_values['newVehicleWhereDamageRadio'] = '/0'
            field_values['newVehicleWhereDamageRadioA'] = '/0'
        elif form_data.get('new_damage_20') == 'no':
            field_values['newVehicleWhereDamageRadio'] = '/1'
            field_values['newVehicleWhereDamageRadioA'] = '/1'

        # Used vehicle over $2,000 damage
        if form_data.get('used_damage_2k') == 'yes':
            field_values['usedVehicleDamageRadio'] = '/0'
            field_values['usedVehicleDamageRadioA'] = '/0'
        elif form_data.get('used_damage_2k') == 'no':
            field_values['usedVehicleDamageRadio'] = '/1'
            field_values['usedVehicleDamageRadioA'] = '/1'

    writer.update_page_form_field_values(writer.pages[0], field_values)
    for page in writer.pages:
        try:
            writer.update_page_form_field_values(page, field_values)
        except Exception:
            pass

    with open(output_path, 'wb') as f:
        writer.write(f)


@app.route('/')
@login_required
def index():
    return render_template('index.html')


@app.route('/login')
def login():
    """Show login page."""
    if 'user' in session:
        return redirect(url_for('index'))
    error = request.args.get('error')
    return render_template('login.html', error=error)


@app.route('/login/google')
def login_google():
    """Initiate Google OAuth login."""
    # Check if OAuth is configured
    if not os.environ.get('GOOGLE_CLIENT_ID'):
        # Development mode - auto login
        session['user'] = {'email': 'dev@clutch.ca', 'name': 'Developer'}
        return redirect(url_for('index'))
    redirect_uri = url_for('auth_callback', _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route('/auth/callback')
def auth_callback():
    """Handle Google OAuth callback."""
    try:
        token = google.authorize_access_token()
        user_info = token.get('userinfo')
        if not user_info:
            user_info = google.get('https://openidconnect.googleapis.com/v1/userinfo').json()

        email = user_info.get('email', '')
        settings = get_settings()
        allowed_domain = settings.get('allowed_domain', 'clutch.ca')

        # Check email domain
        if not email.endswith('@' + allowed_domain):
            return redirect(url_for('login', error=f'Only @{allowed_domain} accounts can sign in'))

        session['user'] = {
            'email': email,
            'name': user_info.get('name', email),
        }
        return redirect(url_for('index'))
    except Exception as e:
        return redirect(url_for('login', error='Login failed. Please try again.'))


@app.route('/logout')
def logout():
    """Log out user."""
    session.pop('user', None)
    return redirect(url_for('login'))


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """Settings page for dealer info."""
    success = False
    if request.method == 'POST':
        new_settings = {
            'company_name': request.form.get('company_name', ''),
            'street': request.form.get('street', ''),
            'city': request.form.get('city', ''),
            'province': request.form.get('province', ''),
            'postal_code': request.form.get('postal_code', ''),
            'dealer_reg': request.form.get('dealer_reg', ''),
            'allowed_domain': request.form.get('allowed_domain', 'clutch.ca'),
        }
        save_settings(new_settings)
        success = True

    current_settings = get_settings()
    user_email = session.get('user', {}).get('email', '')
    return render_template('settings.html', settings=current_settings, user_email=user_email, success=success)


@app.route('/health')
def health():
    """Diagnostic endpoint to check system status."""
    import traceback
    status = {
        'app': 'running',
        'ocr_available': OCR_AVAILABLE,
        'upload_folder': str(app.config['UPLOAD_FOLDER']),
        'template_exists': APV9T_TEMPLATE.exists(),
        'template_path': str(APV9T_TEMPLATE),
    }

    # Test if upload folder is writable
    try:
        test_file = app.config['UPLOAD_FOLDER'] / 'test_write.txt'
        test_file.write_text('test')
        test_file.unlink()
        status['upload_writable'] = True
    except Exception as e:
        status['upload_writable'] = False
        status['upload_error'] = str(e)

    return jsonify(status)


@app.route('/upload', methods=['POST'])
@login_required
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    allowed_extensions = ('.pdf', '.jpg', '.jpeg', '.png')
    if not file.filename.lower().endswith(allowed_extensions):
        return jsonify({'error': 'Please upload a PDF or image file'}), 400

    # Save uploaded file
    filename = secure_filename(file.filename)
    upload_path = app.config['UPLOAD_FOLDER'] / filename

    try:
        file.save(str(upload_path))
    except Exception as e:
        return jsonify({'error': f'Failed to save file: {str(e)}'}), 500

    try:
        # Extract data from APV250
        vehicle_data = extract_apv250_data(str(upload_path))

        if not vehicle_data.get('vin'):
            return jsonify({'error': 'Could not extract vehicle data. Is this a Vehicle Ownership document?'}), 400

        # Generate output filename
        vin = vehicle_data.get('vin', 'unknown')
        output_filename = f"APV9T_Filled_{vin[-6:]}.pdf"
        output_path = app.config['UPLOAD_FOLDER'] / output_filename

        # Fill the form
        fill_apv9t(vehicle_data, str(output_path))

        # Clean up uploaded file
        os.remove(str(upload_path))

        return jsonify({
            'success': True,
            'data': vehicle_data,
            'download_url': f'/download/{output_filename}'
        })

    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'details': traceback.format_exc()}), 500


@app.route('/download/<filename>')
@login_required
def download(filename):
    file_path = app.config['UPLOAD_FOLDER'] / secure_filename(filename)
    if file_path.exists():
        return send_file(
            str(file_path),
            as_attachment=True,
            download_name=filename
        )
    return jsonify({'error': 'File not found'}), 404


@app.route('/update-pdf', methods=['POST'])
@login_required
def update_pdf():
    """Update an existing PDF with manual field values."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    download_url = data.get('download_url', '')
    manual_fields = data.get('manual_fields', {})

    # Extract filename from download URL
    filename = download_url.split('/')[-1]
    file_path = app.config['UPLOAD_FOLDER'] / secure_filename(filename)

    if not file_path.exists():
        return jsonify({'error': 'PDF not found'}), 404

    try:
        # Read the existing PDF and update fields
        reader = PdfReader(str(file_path))
        writer = PdfWriter()
        writer.clone_document_from_reader(reader)

        # Map manual fields to PDF field names
        field_mapping = {
            'vin': ['vin', 'vinA'],
            'registration_number': ['registrationNumber', 'registrationNumberA'],
            'year': ['modelYear', 'modelYearA'],
            'make': ['make', 'makeA'],
            'model': ['model', 'modelA'],
            'colour': ['colour', 'colourA'],
            'body_style': ['bodyStyle', 'bodyStyleA'],
            'net_weight': ['netWeight', 'netWeightA'],
            'owner_name': ['sellerNameLine1', 'sellerNameLine1A'],
            'owner_street': ['sellerAddressLine1', 'sellerAddressLine1A'],
            'owner_city': ['sellerAddressLine3', 'sellerAddressLine3A'],
            'owner_postal': ['sellerPostalcode', 'sellerPostalcodeA'],
        }

        field_values = {}
        for field_key, value in manual_fields.items():
            if field_key in field_mapping:
                for pdf_field in field_mapping[field_key]:
                    field_values[pdf_field] = value.upper() if field_key not in ['vin', 'registration_number'] else value

        # Update all pages
        for page in writer.pages:
            try:
                writer.update_page_form_field_values(page, field_values)
            except Exception:
                pass

        # Save updated PDF
        with open(str(file_path), 'wb') as f:
            writer.write(f)

        return jsonify({
            'success': True,
            'download_url': download_url
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/process-check', methods=['POST'])
@login_required
def process_check():
    """Process form upload and return JSON with warnings for missing fields."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    allowed_extensions = ('.pdf', '.jpg', '.jpeg', '.png')
    if not file.filename.lower().endswith(allowed_extensions):
        return jsonify({'error': 'Please upload a PDF or image file'}), 400

    # Save uploaded file
    filename = secure_filename(file.filename)
    upload_path = app.config['UPLOAD_FOLDER'] / filename

    try:
        file.save(str(upload_path))
    except Exception as e:
        return jsonify({'error': f'Failed to save file: {str(e)}'}), 500

    try:
        # Extract data
        vehicle_data = extract_apv250_data(str(upload_path))

        if not vehicle_data.get('vin') and not vehicle_data.get('registration_number'):
            os.remove(str(upload_path))
            return jsonify({'error': 'Could not extract vehicle data. Is this a Vehicle Ownership document?'}), 400

        # Check for missing/important fields
        missing_fields = []
        field_checks = {
            'vin': 'VIN',
            'registration_number': 'Registration Number',
            'year': 'Year',
            'make': 'Make',
            'model': 'Model',
            'colour': 'Colour',
            'owner_name': 'Owner Name',
            'owner_street': 'Owner Street Address',
            'owner_city': 'Owner City',
            'owner_postal': 'Owner Postal Code',
        }

        for field, label in field_checks.items():
            if not vehicle_data.get(field):
                missing_fields.append(label)

        # Generate output filename
        vin = vehicle_data.get('vin', 'unknown')
        output_filename = f"APV9T_Filled_{vin[-6:] if len(vin) >= 6 else vin}.pdf"
        output_path = app.config['UPLOAD_FOLDER'] / output_filename

        # Get sale date from form
        sale_date = request.form.get('sale_date')

        # Fill the form
        fill_apv9t(vehicle_data, str(output_path), sale_date, request.form)

        # Clean up uploaded file
        os.remove(str(upload_path))

        return jsonify({
            'success': True,
            'data': vehicle_data,
            'missing_fields': missing_fields,
            'download_url': f'/download/{output_filename}'
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/process', methods=['POST'])
@login_required
def process():
    """Process form upload and return filled PDF directly."""
    if 'file' not in request.files:
        return "No file uploaded", 400

    file = request.files['file']
    if file.filename == '':
        return "No file selected", 400

    allowed_extensions = ('.pdf', '.jpg', '.jpeg', '.png')
    if not file.filename.lower().endswith(allowed_extensions):
        return "Please upload a PDF or image file (JPG, PNG)", 400

    # Save uploaded file
    filename = secure_filename(file.filename)
    upload_path = app.config['UPLOAD_FOLDER'] / filename

    try:
        file.save(str(upload_path))
    except Exception as e:
        return jsonify({'error': f'Failed to save file: {str(e)}'}), 500

    try:
        # Extract data from APV250
        vehicle_data = extract_apv250_data(str(upload_path))

        if not vehicle_data.get('vin'):
            os.remove(str(upload_path))
            return "Could not extract vehicle data. Is this a Vehicle Ownership document?", 400

        # Generate output filename
        vin = vehicle_data.get('vin', 'unknown')
        output_filename = f"APV9T_Filled_{vin[-6:]}.pdf"
        output_path = app.config['UPLOAD_FOLDER'] / output_filename

        # Get sale date from form (if provided)
        sale_date = request.form.get('sale_date')

        # Fill the form with optional fields
        fill_apv9t(vehicle_data, str(output_path), sale_date, request.form)

        # Clean up uploaded file
        os.remove(str(upload_path))

        # Return the filled PDF directly
        return send_file(
            str(output_path),
            as_attachment=True,
            download_name=output_filename
        )

    except Exception as e:
        return f"Error: {str(e)}", 500


if __name__ == '__main__':
    print("\n" + "="*50)
    print("  APV9T Form Filler")
    print("  Open http://127.0.0.1:5000 in your browser")
    print("="*50 + "\n")
    app.run(host='127.0.0.1', port=5000, debug=False)
