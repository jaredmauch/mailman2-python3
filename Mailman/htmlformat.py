# Copyright (C) 1998-2018 by the Free Software Foundation, Inc.
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


"""Library for program-based construction of HTML documents.

This module provides classes and functions for programmatically generating
HTML documents. It encapsulates HTML formatting directives in classes that
act as containers for Python objects and, recursively, for nested HTML
formatting objects.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from typing import Any, Dict, List, Optional, Tuple, Union

from Mailman import mm_cfg
from Mailman import Utils
from Mailman.i18n import _, get_translation
from Mailman.CSRFcheck import csrf_token

# Constants
SPACE: str = ' '
EMPTYSTRING: str = ''
NL: str = '\n'


def HTMLFormatObject(item: Any, indent: int) -> str:
    """Format an arbitrary object for HTML output.
    
    Args:
        item: The object to format
        indent: The indentation level
        
    Returns:
        A string representation of the object
    """
    if isinstance(item, str):
        return item
    elif not hasattr(item, "Format"):
        return repr(item)
    else:
        return item.Format(indent)


def CaseInsensitiveKeyedDict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Create a dictionary with case-insensitive keys.
    
    Args:
        d: The input dictionary
        
    Returns:
        A new dictionary with lowercase keys
    """
    result = {}
    for k, v in d.items():
        result[k.lower()] = v
    return result


def DictMerge(destination: Dict[str, Any], fresh_dict: Dict[str, Any]) -> None:
    """Merge one dictionary into another.
    
    Args:
        destination: The dictionary to merge into
        fresh_dict: The dictionary to merge from
    """
    for key, value in fresh_dict.items():
        destination[key] = value


