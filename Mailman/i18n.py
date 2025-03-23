# Copyright (C) 2000-2018 by the Free Software Foundation, Inc.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301,
# USA.

"""Internationalization support for Mailman."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import os
import sys
import time
import errno
import gettext
from typing import Dict, Optional, Callable

from Mailman import mm_cfg
from Mailman import Utils
from Mailman.SafeDict import SafeDict

# Global catalog dictionary for all languages
_catalogs: Dict[str, gettext.NullTranslations] = {}

# Module level charset registry.  Maps language name to character set name
_charset_registry: Dict[str, str] = {}

def init(language: str = None):
    """Initialize the i18n subsystem.

    Args:
        language: Optional language code. If not provided, defaults to the
                 system default language.
    """
    global _translation
    if language is None:
        language = mm_cfg.DEFAULT_SERVER_LANGUAGE
    _translation = get_translation(language)


def get_translation(language: str) -> gettext.NullTranslations:
    """Return translation instance for a given language code.

    Args:
        language: The language code to get translations for.

    Returns:
        A gettext translation instance for the specified language.
    """
    if language not in _catalogs:
        # Add support for overriding the location of the locale directory.
        # This is used primarily for testing purposes, but could be useful for
        # other things.
        locale_dir = os.environ.get('MAILMAN_LOCALE_DIR', mm_cfg.LOCALE_DIR)
        try:
            _catalogs[language] = gettext.translation(
                'mailman', locale_dir, [language])
        except IOError as e:
            if e.errno != errno.ENOENT:
                raise
            # The catalog doesn't exist for this language.  Install a NullTranslations
            # instance which will defer to the default language.
            _catalogs[language] = gettext.NullTranslations()
    return _catalogs[language]


def set_language(language: str):
    """Set the global translation language.

    Args:
        language: The language code to set as current.
    """
    global _translation
    _translation = get_translation(language)


def get_language() -> str:
    """Get the current global translation language.

    Returns:
        The current language code.
    """
    return _translation._info.get('language-code', mm_cfg.DEFAULT_SERVER_LANGUAGE)


def add_charset(language: str, charset: str):
    """Register a charset for a specific language.

    Args:
        language: The language code.
        charset: The charset name.
    """
    _charset_registry[language] = charset


def get_charset(language: str) -> str:
    """Get the charset registered for a specific language.

    Args:
        language: The language code.

    Returns:
        The charset name registered for the language, or us-ascii if none.
    """
    return _charset_registry.get(language, 'us-ascii')


def get_translation_obj() -> gettext.NullTranslations:
    """Return the active translation object.

    Returns:
        The current gettext translation instance.
    """
    return _translation


# Set up the global translation based on environment variables
_translation = gettext.NullTranslations()

# Look for environment variables that can help us determine the proper locale
# settings.  Note that the environment variables must be encoded as UTF-8 strings.
for envar in ('LC_ALL', 'LANGUAGE', 'LC_MESSAGES', 'LANG'):
    if envar in os.environ:
        try:
            val = os.environ[envar]
            if val:
                init(val.split('_')[0])
                break
        except (TypeError, ValueError):
            continue

# Install the _ function into Python's builtins
builtins = sys.modules.get('__builtin__', sys.modules.get('builtins'))
builtins._ = _translation.gettext

# Convenience functions for string interpolation in translated text
def _(s: str) -> str:
    """Translate and interpolate a string.

    Args:
        s: The string to translate.

    Returns:
        The translated string.
    """
    return _translation.gettext(s)

def N_(s: str) -> str:
    """Mark a string for translation but don't translate it yet.

    Args:
        s: The string to mark for translation.

    Returns:
        The original string.
    """
    return s

def C_(s: str) -> str:
    """Translate and interpolate a string in the context of the current language.

    Args:
        s: The string to translate.

    Returns:
        The translated string.
    """
    return _translation.gettext(s)

def interpolate(template: str, mapping: Dict[str, str], 
               safe: bool = False, **kws) -> str:
    """Interpolate a translated string with the given mapping.

    Args:
        template: The string template to interpolate.
        mapping: Dictionary of key/value pairs to substitute.
        safe: Whether to use SafeDict to prevent KeyError.
        **kws: Additional key/value pairs to add to mapping.

    Returns:
        The interpolated string.
    """
    if safe:
        mapping = SafeDict(mapping)
    if kws:
        mapping.update(kws)
    return template % mapping

def _get_ctype_charset():
    old = locale.setlocale(locale.LC_CTYPE, '')
    charset = locale.nl_langinfo(locale.CODESET)
    locale.setlocale(locale.LC_CTYPE, old)
    return charset

if not mm_cfg.DISABLE_COMMAND_LOCALE_CSET:
    _ctype_charset = _get_ctype_charset()
else:
    _ctype_charset = None


def tolocale(s):
    global _ctype_charset
    if isinstance(s, UnicodeType) or _ctype_charset is None:
        return s
    source = _translation.charset()
    if not source:
        return s
    return str(s, source, 'replace').encode(_ctype_charset, 'replace')

if mm_cfg.DISABLE_COMMAND_LOCALE_CSET:
    C_ = _
else:
    def C_(s):
        return tolocale(_(s, 2))


def ctime(date):
    # Don't make these module globals since we have to do runtime translation
    # of the strings anyway.
    daysofweek = [
        _('Mon'), _('Tue'), _('Wed'), _('Thu'),
        _('Fri'), _('Sat'), _('Sun')
        ]
    months = [
        '',
        _('Jan'), _('Feb'), _('Mar'), _('Apr'), _('May'), _('Jun'),
        _('Jul'), _('Aug'), _('Sep'), _('Oct'), _('Nov'), _('Dec')
        ]

    tzname = _('Server Local Time')
    if isinstance(date, str):
        try:
            year, mon, day, hh, mm, ss, wday, ydat, dst = time.strptime(date)
            if dst in (0,1):
                tzname = time.tzname[dst]
            else:
                # MAS: No exception but dst = -1 so try
                return ctime(time.mktime((year, mon, day, hh, mm, ss, wday,
                                          ydat, dst)))
        except (ValueError, AttributeError):
            try:
                wday, mon, day, hms, year = date.split()
                hh, mm, ss = hms.split(':')
                year = int(year)
                day = int(day)
                hh = int(hh)
                mm = int(mm)
                ss = int(ss)
            except ValueError:
                return date
            else:
                for i in range(0, 7):
                    wconst = (1999, 1, 1, 0, 0, 0, i, 1, 0)
                    if wday.lower() == time.strftime('%a', wconst).lower():
                        wday = i
                        break
                for i in range(1, 13):
                    mconst = (1999, i, 1, 0, 0, 0, 0, 1, 0)
                    if mon.lower() == time.strftime('%b', mconst).lower():
                        mon = i
                        break
    else:
        year, mon, day, hh, mm, ss, wday, yday, dst = time.localtime(date)
        if dst in (0,1):
            tzname = time.tzname[dst]

    wday = daysofweek[wday]
    mon = months[mon]
    return '{0} {1} {2:2d} {3:02d}:{4:02d}:{5:02d} {6} {7:04d}'.format(
        wday, mon, day, hh, mm, ss, tzname, year)