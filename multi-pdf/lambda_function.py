import json
import base64
import os
import tempfile
import zipfile
import boto3
import urllib.request
import uuid
import subprocess
import concurrent.futures
from pdf2image import convert_from_bytes, convert_from_path
from io import BytesIO
from PIL import Image
from typing import List, Dict, Any, Tuple

def extract_pdfs_from_multipart(data_string: str) -> List[bytes]:
    """
    Extract multiple PDF contents from multipart form data string.
    
    Args:
        data_string: Base64 encoded multipart form data
    
    Returns:
        List of PDF contents as bytes
    """
    try:
        print(f"Processing data string of length: {len(data_string)}")
        
        # Decode the base64 data to get the raw multipart content
        decoded_data = base64.b64decode(data_string)
        print(f"Decoded data length: {len(decoded_data)}")
        
        # Try to decode as text to find boundaries and headers
        try:
            content = decoded_data.decode('utf-8', errors='replace')
        except:
            content = decoded_data.decode('latin1', errors='replace')
        
        print(f"First 500 chars of content: {content[:500]}")
        
        # Split into lines for processing
        lines = content.split('\n')
        
        # Find all PDF sections
        pdf_sections = []
        current_section = None
        
        for i, line in enumerate(lines):
            line_strip = line.strip()
            
            # Check for PDF content type or filename
            if ('application/pdf' in line_strip.lower() or 
                ('filename=' in line_strip.lower() and '.pdf' in line_strip.lower())):
                
                # Start of a new PDF section
                if current_section:
                    pdf_sections.append(current_section)
                
                current_section = {
                    'start_line': None,
                    'end_line': None,
                    'filename': None
                }
                
                # Extract filename if present
                if 'filename=' in line_strip:
                    try:
                        filename_part = line_strip.split('filename=')[1]
                        filename = filename_part.split('"')[1] if '"' in filename_part else filename_part.split()[0]
                        current_section['filename'] = filename
                        print(f"Found PDF filename: {filename}")
                    except:
                        pass
                
                # Find the start of PDF content (after headers)
                for j in range(i + 1, len(lines)):
                    if lines[j].strip() == '':
                        current_section['start_line'] = j + 1
                        break
            
            # Check for boundary (end of current section)
            elif line.startswith('--') and len(line) > 10 and current_section and current_section['start_line']:
                current_section['end_line'] = i
                pdf_sections.append(current_section)
                current_section = None
        
        # Handle the last section if it exists
        if current_section and current_section['start_line']:
            current_section['end_line'] = len(lines)
            pdf_sections.append(current_section)
        
        print(f"Found {len(pdf_sections)} PDF sections")
        
        # Extract each PDF
        extracted_pdfs = []
        
        for idx, section in enumerate(pdf_sections):
            if not section['start_line'] or not section['end_line']:
                continue
                
            filename = section['filename'] or f"document_{idx + 1}.pdf"
            print(f"Extracting PDF {idx + 1}: {filename}")
            print(f"  Lines {section['start_line']} to {section['end_line']}")
            
            # Extract the PDF content lines
            pdf_lines = lines[section['start_line']:section['end_line']]
            pdf_content_str = '\n'.join(pdf_lines).strip()
            
            print(f"  Extracted content length: {len(pdf_content_str)}")
            
            try:
                # Try to decode as base64
                pdf_content = base64.b64decode(pdf_content_str)
                print(f"  Successfully decoded base64: {len(pdf_content)} bytes")
                
                # Verify it's a PDF by checking the header
                if pdf_content.startswith(b'%PDF'):
                    print(f"  ✅ Confirmed PDF header for {filename}")
                    extracted_pdfs.append(pdf_content)
                else:
                    print(f"  ❌ No PDF header found for {filename}. First 20 bytes: {pdf_content[:20]}")
                    
            except Exception as e:
                print(f"  ❌ Base64 decode failed for {filename}: {e}")
                
                # Try using raw bytes
                try:
                    pdf_content = pdf_content_str.encode('latin1')
                    if pdf_content.startswith(b'%PDF'):
                        print(f"  ✅ Found PDF in raw latin1 encoding for {filename}")
                        extracted_pdfs.append(pdf_content)
                    else:
                        print(f"  ❌ Still no PDF header in latin1 for {filename}")
                except Exception as e2:
                    print(f"  ❌ Latin1 encode also failed for {filename}: {e2}")
        
        if not extracted_pdfs:
            # Fallback: try to find PDF magic bytes directly in the decoded data
            print("No PDFs extracted, trying fallback method...")
            pdf_start = 0
            while True:
                pdf_start = decoded_data.find(b'%PDF', pdf_start)
                if pdf_start < 0:
                    break
                    
                print(f"Found PDF magic bytes at position {pdf_start}")
                pdf_end = decoded_data.find(b'%%EOF', pdf_start)
                if pdf_end >= 0:
                    pdf_end += 5  # Include %%EOF
                    pdf_content = decoded_data[pdf_start:pdf_end]
                    extracted_pdfs.append(pdf_content)
                    print(f"Extracted PDF of {len(pdf_content)} bytes")
                    pdf_start = pdf_end
                else:
                    # No %%EOF found, take everything from PDF start to next PDF or end
                    next_pdf = decoded_data.find(b'%PDF', pdf_start + 1)
                    if next_pdf > 0:
                        pdf_content = decoded_data[pdf_start:next_pdf]
                    else:
                        pdf_content = decoded_data[pdf_start:]
                    extracted_pdfs.append(pdf_content)
                    print(f"Extracted PDF of {len(pdf_content)} bytes (no %%EOF)")
                    break
        
        print(f"Successfully extracted {len(extracted_pdfs)} PDFs")
        return extracted_pdfs
        
    except Exception as e:
        print(f"Error extracting PDFs from multipart data: {e}")
        print(f"Data preview: {data_string[:200]}...")
        raise ValueError(f"Could not extract PDF content: {e}")