class Table:
    """A class for creating HTML tables.
    
    This class provides methods for building HTML tables with support for
    cell and row attributes, and table-wide options.
    
    Attributes:
        cells: List of rows, each containing a list of cells
        cell_info: Dictionary mapping (row, col) to cell attributes
        row_info: Dictionary mapping row numbers to row attributes
        opts: Table-wide options
    """

    def __init__(self, **table_opts: Any) -> None:
        """Initialize a new Table instance.
        
        Args:
            **table_opts: Table-wide options
        """
        self.cells: List[List[Any]] = []
        self.cell_info: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self.row_info: Dict[int, Dict[str, Any]] = {}
        self.opts = table_opts

    def AddOptions(self, opts: Dict[str, Any]) -> None:
        """Add table options.
        
        Args:
            opts: Dictionary of options to add
        """
        DictMerge(self.opts, opts)

    def SetAllCells(self, cells: List[List[Any]]) -> None:
        """Set all cells in the table.
        
        Args:
            cells: List of rows, each containing a list of cells
        """
        self.cells = cells

    def NewRow(self) -> None:
        """Add a new blank row at the end."""
        self.cells.append([])

    def NewCell(self) -> None:
        """Add a new blank cell at the end of the last row."""
        self.cells[-1].append('')

    def AddRow(self, row: List[Any]) -> None:
        """Add a new row.
        
        Args:
            row: List of cells to add as a new row
        """
        self.cells.append(row)

    def AddCell(self, cell: Any) -> None:
        """Add a new cell to the last row.
        
        Args:
            cell: The cell content to add
        """
        self.cells[-1].append(cell)

    def AddCellInfo(self, row: int, col: int, **kws: Any) -> None:
        """Add information about a specific cell.
        
        Args:
            row: The row number
            col: The column number
            **kws: Cell attributes
        """
        kws = CaseInsensitiveKeyedDict(kws)
        if row not in self.cell_info:
            self.cell_info[row] = {col: kws}
        elif col not in self.cell_info[row]:
            self.cell_info[row][col] = kws
        else:
            DictMerge(self.cell_info[row][col], kws)

    def AddRowInfo(self, row: int, **kws: Any) -> None:
        """Add information about a specific row.
        
        Args:
            row: The row number
            **kws: Row attributes
        """
        kws = CaseInsensitiveKeyedDict(kws)
        if row not in self.row_info:
            self.row_info[row] = kws
        else:
            DictMerge(self.row_info[row], kws)

    def GetCurrentRowIndex(self) -> int:
        """Get the index of the last row.
        
        Returns:
            The index of the last row
        """
        return len(self.cells) - 1

    def GetCurrentCellIndex(self) -> int:
        """Get the index of the last cell in the last row.
        
        Returns:
            The index of the last cell
        """
        return len(self.cells[-1]) - 1

    def ExtractCellInfo(self, info: Dict[str, Any]) -> str:
        """Extract cell information as HTML attributes.
        
        Args:
            info: Dictionary of cell attributes
            
        Returns:
            HTML attribute string
        """
        valid_mods = ['align', 'valign', 'nowrap', 'rowspan', 'colspan', 'bgcolor']
        output = ''

        for key, val in info.items():
            if key not in valid_mods:
                continue
            if key == 'nowrap':
                output = output + ' NOWRAP'
                continue
            else:
                output = output + ' {0}="{1}"'.format(key.upper(), val)

        return output

    def ExtractRowInfo(self, info: Dict[str, Any]) -> str:
        """Extract row information as HTML attributes.
        
        Args:
            info: Dictionary of row attributes
            
        Returns:
            HTML attribute string
        """
        valid_mods = ['align', 'valign', 'bgcolor']
        output = ''

        for key, val in info.items():
            if key not in valid_mods:
                continue
            output = output + ' {0}="{1}"'.format(key.upper(), val)

        return output

    def ExtractTableInfo(self, info: Dict[str, Any]) -> str:
        """Extract table information as HTML attributes.
        
        Args:
            info: Dictionary of table attributes
            
        Returns:
            HTML attribute string
        """
        valid_mods = ['align', 'width', 'border', 'cellspacing', 'cellpadding', 'bgcolor']
        output = ''

        for key, val in info.items():
            if key not in valid_mods:
                continue
            if key == 'border' and val is None:
                output = output + ' BORDER'
                continue
            else:
                output = output + ' {0}="{1}"'.format(key.upper(), val)

        return output

    def FormatCell(self, row: int, col: int, indent: int) -> str:
        """Format a cell as HTML.
        
        Args:
            row: The row number
            col: The column number
            indent: The indentation level
            
        Returns:
            HTML string for the cell
        """
        try:
            my_info = self.cell_info[row][col]
        except (KeyError, IndexError):
            my_info = None

        output = '\n' + ' ' * indent + '<td'
        if my_info:
            output = output + self.ExtractCellInfo(my_info)
        item = self.cells[row][col]
        item_format = HTMLFormatObject(item, indent + 4)
        output = '{0}>{1}</td>'.format(output, item_format)
        return output

    def FormatRow(self, row: int, indent: int) -> str:
        """Format a row as HTML.
        
        Args:
            row: The row number
            indent: The indentation level
            
        Returns:
            HTML string for the row
        """
        try:
            my_info = self.row_info[row]
        except KeyError:
            my_info = None

        output = '\n' + ' ' * indent + '<tr'
        if my_info:
            output = output + self.ExtractRowInfo(my_info)
        output = output + '>'

        for i in range(len(self.cells[row])):
            output = output + self.FormatCell(row, i, indent + 2)

        output = output + '\n' + ' ' * indent + '</tr>'
        return output

    def Format(self, indent: int = 0) -> str:
        """Format the entire table as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the table
        """
        output = '\n' + ' ' * indent + '<table'
        output = output + self.ExtractTableInfo(self.opts)
        output = output + '>'

        for i in range(len(self.cells)):
            output = output + self.FormatRow(i, indent + 2)

        output = output + '\n' + ' ' * indent + '</table>\n'
        return output


class Link:
    """A class for creating HTML links.
    
    This class provides methods for creating HTML anchor tags with support for
    href, text, and target attributes.
    
    Attributes:
        href: The URL the link points to
        text: The text to display for the link
        target: Optional target window/frame for the link
    """
    
    def __init__(self, href: str, text: str, target: Optional[str] = None) -> None:
        """Initialize a new Link instance.
        
        Args:
            href: The URL the link points to
            text: The text to display for the link
            target: Optional target window/frame for the link
        """
        self.href = href
        self.text = text
        self.target = target

    def Format(self, indent: int = 0) -> str:
        """Format the link as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the link
        """
        texpr = ""
        if self.target is not None:
            texpr = ' target="{0}"'.format(self.target)
        return '<a href="{0}"{1}>{2}</a>'.format(
            HTMLFormatObject(self.href, indent),
            texpr,
            HTMLFormatObject(self.text, indent))


