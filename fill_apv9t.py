#!/usr/bin/env python3
"""
APV9T Form Filler
Extracts vehicle data from BC APV250 (Vehicle Registration) PDF
and fills in APV9T Transfer/Tax Form for Clutch Technologies Inc.
"""

import re
import sys
from datetime import datetime
from pathlib import Path
from pypdf import PdfReader, PdfWriter


# Purchaser info (always Clutch Technologies Inc)
PURCHASER = {
    "name": "Clutch Technologies Inc",
    "street": "1735-4311 Hazelbridge Way",
    "city": "Richmond",
    "province": "BC",
    "postal_code": "V6X 3L7",
    "dealer_reg": "D50035",
}


def extract_apv250_data(pdf_path: str) -> dict:
    """Extract vehicle and owner data from APV250 PDF text."""
    reader = PdfReader(pdf_path)

    # Combine text from all pages
    text = ""
    for page in reader.pages:
        text += page.extract_text() + "\n"

    data = {}

    # Registration Number
    match = re.search(r'Registration Number[:\s]+(\d+)', text)
    if match:
        data['registration_number'] = match.group(1)

    # VIN
    match = re.search(r'VIN[:\s]+([A-HJ-NPR-Z0-9]{17})', text)
    if match:
        data['vin'] = match.group(1)

    # Year
    match = re.search(r'Year[:\s]+(\d{4})', text)
    if match:
        data['year'] = match.group(1)

    # Make
    match = re.search(r'Make[:\s]+([A-Za-z/]+)', text)
    if match:
        data['make'] = match.group(1).split('/')[0].upper()

    # Model
    match = re.search(r'Model[:\s]+([A-Za-z0-9]+)', text)
    if match:
        data['model'] = match.group(1).upper()

    # Body Style
    match = re.search(r'Body Style[:\s]+([A-Za-z0-9 ]+?)(?:\n|VIC)', text)
    if match:
        data['body_style'] = match.group(1).strip().upper()

    # Colour
    match = re.search(r'Colour[:\s]+([A-Za-z]+)', text)
    if match:
        data['colour'] = match.group(1).upper()

    # Fuel Type
    match = re.search(r'Fuel Type[:\s]+([A-Za-z]+)', text)
    if match:
        fuel = match.group(1).upper()
        # Convert to fuel code
        fuel_codes = {
            'GASOLINE': 'G',
            'DIESEL': 'D',
            'ELECTRIC': 'E',
            'HYBRID': 'L',
            'PROPANE': 'P',
            'NATURAL': 'N',
        }
        data['fuel_code'] = fuel_codes.get(fuel, 'G')
        data['fuel_type'] = fuel

    # Net Weight
    match = re.search(r'Net Weight \(kg\)[:\s]+([\d,]+)', text)
    if match:
        data['net_weight'] = match.group(1).replace(',', '')

    # Owner Name (format: SURNAME FIRSTNAME)
    # Check for multiple owners (Number of Owners field)
    num_owners_match = re.search(r'Number of Owners[:\s]+(\d+)', text)
    num_owners = int(num_owners_match.group(1)) if num_owners_match else 1

    # Extract owner names - look for names after "Registered Owner" or "Owner"
    owner_matches = re.findall(r'(?:Registered Owner|Owner)\s*\n([A-Z]+(?:\s+[A-Z]+)+)\n', text)
    if owner_matches:
        data['owner_name'] = owner_matches[0].strip()
        # If there are multiple owners, try to find second name
        if num_owners > 1 and len(owner_matches) > 1:
            data['owner_name_2'] = owner_matches[1].strip()
        elif num_owners > 1:
            # Try alternative pattern for co-owner
            coowner_match = re.search(r'Owner\s*\n[A-Z\s]+\n([A-Z]+(?:\s+[A-Z]+)+)\n', text)
            if coowner_match and coowner_match.group(1).strip() != data['owner_name']:
                data['owner_name_2'] = coowner_match.group(1).strip()

    # Owner Address - look for the pattern after owner name
    # Try to find street address
    match = re.search(r'Owner\s*\n[A-Z\s]+\n([\d\-]+[A-Z0-9\s\.]+(?:ST|AVE|RD|DR|BLVD|WAY|CRES|PL|CT|LANE|CRT)\.?)\s*\n([A-Z\s]+)\s+BC\s+([A-Z]\d[A-Z]\s?\d[A-Z]\d)', text, re.IGNORECASE)
    if match:
        data['owner_street'] = match.group(1).strip()
        data['owner_city'] = match.group(2).strip()
        data['owner_province'] = 'BC'
        data['owner_postal'] = match.group(3).strip()
    else:
        # Alternative pattern
        match = re.search(r'(\d+[\-\d]*\s+[A-Z0-9\s\.]+(?:ST|AVE|RD|DR|BLVD|WAY|CRES|PL|CT|LANE|CRT)\.?)\s*\n([A-Z\s]+)\s+BC\s+([A-Z]\d[A-Z]\s?\d[A-Z]\d)', text, re.IGNORECASE)
        if match:
            data['owner_street'] = match.group(1).strip()
            data['owner_city'] = match.group(2).strip()
            data['owner_province'] = 'BC'
            data['owner_postal'] = match.group(3).strip()

    return data