def process_single_data_field(pdf_data: Dict[str, Any], temp_dir: str, field_index: int) -> Tuple[List[Tuple[str, List[Image.Image]]], List[str]]:
    """
    Process a single data field that may contain multiple PDFs.
    
    Args:
        pdf_data: Dictionary containing PDF information with 'data' field
        temp_dir: Temporary directory for file operations
        field_index: Index of the data field for naming
    
    Returns:
        Tuple of (list_of_successful_conversions, list_of_error_messages)
        where successful_conversions is [(doc_id, images), ...]
    """
    field_name = pdf_data.get('field_name', f'field_{field_index + 1}')
    successful_conversions = []
    error_messages = []
    
    try:
        # Extract all PDFs from the data field (may contain multiple PDFs)
        pdf_contents = extract_pdfs_from_multipart(pdf_data['data'])
        
        print(f"Extracted {len(pdf_contents)} PDFs from {field_name}")
        
        for pdf_index, pdf_content in enumerate(pdf_contents):
            doc_id = f"{field_name}_pdf_{pdf_index + 1}"
            
            try:
                # Save to temporary file
                pdf_path = os.path.join(temp_dir, f"{doc_id}.pdf")
                with open(pdf_path, 'wb') as f:
                    f.write(pdf_content)
                
                print(f"Converting PDF {doc_id}: {len(pdf_content)} bytes")
                
                # Convert PDF to images
                images = convert_from_path(
                    pdf_path,
                    dpi=150,  # Fixed DPI for consistency
                    fmt='jpeg',
                    thread_count=1
                )
                
                print(f"Successfully converted {len(images)} pages for document {doc_id}")
                successful_conversions.append((doc_id, images))
                
            except Exception as e:
                error_msg = f"Error processing {doc_id}: {str(e)}"
                print(error_msg)
                error_messages.append(error_msg)
        
    except Exception as e:
        error_msg = f"Error extracting PDFs from {field_name}: {str(e)}"
        print(error_msg)
        error_messages.append(error_msg)
    
    return successful_conversions, error_messages