class FontSize:
    """FontSize is being deprecated - use FontAttr(..., size="...") instead.
    
    This class provides methods for creating HTML font tags with size attributes.
    It is being deprecated in favor of the more flexible FontAttr class.
    
    Attributes:
        items: List of items to format within the font tag
        size: The font size to apply
    """
    
    def __init__(self, size: str, *items: Any) -> None:
        """Initialize a new FontSize instance.
        
        Args:
            size: The font size to apply
            *items: Items to format within the font tag
        """
        self.items = list(items)
        self.size = size

    def Format(self, indent: int = 0) -> str:
        """Format the font tag as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the font tag
        """
        output = '<font size="{0}">'.format(self.size)
        for item in self.items:
            output = output + HTMLFormatObject(item, indent)
        output = output + '</font>'
        return output


class FontAttr:
    """Present arbitrary font attributes.
    
    This class provides methods for creating HTML font tags with arbitrary
    attributes.
    
    Attributes:
        items: List of items to format within the font tag
        attrs: Dictionary of font attributes
    """
    
    def __init__(self, *items: Any, **kw: Any) -> None:
        """Initialize a new FontAttr instance.
        
        Args:
            *items: Items to format within the font tag
            **kw: Font attributes as keyword arguments
        """
        self.items = list(items)
        self.attrs = kw

    def Format(self, indent: int = 0) -> str:
        """Format the font tag as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the font tag
        """
        seq = []
        for k, v in self.attrs.items():
            seq.append('{0}="{1}"'.format(k, v))
        output = '<font {0}>'.format(SPACE.join(seq))
        for item in self.items:
            output = output + HTMLFormatObject(item, indent)
        output = output + '</font>'
        return output


class Container:
    """A base class for HTML containers.
    
    This class provides methods for managing collections of HTML items and
    formatting them together.
    
    Attributes:
        items: List of items to format
    """
    
    def __init__(self, *items: Any) -> None:
        """Initialize a new Container instance.
        
        Args:
            *items: Items to format
        """
        if not items:
            self.items = []
        else:
            self.items = items

    def AddItem(self, obj: Any) -> None:
        """Add an item to the container.
        
        Args:
            obj: The item to add
        """
        self.items.append(obj)

    def Format(self, indent: int = 0) -> str:
        """Format all items as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for all items
        """
        output = []
        for item in self.items:
            output.append(HTMLFormatObject(item, indent))
        return EMPTYSTRING.join(output)


class Label(Container):
    """A container for right-aligned content.
    
    This class provides methods for creating right-aligned HTML content.
    
    Attributes:
        align: The alignment value (defaults to 'right')
    """
    
    align = 'right'

    def __init__(self, *items: Any) -> None:
        """Initialize a new Label instance.
        
        Args:
            *items: Items to format
        """
        Container.__init__(self, *items)

    def Format(self, indent: int = 0) -> str:
        """Format the content as right-aligned HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the right-aligned content
        """
        return ('<div align="{0}">'.format(self.align) +
                Container.Format(self, indent) +
                '</div>')


# My own standard document template.  YMMV.
# something more abstract would be more work to use...

