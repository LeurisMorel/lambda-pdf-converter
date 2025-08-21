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

def extract_pdf_from_multipart(data_string: str) -> bytes:
    """
    Extract PDF content from multipart form data string.
    
    Args:
        data_string: Base64 encoded multipart form data
    
    Returns:
        PDF content as bytes
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
        
        # Find the actual PDF content
        pdf_start_idx = None
        pdf_end_idx = None
        
        # Look for Content-Type: application/pdf or similar patterns
        for i, line in enumerate(lines):
            line_strip = line.strip()
            
            # Check for PDF content type
            if 'application/pdf' in line_strip.lower():
                print(f"Found PDF content type at line {i}: {line_strip}")
                # PDF content usually starts after the next empty line
                for j in range(i + 1, len(lines)):
                    if lines[j].strip() == '':
                        pdf_start_idx = j + 1
                        break
                break
            
            # Alternative: look for filename with .pdf extension
            elif 'filename=' in line_strip.lower() and '.pdf' in line_strip.lower():
                print(f"Found PDF filename at line {i}: {line_strip}")
                # Continue looking for content-type or empty line
                continue
        
        # If we didn't find a clear start, look for the first empty line after headers
        if pdf_start_idx is None:
            for i, line in enumerate(lines):
                if line.strip() == '' and i > 5:  # Skip the first few lines
                    pdf_start_idx = i + 1
                    print(f"Using fallback PDF start at line {i + 1}")
                    break
        
        # Find the end boundary
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith('--') and len(lines[i]) > 10:
                pdf_end_idx = i
                print(f"Found end boundary at line {i}: {lines[i][:50]}")
                break
        
        if pdf_start_idx is None:
            print("Could not find PDF start, using entire content")
            return decoded_data
        
        # Extract the PDF content lines
        if pdf_end_idx is not None:
            pdf_lines = lines[pdf_start_idx:pdf_end_idx]
        else:
            pdf_lines = lines[pdf_start_idx:]
        
        # Join the PDF content
        pdf_content_str = '\n'.join(pdf_lines).strip()
        
        print(f"Extracted PDF content length: {len(pdf_content_str)}")
        print(f"First 100 chars of PDF content: {pdf_content_str[:100]}")
        
        # The PDF content is likely base64 encoded within the multipart
        try:
            # Try to decode as base64
            pdf_content = base64.b64decode(pdf_content_str)
            print(f"Successfully decoded base64 PDF content: {len(pdf_content)} bytes")
            
            # Verify it's a PDF by checking the header
            if pdf_content.startswith(b'%PDF'):
                print("Confirmed PDF header found")
                return pdf_content
            else:
                print(f"No PDF header found. First 20 bytes: {pdf_content[:20]}")
                
        except Exception as e:
            print(f"Base64 decode failed: {e}")
        
        # If base64 decode fails, try using the raw bytes
        try:
            pdf_content = pdf_content_str.encode('latin1')
            if pdf_content.startswith(b'%PDF'):
                print("Found PDF in raw latin1 encoding")
                return pdf_content
        except Exception as e:
            print(f"Latin1 encode failed: {e}")
        
        # Last resort: try to find PDF magic bytes in the decoded data
        pdf_start = decoded_data.find(b'%PDF')
        if pdf_start >= 0:
            print(f"Found PDF magic bytes at position {pdf_start}")
            # Find the end of the PDF (usually %%EOF)
            pdf_end = decoded_data.find(b'%%EOF', pdf_start)
            if pdf_end >= 0:
                pdf_end += 5  # Include %%EOF
                return decoded_data[pdf_start:pdf_end]
            else:
                # If no %%EOF found, take everything from PDF start
                return decoded_data[pdf_start:]
        
        raise ValueError("Could not find valid PDF content in multipart data")
        
    except Exception as e:
        print(f"Error extracting PDF from multipart data: {e}")
        print(f"Data preview: {data_string[:200]}...")
        raise ValueError(f"Could not extract PDF content: {e}")

def process_single_pdf(pdf_data: Dict[str, Any], temp_dir: str, pdf_index: int) -> Tuple[str, List[Image.Image], str]:
    """
    Process a single PDF and return the document ID, images, and any error message.
    
    Args:
        pdf_data: Dictionary containing PDF information
        temp_dir: Temporary directory for file operations
        pdf_index: Index of the PDF for naming
    
    Returns:
        Tuple of (document_id, list_of_images, error_message)
    """
    doc_id = f"doc_{pdf_index + 1}"
    error_msg = None
    images = []
    
    try:
        # Extract PDF content from the data field
        pdf_content = extract_pdf_from_multipart(pdf_data['data'])
        
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
        
    except Exception as e:
        error_msg = str(e)
        print(f"Error processing document {doc_id}: {error_msg}")
    
    return doc_id, images, error_msg

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
        
        # Determine the input format
        pdfs_to_process = []
        
        if isinstance(body_json, list):
            # Array of PDFs with data field
            print(f"Processing array of {len(body_json)} PDFs")
            pdfs_to_process = body_json
            
        elif isinstance(body_json, dict):
            if 'pdf_url' in body_json:
                # Single PDF with URL (backward compatibility)
                print("Processing single PDF from URL")
                with tempfile.TemporaryDirectory() as temp_dir:
                    pdf_path = os.path.join(temp_dir, "single.pdf")
                    urllib.request.urlretrieve(body_json['pdf_url'], pdf_path)
                    
                    with open(pdf_path, 'rb') as f:
                        pdf_content = f.read()
                    
                    pdfs_to_process = [{'data': base64.b64encode(pdf_content).decode('utf-8')}]
            else:
                # Assume single PDF data
                print("Processing single PDF from data")
                pdfs_to_process = [{'data': body_json.get('content', body)}]
        else:
            # Assume entire body is base64 PDF content (backward compatibility)
            print("Processing single PDF from raw body")
            pdfs_to_process = [{'data': body}]
        
        if not pdfs_to_process:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'No PDFs provided for processing'})
            }
        
        print(f"Processing {len(pdfs_to_process)} PDFs")
        
        # Process PDFs with controlled concurrency (max 3 concurrent)
        max_concurrent = min(3, len(pdfs_to_process))
        successful_conversions = 0
        total_pages = 0
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Use ThreadPoolExecutor for concurrent processing
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as executor:
                # Submit all tasks
                future_to_index = {
                    executor.submit(process_single_pdf, pdf_data, temp_dir, i): i 
                    for i, pdf_data in enumerate(pdfs_to_process)
                }
                
                # Collect all images from successful conversions
                all_images = {}  # doc_id -> list of images
                
                # Collect results as they complete
                for future in concurrent.futures.as_completed(future_to_index):
                    doc_id, images, error_msg = future.result()
                    
                    if error_msg:
                        print(f"Failed to process {doc_id}: {error_msg}")
                    else:
                        all_images[doc_id] = images
                        successful_conversions += 1
                        total_pages += len(images)
                        print(f"Successfully processed {doc_id}: {len(images)} pages")
            
            if successful_conversions == 0:
                return {
                    'statusCode': 500,
                    'headers': {'Content-Type': 'application/json'},
                    'body': json.dumps({'error': 'Failed to convert any PDFs'})
                }
            
            # Create ZIP file with all converted images
            zip_buffer = BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                
                for doc_id in sorted(all_images.keys()):
                    images = all_images[doc_id]
                    
                    for i, image in enumerate(images):
                        img_buffer = BytesIO()
                        image.save(img_buffer, format='JPEG', quality=85, optimize=True)
                        img_buffer.seek(0)
                        
                        # File naming strategy
                        if len(pdfs_to_process) > 1:
                            # Multiple PDFs: include document identifier
                            file_path = f"{doc_id}_page_{i+1}.jpg"
                        else:
                            # Single PDF: simple naming
                            file_path = f"page_{i+1}.jpg"
                        
                        zip_file.writestr(file_path, img_buffer.getvalue())
            
            zip_buffer.seek(0)
            zip_data = base64.b64encode(zip_buffer.getvalue()).decode('utf-8')
            
            print(f"Successfully processed {successful_conversions}/{len(pdfs_to_process)} PDFs, total {total_pages} pages")
            
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