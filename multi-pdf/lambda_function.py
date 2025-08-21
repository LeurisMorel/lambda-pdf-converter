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

def process_single_pdf(pdf_data: Dict[str, Any], temp_dir: str) -> Tuple[str, List[Image.Image], str]:
    """
    Process a single PDF and return the document ID, images, and any error message.
    
    Args:
        pdf_data: Dictionary containing PDF information (content, url, or file_name)
        temp_dir: Temporary directory for file operations
    
    Returns:
        Tuple of (document_id, list_of_images, error_message)
    """
    doc_id = pdf_data.get('id', str(uuid.uuid4()))
    error_msg = None
    images = []
    
    try:
        pdf_path = None
        
        # Handle different input types
        if 'content' in pdf_data:
            # Base64 encoded PDF content
            pdf_content = base64.b64decode(pdf_data['content'])
            pdf_path = os.path.join(temp_dir, f"{doc_id}.pdf")
            with open(pdf_path, 'wb') as f:
                f.write(pdf_content)
                
        elif 'url' in pdf_data:
            # URL to download PDF
            pdf_url = pdf_data['url']
            pdf_path = os.path.join(temp_dir, f"{doc_id}.pdf")
            print(f"Downloading PDF from URL: {pdf_url}")
            urllib.request.urlretrieve(pdf_url, pdf_path)
            
        elif 'file_name' in pdf_data and 'content' in pdf_data:
            # Named file with content
            pdf_content = base64.b64decode(pdf_data['content'])
            pdf_path = os.path.join(temp_dir, pdf_data['file_name'])
            with open(pdf_path, 'wb') as f:
                f.write(pdf_content)
        else:
            raise ValueError("PDF data must contain either 'content', 'url', or both 'file_name' and 'content'")
        
        print(f"Converting PDF {doc_id}: {pdf_path}")
        
        # Convert PDF to images
        images = convert_from_path(
            pdf_path,
            dpi=pdf_data.get('dpi', 150),
            fmt='jpeg',
            thread_count=1  # Reduced to avoid overwhelming the Lambda
        )
        
        print(f"Successfully converted {len(images)} pages for document {doc_id}")
        
    except Exception as e:
        error_msg = str(e)
        print(f"Error processing document {doc_id}: {error_msg}")
    
    return doc_id, images, error_msg