class Document(Container):
    """A standard HTML document template.
    
    This class provides methods for creating complete HTML documents with
    proper headers, character sets, and styling.
    
    Attributes:
        title: The document title
        language: The document language
        bgcolor: The background color
        suppress_head: Whether to suppress the head section
    """
    
    title: Optional[str] = None
    language: Optional[str] = None
    bgcolor: str = mm_cfg.WEB_BG_COLOR
    suppress_head: bool = False

    def set_language(self, lang: Optional[str] = None) -> None:
        """Set the document language.
        
        Args:
            lang: The language code to set
        """
        self.language = lang

    def set_bgcolor(self, color: str) -> None:
        """Set the document background color.
        
        Args:
            color: The background color to set
        """
        self.bgcolor = color

    def SetTitle(self, title: str) -> None:
        """Set the document title.
        
        Args:
            title: The title to set
        """
        self.title = title

    def Format(self, indent: int = 0, **kws: Any) -> str:
        """Format the document as HTML.
        
        Args:
            indent: The indentation level
            **kws: Additional HTML attributes
            
        Returns:
            HTML string for the complete document
        """
        charset = 'us-ascii'
        if self.language and Utils.IsLanguage(self.language):
            charset = Utils.GetCharSet(self.language)
        output = ['Content-Type: text/html; charset={0}\n'.format(charset)]
        
        if not self.suppress_head:
            kws.setdefault('bgcolor', self.bgcolor)
            tab = ' ' * indent
            output.extend([
                tab,
                '<HTML>',
                '<HEAD>'
            ])
            
            if mm_cfg.IMAGE_LOGOS:
                output.append('<LINK REL="SHORTCUT ICON" HREF="{0}">'.format(
                    mm_cfg.IMAGE_LOGOS + mm_cfg.SHORTCUT_ICON))
            
            # Hit all the bases
            output.append('<META http-equiv="Content-Type" '
                         'content="text/html; charset={0}">'.format(charset))
            
            if self.title:
                output.append('{0}<TITLE>{1}</TITLE>'.format(tab, self.title))
                
            # Add CSS to visually hide some labeling text but allow screen
            # readers to read it.
            output.append("""\
<style type="text/css">
    div.hidden
        {position:absolute;
        left:-10000px;
        top:auto;
        width:1px;
        height:1px;
        overflow:hidden;}
</style>
""")
            
            if mm_cfg.WEB_HEAD_ADD:
                output.append(mm_cfg.WEB_HEAD_ADD)
                
            output.append('{0}</HEAD>'.format(tab))
            
            quals = []
            # Default link colors
            if mm_cfg.WEB_VLINK_COLOR:
                kws.setdefault('vlink', mm_cfg.WEB_VLINK_COLOR)
            if mm_cfg.WEB_ALINK_COLOR:
                kws.setdefault('alink', mm_cfg.WEB_ALINK_COLOR)
            if mm_cfg.WEB_LINK_COLOR:
                kws.setdefault('link', mm_cfg.WEB_LINK_COLOR)
                
            for k, v in kws.items():
                quals.append('{0}="{1}"'.format(k, v))
                
            output.append('{0}<BODY {1}'.format(tab, SPACE.join(quals)))
            
            # Language direction
            direction = Utils.GetDirection(self.language)
            output.append('dir="{0}">'.format(direction))
            
        # Always do this...
        output.append(Container.Format(self, indent))
        
        if not self.suppress_head:
            output.append('{0}</BODY>'.format(tab))
            output.append('{0}</HTML>'.format(tab))
            
        return NL.join(output)

    def addError(self, errmsg: str, tag: Optional[str] = None) -> None:
        """Add an error message to the document.
        
        Args:
            errmsg: The error message to display
            tag: Optional tag to prefix the error message
        """
        if tag is None:
            tag = _('Error: ')
        self.AddItem(Header(3, Bold(FontAttr(
            _(tag), color=mm_cfg.WEB_ERROR_COLOR, size='+2')).Format() +
            Italic(errmsg).Format()))


class HeadlessDocument(Document):
    """Document without head section, for templates that provide their own.
    
    This class extends Document to suppress the head section, allowing
    templates to provide their own head content.
    """
    
    suppress_head: bool = True


class StdContainer(Container):
    """A container with standard HTML tags.
    
    This class provides methods for creating HTML containers with standard
    opening and closing tags.
    
    Attributes:
        tag: The HTML tag to use
    """
    
    def __init__(self, tag: str) -> None:
        """Initialize a new StdContainer instance.
        
        Args:
            tag: The HTML tag to use
        """
        self.tag = tag

    def Format(self, indent: int = 0) -> str:
        """Format the container as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the container
        """
        # If I don't start a new I ignore indent
        output = '<{0}>'.format(self.tag)
        output = output + Container.Format(self, indent)
        output = '{0}</{1}>'.format(output, self.tag)
        return output


class QuotedContainer(Container):
    """A container with quoted content.
    
    This class provides methods for creating HTML containers where the
    content is quoted and escaped.
    
    Attributes:
        tag: The HTML tag to use
    """
    
    def __init__(self, tag: str) -> None:
        """Initialize a new QuotedContainer instance.
        
        Args:
            tag: The HTML tag to use
        """
        self.tag = tag

    def Format(self, indent: int = 0) -> str:
        """Format the container as HTML with quoted content.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the container
        """
        # If I don't start a new I ignore indent
        output = '<{0}>{1}</{2}>'.format(
            self.tag,
            Utils.websafe(Container.Format(self, indent)),
            self.tag)
        return output


class Header(StdContainer):
    """A container for HTML headers.
    
    This class provides methods for creating HTML header tags (h1-h6).
    
    Attributes:
        num: The header level (1-6)
    """
    
    def __init__(self, num: int, *items: Any) -> None:
        """Initialize a new Header instance.
        
        Args:
            num: The header level (1-6)
            *items: Items to format within the header
        """
        self.items = items
        self.tag = 'h{0}'.format(num)


class Address(StdContainer):
    """A container for HTML address tags."""
    tag = 'address'


class Underline(StdContainer):
    """A container for HTML underline tags."""
    tag = 'u'


class Bold(StdContainer):
    """A container for HTML bold tags."""
    tag = 'strong'