def lambda_handler(event, context):
    """
    AWS Lambda function that converts multiple PDFs to JPEGs.
    
    Expected input formats:
    
    1. Single PDF (backward compatibility):
    - PDF file content as base64 in the request body
    OR
    - {"pdf_url": "https://example.com/file.pdf"}
    
    2. Multiple PDFs in array format:
    [
        {
            "data": "base64_encoded_multipart_data_with_pdf"
        },
        {
            "data": "base64_encoded_multipart_data_with_pdf"
        }
    ]
    
    Returns:
    - Base64 encoded ZIP file containing all JPEGs (same format as original function)
    """
    
    # Print initial environment for debugging
    print("Lambda environment:")
    print(f"PATH: {os.environ.get('PATH', 'Not set')}")
    
    # Check poppler installation
    try:
        result = subprocess.run(["pdftoppm", "-v"], capture_output=True, text=True)
        print(f"pdftoppm version: {result.stderr}")
    except Exception as e:
        print(f"Error checking pdftoppm: {e}")
    
    try:
        # Extract the input from the event
        if 'body' not in event:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'No body found in request'})
            }
        
        body = event['body']
        
        # Parse the request body
        if isinstance(body, str):
            try:
                body_json = json.loads(body)
            except json.JSONDecodeError:
                # Assume it's a base64 encoded PDF (backward compatibility)
                body_json = body
        else:
            body_json = body
        
        # DEBUG: Print the received data structure
        print("=== RECEIVED DATA STRUCTURE ===")
        print(f"Body type: {type(body_json)}")
        print(f"Body content (first 1000 chars): {str(body_json)[:1000]}")
        
        if isinstance(body_json, dict):
            print(f"Body keys: {list(body_json.keys())}")
            for key, value in body_json.items():
                if isinstance(value, str) and len(value) > 100:
                    print(f"  {key}: <string of length {len(value)}>")
                    # Print first few chars to help identify content
                    print(f"    First 100 chars: {value[:100]}")
                else:
                    print(f"  {key}: {type(value)} - {str(value)[:100]}")
        elif isinstance(body_json, list):
            print(f"Body is list with {len(body_json)} items")
            for i, item in enumerate(body_json):
                print(f"  Item {i}: {type(item)}")
                if isinstance(item, dict):
                    print(f"    Keys: {list(item.keys())}")
                    for key, value in item.items():
                        if isinstance(value, str) and len(value) > 100:
                            print(f"      {key}: <string of length {len(value)}>")
                            print(f"        First 100 chars: {value[:100]}")
                        else:
                            print(f"      {key}: {type(value)} - {str(value)[:100]}")
        print("=== END DEBUG ===")
        
        # Determine the input format
        pdfs_to_process = []
        
        if isinstance(body_json, list):
            # Array of PDFs with data field
            print(f"Processing array of {len(body_json)} PDFs")
            pdfs_to_process = body_json
            
        elif isinstance(body_json, dict):
            # Check if it's a single object with multiple PDF fields
            pdf_fields = []
            for key, value in body_json.items():
                if key.startswith('doc') and isinstance(value, str) and len(value) > 100:
                    # This looks like a PDF data field
                    pdf_fields.append({'data': value, 'field_name': key})
                    print(f"Found PDF field: {key} with {len(value)} characters")
                elif key == 'data' and isinstance(value, str) and len(value) > 100:
                    # Standard data field
                    pdf_fields.append({'data': value, 'field_name': 'data'})
                    print(f"Found standard data field with {len(value)} characters")
            
            if pdf_fields:
                print(f"Found {len(pdf_fields)} PDF fields in object: {[f['field_name'] for f in pdf_fields]}")
                pdfs_to_process = pdf_fields
            elif 'pdf_url' in body_json:
                # Single PDF with URL (backward compatibility)
                print("Processing single PDF from URL")
                with tempfile.TemporaryDirectory() as temp_dir:
                    pdf_path = os.path.join(temp_dir, "single.pdf")
                    urllib.request.urlretrieve(body_json['pdf_url'], pdf_path)
                    
                    with open(pdf_path, 'rb') as f:
                        pdf_content = f.read()
                    
                    pdfs_to_process = [{'data': base64.b64encode(pdf_content).decode('utf-8')}]
            else:
                # Check for any field that might contain PDF data
                pdf_found = False
                for key, value in body_json.items():
                    if isinstance(value, str) and len(value) > 100:
                        print(f"Processing single PDF from field '{key}'")
                        pdfs_to_process = [{'data': value, 'field_name': key}]
                        pdf_found = True
                        break
                
                if not pdf_found:
                    print("No PDF data found in object")
                    pdfs_to_process = []
        else:
            # Assume entire body is base64 PDF content (backward compatibility)
            print("Processing single PDF from raw body")
            pdfs_to_process = [{'data': body, 'field_name': 'raw_body'}]
        
        if not pdfs_to_process:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'No PDFs provided for processing'})
            }
        
        print(f"Processing {len(pdfs_to_process)} data fields")
        
        # Process data fields with controlled concurrency (max 3 concurrent)
        max_concurrent = min(3, len(pdfs_to_process))
        successful_conversions = 0
        total_pages = 0
        all_conversions = []  # List of (doc_id, images) tuples
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Use ThreadPoolExecutor for concurrent processing
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as executor:
                # Submit all tasks
                future_to_index = {
                    executor.submit(process_single_data_field, data_field, temp_dir, i): i 
                    for i, data_field in enumerate(pdfs_to_process)
                }
                
                # Collect results as they complete
                for future in concurrent.futures.as_completed(future_to_index):
                    field_conversions, field_errors = future.result()
                    
                    if field_errors:
                        for error in field_errors:
                            print(f"Field processing error: {error}")
                    
                    for doc_id, images in field_conversions:
                        all_conversions.append((doc_id, images))
                        successful_conversions += 1
                        total_pages += len(images)
                        print(f"Successfully processed {doc_id}: {len(images)} pages")
            
            if not all_conversions:
                return {
                    'statusCode': 500,
                    'headers': {'Content-Type': 'application/json'},
                    'body': json.dumps({'error': 'Failed to convert any PDFs'})
                }
            
            # Create ZIP file with all converted images
            zip_buffer = BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                
                for doc_id, images in all_conversions:
                    for i, image in enumerate(images):
                        img_buffer = BytesIO()
                        image.save(img_buffer, format='JPEG', quality=85, optimize=True)
                        img_buffer.seek(0)
                        
                        # File naming strategy
                        if len(all_conversions) > 1:
                            # Multiple documents: include document identifier
                            file_path = f"{doc_id}_page_{i+1}.jpg"
                        else:
                            # Single document: simple naming
                            file_path = f"page_{i+1}.jpg"
                        
                        zip_file.writestr(file_path, img_buffer.getvalue())
            
            zip_buffer.seek(0)
            zip_data = base64.b64encode(zip_buffer.getvalue()).decode('utf-8')
            
            total_pdfs = len(all_conversions)
            print(f"Successfully processed {total_pdfs} PDFs from {len(pdfs_to_process)} data fields, total {total_pages} pages")
            
            # Return only the base64 ZIP data (same format as original function)
            return {
                'statusCode': 200,
                'headers': {
                    'Content-Type': 'application/zip',
                    'Content-Disposition': 'attachment; filename=pdf_images.zip'
                },
                'body': zip_data,
                'isBase64Encoded': True
            }
    
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({
                'error': str(e),
                'details': 'Check CloudWatch logs for more information'
            })
        }