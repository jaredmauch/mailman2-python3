from __future__ import print_function

from builtins import str
from builtins import range
import os
import sys
import unittest
from unittest.mock import MagicMock, patch
import time
import email
import errno
import pickle
from email.generator import Generator
try:
    from Mailman import __init__
except ImportError:
    import paths

from Mailman import mm_cfg
from Mailman.MailList import MailList
from Mailman.Message import Message
from Mailman import Errors
from Mailman import Pending
from Mailman.Queue.Switchboard import Switchboard

# Add the parent directory to the Python path so we can import Mailman modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Create a mock email module
class MockMessage:
    def __init__(self):
        self.headers = {}
        self.body = ''

    def get(self, key, default=None):
        return self.headers.get(key, default)

    def __getitem__(self, key):
        return self.headers[key]

    def __setitem__(self, key, value):
        self.headers[key] = value

    def get_payload(self):
        return self.body

    def set_payload(self, payload):
        self.body = payload

class MockUtils:
    @staticmethod
    def unquote(value):
        return value
        
    @staticmethod
    def parseaddr(addr):
        return ('', addr)
        
    @staticmethod
    def formataddr(pair):
        return pair[1]

class MockEmailModule:
    message = MagicMock()
    message.Message = MockMessage
    utils = MockUtils

sys.modules['email'] = MockEmailModule
sys.modules['email.message'] = MockEmailModule.message
sys.modules['email.utils'] = MockEmailModule.utils

# Create a mock mm_cfg module
class MockMMCfg:
    def __init__(self):
        # Basic configuration options
        self.Radio = 1
        self.Toggle = 2
        self.Number = 3
        self.ADMIN_CATEGORIES = ['general', 'privacy', 'members']
        
        # Additional required attributes
        self.MEMBER_PASSWORD_LENGTH = 10
        self.DEFAULT_SERVER_LANGUAGE = 'en'
        self.WEB_HEADER_COLOR = '#ffffff'
        self.WEB_ADMINITEM_COLOR = '#eeeeee'
        self.WEB_ERROR_COLOR = '#ff0000'
        self.WEB_HIGHLIGHT_COLOR = '#dddddd'
        self.MAILMAN_SITE_LIST = 'mailman'
        self.OWNERS_CAN_DELETE_THEIR_OWN_LISTS = True
        self.DEFAULT_EMAIL_HOST = 'example.com'
        self.ACCEPTABLE_LISTNAME_CHARACTERS = '[-_.=a-z0-9]'
        self.SYNC_AFTER_WRITE = True
        self.ALLOW_OPEN_SUBSCRIBE = True
        self.AuthListAdmin = 1
        self.AuthSiteAdmin = 2
        self.OPTINFO = {
            'hide': True,
            'nomail': True,
            'ack': True,
            'notmetoo': True,
            'nodupes': True,
            'plain': True
        }

sys.modules['Mailman.mm_cfg'] = MockMMCfg()

# Mock the Utils module
class MockMailmanUtils:
    @staticmethod
    def list_exists(name):
        return False
        
    @staticmethod
    def ValidateEmail(addr):
        return True

    @staticmethod
    def GetLanguageDescr(lang):
        return 'English' if lang == 'en' else 'Unknown'

sys.modules['Mailman.Utils'] = MockMailmanUtils

from Mailman.Cgi import admin
from Mailman.htmlformat import Document
from Mailman.Message import Message

class TestAdmin(unittest.TestCase):
    def setUp(self):
        # Create a mock MailList object
        self.mlist = MagicMock()
        self.doc = Document()
        
        # Set up basic mlist attributes
        self.mlist.real_name = ''
        self.mlist.description = ''
        self.mlist.info = ''
        self.mlist.subscribe_policy = 0
        self.mlist.advertised = False
        self.mlist.preferred_language = 'en'
        
        # Mock GetConfigCategories to return our test categories
        class MockGui:
            def GetConfigInfo(self, mlist, category):
                if category == 'general':
                    return [
                        ('real_name', 1, 30, None, 'List name', None),
                        ('description', 1, 30, None, 'List description', None),
                        ('info', 1, 30, None, 'List info', None),
                        ('subscribe_policy', 1, None, None, 'Subscribe policy', None),
                        ('advertised', 2, None, None, 'List advertised', None)
                    ]
                return []

        class MockCategoryDict(dict):
            def __init__(self):
                super().__init__()
                self['general'] = ('General Options', MockGui())
                self['privacy'] = ('Privacy Options', MockGui())
                self['members'] = ('Member Options', MockGui())

            def keys(self):
                return ['general', 'privacy', 'members']

        self.mlist.GetConfigCategories.return_value = MockCategoryDict()
        
    def test_change_options_basic(self):
        """Test basic option changing functionality"""
        # Create a mock CGI data dictionary
        cgidata = {
            'real_name': ['Test List'],
            'description': ['A test mailing list'],
            'info': ['List information'],
        }
        
        # Call change_options with the general category
        result = admin.change_options(self.mlist, 'general', None, cgidata, self.doc)
        
        # Verify the changes were made
        self.assertEqual(self.mlist.real_name, 'Test List')
        self.assertEqual(self.mlist.description, 'A test mailing list')
        self.assertEqual(self.mlist.info, 'List information')
        
    def test_change_options_radio(self):
        """Test changing radio button options"""
        # Create a mock CGI data dictionary with a radio button option
        cgidata = {
            'subscribe_policy': ['2']  # 2 = require approval
        }
        
        # Call change_options
        result = admin.change_options(self.mlist, 'general', None, cgidata, self.doc)
        
        # Verify the change was made
        self.assertEqual(self.mlist.subscribe_policy, 2)
        
    def test_change_options_toggle(self):
        """Test changing toggle options"""
        # Create a mock CGI data dictionary with a toggle option
        cgidata = {
            'advertised': ['1']  # 1 = True
        }
        
        # Call change_options
        result = admin.change_options(self.mlist, 'general', None, cgidata, self.doc)
        
        # Verify the change was made
        self.assertTrue(self.mlist.advertised)
        
    def test_change_options_invalid_category(self):
        """Test handling of invalid category"""
        # Create a mock CGI data dictionary
        cgidata = {
            'real_name': ['Test List']
        }
        
        # Call change_options with an invalid category
        result = admin.change_options(self.mlist, 'nonexistent', None, cgidata, self.doc)
        
        # Verify no changes were made
        self.assertNotEqual(self.mlist.real_name, 'Test List')

    def test_config_categories_handling(self):
        """Test that GetConfigCategories handling works correctly"""
        # Get the categories
        categories = self.mlist.GetConfigCategories()
        
        # Test that we can access each category
        for category in categories:
            # Create a simple CGI data dictionary
            cgidata = {'real_name': ['Test List']}
            
            try:
                # This should not raise an AttributeError
                result = admin.change_options(self.mlist, category, None, cgidata, self.doc)
            except AttributeError as e:
                self.fail(f"Got AttributeError when processing category {category}: {str(e)}")

if __name__ == '__main__':
    unittest.main() 