class Italic(StdContainer):
    """A container for HTML italic tags."""
    tag = 'em'


class Preformatted(QuotedContainer):
    """A container for HTML preformatted text tags."""
    tag = 'pre'


class Subscript(StdContainer):
    """A container for HTML subscript tags."""
    tag = 'sub'


class Superscript(StdContainer):
    """A container for HTML superscript tags."""
    tag = 'sup'


class Strikeout(StdContainer):
    """A container for HTML strikethrough tags."""
    tag = 'strike'


class Center(StdContainer):
    """A container for HTML center tags."""
    tag = 'center'


class Form(Container):
    """A container for HTML forms.
    
    This class provides methods for creating HTML forms with support for
    actions, methods, and CSRF tokens.
    
    Attributes:
        action: The form action URL
        method: The form submission method
        encoding: The form encoding type
        mlist: The mailing list object
        contexts: The form contexts
        user: The user object
    """
    
    def __init__(self, action: str = '', method: str = 'POST',
                 encoding: Optional[str] = None, mlist: Optional[Any] = None,
                 contexts: Optional[List[str]] = None, user: Optional[Any] = None,
                 *items: Any) -> None:
        """Initialize a new Form instance.
        
        Args:
            action: The form action URL
            method: The form submission method
            encoding: The form encoding type
            mlist: The mailing list object
            contexts: The form contexts
            user: The user object
            *items: Items to format within the form
        """
        Container.__init__(self, *items)
        self.action = action
        self.method = method
        self.encoding = encoding
        self.mlist = mlist
        self.contexts = contexts
        self.user = user

    def set_action(self, action: str) -> None:
        """Set the form action URL.
        
        Args:
            action: The form action URL to set
        """
        self.action = action

    def Format(self, indent: int = 0) -> str:
        """Format the form as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the form
        """
        spaces = ' ' * indent
        encoding = ''
        if self.encoding:
            encoding = 'enctype="{0}"'.format(self.encoding)
        output = '\n{0}<FORM action="{1}" method="{2}" {3}>\n'.format(
            spaces, self.action, self.method, encoding)
            
        if self.mlist:
            output = output + \
                '<input type="hidden" name="csrf_token" value="{0}">\n'.format(
                    csrf_token(self.mlist, self.contexts, self.user))
                    
        output = output + Container.Format(self, indent + 2)
        output = '{0}\n{1}</FORM>\n'.format(output, spaces)
        return output


class InputObj:
    """A base class for HTML input elements.
    
    This class provides methods for creating HTML input elements with
    support for various attributes.
    
    Attributes:
        name: The input name
        type: The input type
        value: The input value
        checked: Whether the input is checked
        kws: Additional input attributes
    """
    
    def __init__(self, name: str, ty: str, value: str, checked: bool,
                 **kws: Any) -> None:
        """Initialize a new InputObj instance.
        
        Args:
            name: The input name
            ty: The input type
            value: The input value
            checked: Whether the input is checked
            **kws: Additional input attributes
        """
        self.name = name
        self.type = ty
        self.value = value
        self.checked = checked
        self.kws = kws

    def Format(self, indent: int = 0) -> str:
        """Format the input as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the input
        """
        charset = get_translation().charset() or 'us-ascii'
        output = ['<INPUT name="{0}" type="{1}" value="{2}"'.format(
            self.name, self.type, self.value)]
            
        for item in self.kws.items():
            output.append('{0}="{1}"'.format(*item))
            
        if self.checked:
            output.append('CHECKED')
            
        output.append('>')
        ret = SPACE.join(output)
        if self.type == 'TEXT' and isinstance(ret, str):
            ret = ret.encode(charset, 'xmlcharrefreplace')
        return ret


class SubmitButton(InputObj):
    """A class for HTML submit buttons."""
    
    def __init__(self, name: str, button_text: str) -> None:
        """Initialize a new SubmitButton instance.
        
        Args:
            name: The button name
            button_text: The button text
        """
        InputObj.__init__(self, name, "SUBMIT", button_text, checked=False)


class PasswordBox(InputObj):
    """A class for HTML password input fields."""
    
    def __init__(self, name: str, value: str = '',
                 size: int = mm_cfg.TEXTFIELDWIDTH) -> None:
        """Initialize a new PasswordBox instance.
        
        Args:
            name: The input name
            value: The input value
            size: The input size
        """
        InputObj.__init__(self, name, "PASSWORD", value, checked=False,
                         size=size)