def lambda_handler(event, context):
    """
    AWS Lambda function that converts multiple PDFs to JPEGs using a Docker container.
    
    Expected input formats:
    
    1. Single PDF (backward compatibility):
    - PDF file content as base64 in the request body
    OR
    - {"pdf_url": "https://example.com/file.pdf"}
    
    2. Multiple PDFs:
    {
        "pdfs": [
            {
                "id": "doc1",  # Optional, will generate UUID if not provided
                "content": "base64_encoded_pdf_content",
                "dpi": 150  # Optional, defaults to 150
            },
            {
                "id": "doc2",
                "url": "https://example.com/file.pdf",
                "dpi": 200
            },
            {
                "id": "doc3",
                "file_name": "document.pdf",
                "content": "base64_encoded_pdf_content"
            }
        ],
        "zip_structure": "flat" | "grouped",  # Optional, defaults to "flat"
        "max_concurrent": 3  # Optional, defaults to 3
    }
    
    Returns:
    - Base64 encoded ZIP file containing all JPEGs
    - For multiple PDFs, returns summary with success/error counts
    """
    
    # Print initial environment for debugging
    print("Lambda environment:")
    print(f"PATH: {os.environ.get('PATH', 'Not set')}")
    print(f"LD_LIBRARY_PATH: {os.environ.get('LD_LIBRARY_PATH', 'Not set')}")
    
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
                body_json = {'content': body}
        else:
            body_json = body
        
        # Determine if we're processing single or multiple PDFs
        pdfs_to_process = []
        
        if 'pdfs' in body_json and isinstance(body_json['pdfs'], list):
            # Multiple PDFs mode
            pdfs_to_process = body_json['pdfs']
            zip_structure = body_json.get('zip_structure', 'flat')
            max_concurrent = min(body_json.get('max_concurrent', 3), 5)  # Cap at 5 for Lambda limits
        elif 'pdf_url' in body_json:
            # Single PDF with URL (backward compatibility)
            pdfs_to_process = [{'id': 'single_pdf', 'url': body_json['pdf_url']}]
            zip_structure = 'flat'
            max_concurrent = 1
        elif 'content' in body_json:
            # Single PDF with content (backward compatibility)
            pdfs_to_process = [{'id': 'single_pdf', 'content': body_json['content']}]
            zip_structure = 'flat'
            max_concurrent = 1
        else:
            # Assume entire body is base64 PDF content (backward compatibility)
            pdfs_to_process = [{'id': 'single_pdf', 'content': body}]
            zip_structure = 'flat'
            max_concurrent = 1
        
        if not pdfs_to_process:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'No PDFs provided for processing'})
            }
        
        print(f"Processing {len(pdfs_to_process)} PDFs with max_concurrent={max_concurrent}")
        
        # Process PDFs with controlled concurrency
        results = {}
        successful_conversions = 0
        failed_conversions = 0
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Use ThreadPoolExecutor for concurrent processing
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as executor:
                # Submit all tasks
                future_to_pdf = {
                    executor.submit(process_single_pdf, pdf_data, temp_dir): pdf_data 
                    for pdf_data in pdfs_to_process
                }
                
                # Collect results as they complete
                for future in concurrent.futures.as_completed(future_to_pdf):
                    doc_id, images, error_msg = future.result()
                    
                    if error_msg:
                        results[doc_id] = {'status': 'error', 'error': error_msg, 'pages': 0}
                        failed_conversions += 1
                    else:
                        results[doc_id] = {'status': 'success', 'pages': len(images), 'images': images}
                        successful_conversions += 1
            
            # Create ZIP file with all converted images
            zip_buffer = BytesIO()
            with zipfile.ZipFile(zip_buffer, 'a', zipfile.ZIP_DEFLATED) as zip_file:
                
                for doc_id, result in results.items():
                    if result['status'] == 'success':
                        images = result['images']
                        
                        for i, image in enumerate(images):
                            img_buffer = BytesIO()
                            image.save(img_buffer, format='JPEG', quality=85, optimize=True)
                            img_buffer.seek(0)
                            
                            # Determine file path based on zip structure
                            if zip_structure == 'grouped' and len(pdfs_to_process) > 1:
                                file_path = f"{doc_id}/page_{i+1}.jpg"
                            else:
                                # Flat structure or single PDF
                                if len(pdfs_to_process) > 1:
                                    file_path = f"{doc_id}_page_{i+1}.jpg"
                                else:
                                    file_path = f"page_{i+1}.jpg"
                            
                            zip_file.writestr(file_path, img_buffer.getvalue())
                
                # Add summary file for multi-PDF processing
                if len(pdfs_to_process) > 1:
                    summary = {
                        'total_pdfs': len(pdfs_to_process),
                        'successful_conversions': successful_conversions,
                        'failed_conversions': failed_conversions,
                        'results': {doc_id: {k: v for k, v in result.items() if k != 'images'} 
                                  for doc_id, result in results.items()}
                    }
                    zip_file.writestr('conversion_summary.json', json.dumps(summary, indent=2))
            
            zip_buffer.seek(0)
            zip_data = base64.b64encode(zip_buffer.getvalue()).decode('utf-8')
            
            # Prepare response
            response_body = {
                'total_pdfs': len(pdfs_to_process),
                'successful_conversions': successful_conversions,
                'failed_conversions': failed_conversions,
                'zip_data': zip_data
            }
            
            # Add detailed results for multi-PDF processing
            if len(pdfs_to_process) > 1:
                response_body['results'] = {
                    doc_id: {k: v for k, v in result.items() if k != 'images'} 
                    for doc_id, result in results.items()
                }
            
            return {
                'statusCode': 200,
                'headers': {
                    'Content-Type': 'application/json',
                    'Content-Disposition': 'attachment; filename=pdf_images.zip'
                },
                'body': json.dumps(response_body)
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