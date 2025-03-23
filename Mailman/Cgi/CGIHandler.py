"""CGI handler module to replace the deprecated cgi module.

This module provides functionality similar to the deprecated cgi module,
using http.server instead.
"""

import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, parse_qsl
from io import BytesIO
import email
from email.parser import BytesParser
from email.policy import default

class FieldStorage:
    """A class to handle form data similar to cgi.FieldStorage."""
    
    def __init__(self, keep_blank_values=0):
        self.keep_blank_values = keep_blank_values
        self.list = []
        self.headers = {}
        
        # Get the content type and length
        content_type = os.environ.get('CONTENT_TYPE', '')
        content_length = int(os.environ.get('CONTENT_LENGTH', 0))
        
        if content_type:
            # Parse the content type
            ctype, pdict = email.parser.Parser().parsestr(
                'Content-Type: ' + content_type)
            
            # Handle multipart form data
            if ctype.get_content_maintype() == 'multipart':
                boundary = ctype.get_boundary()
                if boundary:
                    self._parse_multipart(boundary, content_length)
            # Handle URL encoded form data
            elif ctype.get_content_type() == 'application/x-www-form-urlencoded':
                self._parse_urlencoded(content_length)
        
        # Parse query string
        qs = os.environ.get('QUERY_STRING', '')
        if qs:
            self._parse_query_string(qs)
    
    def _parse_multipart(self, boundary, content_length):
        """Parse multipart form data."""
        if content_length <= 0:
            return
            
        # Read the raw POST data
        post_data = sys.stdin.buffer.read(content_length)
        
        # Create a BytesIO object to simulate a file
        fp = BytesIO(post_data)
        
        # Parse the multipart message
        msg = BytesParser(policy=default).parse(fp)
        
        # Process each part
        for part in msg.walk():
            if part.get_content_maintype() == 'multipart':
                continue
                
            # Get the field name from the Content-Disposition header
            cd = part.get('Content-Disposition', '')
            if not cd:
                continue
                
            # Parse the Content-Disposition header
            ctype, params = email.parser.Parser().parsestr('Content-Disposition: ' + cd)
            
            # Get the field name
            name = params.get('name')
            if not name:
                continue
                
            # Get the field value
            value = part.get_content()
            
            # Add to the list
            self.list.append((name, value))
    
    def _parse_urlencoded(self, content_length):
        """Parse URL encoded form data."""
        if content_length <= 0:
            return
            
        # Read the raw POST data
        post_data = sys.stdin.buffer.read(content_length)
        
        # Parse the data
        pairs = parse_qsl(post_data.decode('latin-1'),
                         keep_blank_values=self.keep_blank_values)
        
        # Add to the list
        self.list.extend(pairs)
    
    def _parse_query_string(self, qs):
        """Parse query string data."""
        pairs = parse_qsl(qs, keep_blank_values=self.keep_blank_values)
        self.list.extend(pairs)
    
    def getfirst(self, key, default=None):
        """Get the first value for a key."""
        for k, v in self.list:
            if k == key:
                return v
        return default
    
    def getlist(self, key):
        """Get all values for a key."""
        return [v for k, v in self.list if k == key]
    
    def keys(self):
        """Get all keys."""
        return list(set(k for k, v in self.list))

    def __contains__(self, key):
        """Support the 'in' operator."""
        return any(k == key for k, v in self.list)

    def __iter__(self):
        """Support iteration over keys."""
        return iter(set(k for k, v in self.list))

def parse_qs(qs, keep_blank_values=0):
    """Parse a query string into a dictionary."""
    return parse_qs(qs, keep_blank_values=keep_blank_values) 