class TextBox(InputObj):
    """A class for HTML text input fields."""
    
    def __init__(self, name: str, value: str = '',
                 size: int = mm_cfg.TEXTFIELDWIDTH) -> None:
        """Initialize a new TextBox instance.
        
        Args:
            name: The input name
            value: The input value
            size: The input size
        """
        if isinstance(value, str):
            safevalue = Utils.websafe(value)
        else:
            safevalue = value
        InputObj.__init__(self, name, "TEXT", safevalue, checked=False,
                         size=size)


class Hidden(InputObj):
    """A class for HTML hidden input fields."""
    
    def __init__(self, name: str, value: str = '') -> None:
        """Initialize a new Hidden instance.
        
        Args:
            name: The input name
            value: The input value
        """
        InputObj.__init__(self, name, 'HIDDEN', value, checked=False)


class TextArea:
    """A class for HTML textarea elements.
    
    This class provides methods for creating HTML textarea elements with
    support for various attributes.
    
    Attributes:
        name: The textarea name
        text: The textarea content
        rows: The number of rows
        cols: The number of columns
        wrap: The wrap mode
        readonly: Whether the textarea is readonly
    """
    
    def __init__(self, name: str, text: str = '', rows: Optional[int] = None,
                 cols: Optional[int] = None, wrap: str = 'soft',
                 readonly: bool = False) -> None:
        """Initialize a new TextArea instance.
        
        Args:
            name: The textarea name
            text: The textarea content
            rows: The number of rows
            cols: The number of columns
            wrap: The wrap mode
            readonly: Whether the textarea is readonly
        """
        if isinstance(text, str):
            # Double escape HTML entities in non-readonly areas.
            doubleescape = not readonly
            safetext = Utils.websafe(text, doubleescape)
        else:
            safetext = text
        self.name = name
        self.text = safetext
        self.rows = rows
        self.cols = cols
        self.wrap = wrap
        self.readonly = readonly

    def Format(self, indent: int = 0) -> str:
        """Format the textarea as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the textarea
        """
        charset = get_translation().charset() or 'us-ascii'
        output = '<TEXTAREA NAME="{0}"'.format(self.name)
        
        if self.rows:
            output += ' ROWS="{0}"'.format(self.rows)
        if self.cols:
            output += ' COLS="{0}"'.format(self.cols)
        if self.wrap:
            output += ' WRAP="{0}"'.format(self.wrap)
        if self.readonly:
            output += ' READONLY'
            
        output += '>{0}</TEXTAREA>'.format(self.text)
        
        if isinstance(output, str):
            output = output.encode(charset, 'xmlcharrefreplace')
        return output


class FileUpload(InputObj):
    """A class for HTML file upload input fields."""
    
    def __init__(self, name: str, rows: Optional[int] = None,
                 cols: Optional[int] = None, **kws: Any) -> None:
        """Initialize a new FileUpload instance.
        
        Args:
            name: The input name
            rows: The number of rows
            cols: The number of columns
            **kws: Additional input attributes
        """
        InputObj.__init__(self, name, 'FILE', '', checked=False, **kws)


class RadioButton(InputObj):
    """A class for HTML radio button input fields."""
    
    def __init__(self, name: str, value: str, checked: bool = False,
                 **kws: Any) -> None:
        """Initialize a new RadioButton instance.
        
        Args:
            name: The input name
            value: The input value
            checked: Whether the radio button is checked
            **kws: Additional input attributes
        """
        InputObj.__init__(self, name, 'RADIO', value, checked=checked, **kws)


class CheckBox(InputObj):
    """A class for HTML checkbox input fields."""
    
    def __init__(self, name: str, value: str, checked: bool = False,
                 **kws: Any) -> None:
        """Initialize a new CheckBox instance.
        
        Args:
            name: The input name
            value: The input value
            checked: Whether the checkbox is checked
            **kws: Additional input attributes
        """
        InputObj.__init__(self, name, "CHECKBOX", value, checked=checked, **kws)


class VerticalSpacer:
    """A class for HTML vertical spacing elements."""
    
    def __init__(self, size: int = 10) -> None:
        """Initialize a new VerticalSpacer instance.
        
        Args:
            size: The size of the spacer
        """
        self.size = size
        
    def Format(self, indent: int = 0) -> str:
        """Format the spacer as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the spacer
        """
        output = '<spacer type="vertical" height="{0}">'.format(self.size)
        return output