def fill_apv9t(template_path: str, output_path: str, vehicle_data: dict) -> None:
    """Fill APV9T form with extracted vehicle data."""
    reader = PdfReader(template_path)
    writer = PdfWriter()

    # Clone the PDF
    writer.clone_document_from_reader(reader)

    # Get today's date in dd-mm-yyyy format
    today = datetime.now().strftime("%d-%m-%Y")

    # Prepare field values
    # Note: Some fields appear twice (with and without 'A' suffix) for different copies
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

        # Date of Sale
        'dateOfSale': today,

        # Seller Information
        'sellerNameLine1': vehicle_data.get('owner_name', ''),
        'sellerNameLine2': vehicle_data.get('owner_name_2', ''),  # Co-owner if exists
        'sellerAddressLine1': vehicle_data.get('owner_street', ''),
        'sellerAddressLine2': '',  # Leave blank
        'sellerAddressLine3': vehicle_data.get('owner_city', ''),
        'province1': vehicle_data.get('owner_province', 'BC'),
        'sellerPostalcode': vehicle_data.get('owner_postal', ''),

        # Purchaser Information (Clutch Technologies)
        'purchaserNameLine1': PURCHASER['name'],
        'purchaserAddressLine1': PURCHASER['street'],
        'purchaserAddressLine2': PURCHASER['city'],
        'province2': PURCHASER['province'],
        'purchaserPostalcode': PURCHASER['postal_code'],
        'dealerRegNo': PURCHASER['dealer_reg'],

        # Duplicate fields for other copies (with 'A' suffix)
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
        'sellerNameLine2A': vehicle_data.get('owner_name_2', ''),  # Co-owner if exists
        'sellerAddressLine1A': vehicle_data.get('owner_street', ''),
        'sellerAddressLine2A': '',  # Leave blank
        'sellerAddressLine3A': vehicle_data.get('owner_city', ''),
        'province1A': vehicle_data.get('owner_province', 'BC'),
        'sellerPostalcodeA': vehicle_data.get('owner_postal', ''),
        'purchaserNameLine1A': PURCHASER['name'],
        'purchaserAddressLine1A': PURCHASER['street'],
        'purchaserAddressLine2A': PURCHASER['city'],
        'province2A': PURCHASER['province'],
        'purchaserPostalcodeA': PURCHASER['postal_code'],
        'dealerRegNoA': PURCHASER['dealer_reg'],
    }

    # Update form fields
    writer.update_page_form_field_values(writer.pages[0], field_values)

    # Try to update all pages (multi-page form)
    for i, page in enumerate(writer.pages):
        try:
            writer.update_page_form_field_values(page, field_values)
        except Exception:
            pass

    # Save filled PDF
    with open(output_path, 'wb') as f:
        writer.write(f)


def main():
    # Find files in current directory
    script_dir = Path(__file__).parent

    # Look for APV250 file (ownership/registration)
    apv250_files = list(script_dir.glob('*APV250*')) + \
                   list(script_dir.glob('*Proof*Insurance*')) + \
                   list(script_dir.glob('*Registration*'))

    # Look for APV9T template
    apv9t_files = list(script_dir.glob('*APV9T*'))

    if len(sys.argv) >= 3:
        apv250_path = sys.argv[1]
        apv9t_path = sys.argv[2]
    elif apv250_files and apv9t_files:
        # Use the first non-APV9T PDF as APV250
        apv250_path = str([f for f in apv250_files if 'APV9T' not in f.name][0]) if apv250_files else None
        apv9t_path = str([f for f in apv9t_files if f.name.endswith('.pdf')][0])
    else:
        print("Usage: python fill_apv9t.py [apv250_path] [apv9t_template_path]")
        print("\nOr place files in same directory:")
        print("  - APV250/ownership PDF")
        print("  - APV9T Form.pdf")
        sys.exit(1)

    # Auto-detect APV250 if not found
    if not apv250_path:
        pdf_files = [f for f in script_dir.glob('*.pdf') if 'APV9T' not in f.name]
        if pdf_files:
            apv250_path = str(pdf_files[0])
        else:
            print("Error: No APV250/ownership PDF found")
            sys.exit(1)

    print(f"Reading ownership data from: {apv250_path}")
    print(f"Using APV9T template: {apv9t_path}")

    # Extract data from APV250
    vehicle_data = extract_apv250_data(apv250_path)

    print("\nExtracted Vehicle Data:")
    print("-" * 40)
    for key, value in vehicle_data.items():
        print(f"  {key}: {value}")

    # Generate output filename
    vin = vehicle_data.get('vin', 'unknown')
    output_path = script_dir / f"APV9T_Filled_{vin[-6:]}.pdf"

    # Fill the form
    print(f"\nFilling APV9T form...")
    fill_apv9t(apv9t_path, str(output_path), vehicle_data)

    print(f"\nSaved filled form to: {output_path}")
    print("\nPurchaser (pre-filled):")
    print(f"  {PURCHASER['name']}")
    print(f"  {PURCHASER['street']}")
    print(f"  {PURCHASER['city']} {PURCHASER['province']} {PURCHASER['postal_code']}")
    print(f"  Dealer Reg: {PURCHASER['dealer_reg']}")


if __name__ == "__main__":
    main()