class WidgetArray:
    """A base class for arrays of HTML input widgets.
    
    This class provides methods for creating arrays of HTML input widgets
    with support for horizontal and vertical layouts.
    
    Attributes:
        name: The input name
        button_names: List of button names
        checked: Whether buttons are checked
        horizontal: Whether to layout horizontally
        values: List of button values
    """
    
    Widget = None

    def __init__(self, name: str, button_names: List[str], checked: Any,
                 horizontal: bool, values: List[Any]) -> None:
        """Initialize a new WidgetArray instance.
        
        Args:
            name: The input name
            button_names: List of button names
            checked: Whether buttons are checked
            horizontal: Whether to layout horizontally
            values: List of button values
        """
        self.name = name
        self.button_names = button_names
        self.checked = checked
        self.horizontal = horizontal
        self.values = values
        assert len(values) == len(button_names)
        # Don't assert `checked' because for RadioButtons it is a scalar while
        # for CheckedBoxes it is a vector.  Subclasses will assert length.

    def ischecked(self, i: int) -> bool:
        """Check if a button is checked.
        
        Args:
            i: The button index
            
        Returns:
            Whether the button is checked
        """
        raise NotImplemented

    def Format(self, indent: int = 0) -> str:
        """Format the widget array as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the widget array
        """
        t = Table(cellspacing=5)
        items = []
        for i, name, value in zip(range(len(self.button_names)),
                                self.button_names,
                                self.values):
            ischecked = (self.ischecked(i))
            item = ('<label>' +
                   self.Widget(self.name, value, ischecked).Format() +
                   name + '</label>')
            items.append(item)
            if not self.horizontal:
                t.AddRow(items)
                items = []
        if self.horizontal:
            t.AddRow(items)
        return t.Format(indent)


class RadioButtonArray(WidgetArray):
    """A class for arrays of HTML radio buttons."""
    
    Widget = RadioButton

    def __init__(self, name: str, button_names: List[str],
                 checked: Optional[int] = None, horizontal: bool = True,
                 values: Optional[List[Any]] = None) -> None:
        """Initialize a new RadioButtonArray instance.
        
        Args:
            name: The input name
            button_names: List of button names
            checked: The index of the checked button
            horizontal: Whether to layout horizontally
            values: List of button values
        """
        if values is None:
            values = list(range(len(button_names)))
        # BAW: assert checked is a scalar...
        WidgetArray.__init__(self, name, button_names, checked, horizontal,
                           values)

    def ischecked(self, i: int) -> bool:
        """Check if a radio button is checked.
        
        Args:
            i: The button index
            
        Returns:
            Whether the button is checked
        """
        return self.checked == i


class CheckBoxArray(WidgetArray):
    """A class for arrays of HTML checkboxes."""
    
    Widget = CheckBox

    def __init__(self, name: str, button_names: List[str],
                 checked: Optional[List[bool]] = None, horizontal: bool = False,
                 values: Optional[List[Any]] = None) -> None:
        """Initialize a new CheckBoxArray instance.
        
        Args:
            name: The input name
            button_names: List of button names
            checked: List of checked states
            horizontal: Whether to layout horizontally
            values: List of button values
        """
        if checked is None:
            checked = [False] * len(button_names)
        else:
            assert len(checked) == len(button_names)
        if values is None:
            values = list(range(len(button_names)))
        WidgetArray.__init__(self, name, button_names, checked, horizontal,
                           values)

    def ischecked(self, i: int) -> bool:
        """Check if a checkbox is checked.
        
        Args:
            i: The button index
            
        Returns:
            Whether the button is checked
        """
        return self.checked[i]


class UnorderedList(Container):
    """A container for HTML unordered lists."""
    
    def Format(self, indent: int = 0) -> str:
        """Format the list as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the list
        """
        spaces = ' ' * indent
        output = '\n{0}<ul>\n'.format(spaces)
        for item in self.items:
            output = output + '{0}<li>{1}\n'.format(
                spaces, HTMLFormatObject(item, indent + 2))
        output = output + '{0}</ul>\n'.format(spaces)
        return output


class OrderedList(Container):
    """A container for HTML ordered lists."""
    
    def Format(self, indent: int = 0) -> str:
        """Format the list as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the list
        """
        spaces = ' ' * indent
        output = '\n{0}<ol>\n'.format(spaces)
        for item in self.items:
            output = output + '{0}<li>{1}\n'.format(
                spaces, HTMLFormatObject(item, indent + 2))
        output = output + '{0}</ol>\n'.format(spaces)
        return output


class DefinitionList(Container):
    """A container for HTML definition lists."""
    
    def Format(self, indent: int = 0) -> str:
        """Format the list as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the list
        """
        spaces = ' ' * indent
        output = '\n{0}<dl>\n'.format(spaces)
        for dt, dd in self.items:
            output = output + '{0}<dt>{1}\n<dd>{2}\n'.format(
                spaces,
                HTMLFormatObject(dt, indent + 2),
                HTMLFormatObject(dd, indent + 2))
        output = output + '{0}</dl>\n'.format(spaces)
        return output


# Logo constants
#
# These are the URLs which the image logos link to.  The Mailman home page now
# points at the gnu.org site instead of the www.list.org mirror.
#
from mm_cfg import MAILMAN_URL
PYTHON_URL: str = 'http://www.python.org/'
GNU_URL: str = 'http://www.gnu.org/'

# The names of the image logo files.  These are concatenated onto
# mm_cfg.IMAGE_LOGOS (not urljoined).
DELIVERED_BY: str = 'mailman.jpg'
PYTHON_POWERED: str = 'PythonPowered.png'
GNU_HEAD: str = 'gnu-head-tiny.jpg'


def MailmanLogo() -> Table:
    """Create a table containing the Mailman, Python, and GNU logos.
    
    Returns:
        A Table instance containing the logos and version information
    """
    t = Table(border=0, width='100%')
    if mm_cfg.IMAGE_LOGOS:
        def logo(file: str) -> str:
            """Generate the full URL for a logo file.
            
            Args:
                file: The logo filename
                
            Returns:
                The full URL for the logo
            """
            return mm_cfg.IMAGE_LOGOS + file
            
        mmlink = '<img src="{0}" alt="Delivered by Mailman" border=0><br>version {1}'.format(
            logo(DELIVERED_BY), mm_cfg.VERSION)
        pylink = '<img src="{0}" alt="Python Powered" border=0>'.format(
            logo(PYTHON_POWERED))
        gnulink = '<img src="{0}" alt="GNU\'s Not Unix" border=0>'.format(
            logo(GNU_HEAD))
        t.AddRow([mmlink, pylink, gnulink])
    else:
        # use only textual links
        version = mm_cfg.VERSION
        mmlink = Link(MAILMAN_URL,
                     _('Delivered by Mailman<br>version {0}').format(version))
        pylink = Link(PYTHON_URL, _('Python Powered'))
        gnulink = Link(GNU_URL, _("GNU's Not Unix"))
        t.AddRow([mmlink, pylink, gnulink])
    return t


class SelectOptions:
    """A class for HTML select elements.
    
    This class provides methods for creating HTML select elements with
    support for multiple selection and option groups.
    
    Attributes:
        varname: The select element name
        values: List of option values
        legend: List of option labels
        size: The number of visible options
        multiple: Whether multiple selection is allowed
        selected: Tuple of selected option indices
    """
    
    def __init__(self, varname: str, values: List[str], legend: List[str],
                 selected: Union[int, List[int], Tuple[int, ...]] = 0,
                 size: int = 1, multiple: Optional[bool] = None) -> None:
        """Initialize a new SelectOptions instance.
        
        Args:
            varname: The select element name
            values: List of option values
            legend: List of option labels
            selected: Index or indices of selected options
            size: The number of visible options
            multiple: Whether multiple selection is allowed
        """
        self.varname = varname
        self.values = values
        self.legend = legend
        self.size = size
        self.multiple = multiple
        
        # Convert selected to a tuple of indices
        if not multiple:
            if isinstance(selected, int):
                self.selected = (selected,)
            elif isinstance(selected, tuple):
                self.selected = (selected[0],)
            elif isinstance(selected, list):
                self.selected = (selected[0],)
            else:
                self.selected = (0,)

    def Format(self, indent: int = 0) -> str:
        """Format the select element as HTML.
        
        Args:
            indent: The indentation level
            
        Returns:
            HTML string for the select element
        """
        spaces = ' ' * indent
        items = min(len(self.values), len(self.legend))

        # If there are no arguments, we return nothing to avoid errors
        if items == 0:
            return ''

        text = '\n{0}<select name="{1}"'.format(spaces, self.varname)
        if self.size > 1:
            text = text + ' size="{0}"'.format(self.size)
        if self.multiple:
            text = text + ' multiple'
        text = text + '>\n'

        for i in range(items):
            if i in self.selected:
                checked = ' selected'
            else:
                checked = ''

            opt = ' <option value="{0}"{1}>{2}</option>'.format(
                self.values[i], checked, self.legend[i])
            text = text + spaces + opt + '\n'

        return text + spaces + '</select>'
}