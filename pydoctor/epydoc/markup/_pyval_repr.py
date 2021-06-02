# epydoc -- Marked-up Representations for Python Values
#
# Copyright (C) 2005 Edward Loper
# Author: Edward Loper <edloper@loper.org>
# URL: <http://epydoc.sf.net>
#

"""
Syntax highlighter for Python values.  Currently provides special
colorization support for:

  - lists, tuples, sets, frozensets, dicts
  - numbers
  - strings
  - compiled regexps

The highlighter also takes care of line-wrapping, and automatically
stops generating repr output as soon as it has exceeded the specified
number of lines (which should make it faster than pprint for large
values).  It does I{not} bother to do automatic cycle detection,
because maxlines is typically around 5, so it's really not worth it.

The syntax-highlighted output is encoded using a
L{ParsedDocstring}, which can then be used to generate output in
a variety of formats.
"""

__docformat__ = 'epytext en'

# Implementation note: we use exact tests for classes (list, etc)
# rather than using isinstance, because subclasses might override
# __repr__.

import re
import functools
import sre_parse, sre_constants
from typing import Any, Union, Optional, Mapping
from pydoctor.epydoc.markup.epytext import Element, ParsedEpytextDocstring

def is_re_pattern(pyval):
    return type(pyval).__name__ == 'SRE_Pattern'

def decode_with_backslashreplace(s: bytes) -> str:
    r"""
    Convert the given 8-bit string into unicode, treating any
    character c such that ord(c)<128 as an ascii character, and
    converting any c such that ord(c)>128 into a backslashed escape
    sequence.
        >>> decode_with_backslashreplace('abc\xff\xe8')
        u'abc\\xff\\xe8'
    """
    # s.encode('string-escape') is not appropriate here, since it
    # also adds backslashes to some ascii chars (eg \ and ').

    return (s
            .decode('latin1')
            .encode('ascii', 'backslashreplace')
            .decode('ascii'))

class _ColorizerState:
    """
    An object uesd to keep track of the current state of the pyval
    colorizer.  The L{mark()}/L{restore()} methods can be used to set
    a backup point, and restore back to that backup point.  This is
    used by several colorization methods that first try colorizing
    their object on a single line (setting linebreakok=False); and
    then fall back on a multi-line output if that fails.  The L{score}
    variable is used to keep track of a 'score', reflecting how good
    we think this repr is.  E.g., unhelpful values like '<Foo instance
    at 0x12345>' get low scores.  If the score is too low, we'll use
    the parse-derived repr instead.
    """
    def __init__(self):
        self.result = []
        self.charpos = 0
        self.lineno = 1
        self.linebreakok = True

        #: How good this represention is?
        self.score = 0

    def mark(self):
        return (len(self.result), self.charpos,
                self.lineno, self.linebreakok, self.score)

    def restore(self, mark):
        n, self.charpos, self.lineno, self.linebreakok, self.score = mark
        del self.result[n:]

class _Maxlines(Exception):
    """A control-flow exception that is raised when PyvalColorizer
    exeeds the maximum number of allowed lines."""

class _Linebreak(Exception):
    """A control-flow exception that is raised when PyvalColorizer
    generates a string containing a newline, but the state object's
    linebreakok variable is False."""

class ColorizedPyvalRepr(ParsedEpytextDocstring):
    """
    @ivar score: A score, evaluating how good this repr is.
    @ivar is_complete: True if this colorized repr completely describes
       the object.
    """
    def __init__(self, tree, score, is_complete):
        super().__init__(tree, ())
        self.score = score
        self.is_complete = is_complete

def colorize_pyval(pyval, parse_repr=None, min_score=None,
                   linelen=75, maxlines=5, linebreakok=True, sort=True):
    return PyvalColorizer(linelen, maxlines, linebreakok, sort).colorize(
        pyval, parse_repr, min_score)


class PyvalColorizer:
    """
    Syntax highlighter for Python values.
    """

    def __init__(self, linelen=75, maxlines=5, linebreakok=True, sort=True):
        self.linelen = linelen
        self.maxlines = maxlines
        self.linebreakok = linebreakok
        self.sort = sort

    #////////////////////////////////////////////////////////////
    # Colorization Tags & other constants
    #////////////////////////////////////////////////////////////

    GROUP_TAG = 'variable-group'     # e.g., "[" and "]"
    COMMA_TAG = 'variable-op'        # The "," that separates elements
    COLON_TAG = 'variable-op'        # The ":" in dictionaries
    CONST_TAG = None                 # None, True, False
    NUMBER_TAG = None                # ints, floats, etc
    QUOTE_TAG = 'variable-quote'     # Quotes around strings.
    STRING_TAG = 'variable-string'   # Body of string literals

    RE_CHAR_TAG = None
    RE_GROUP_TAG = 're-group'
    RE_REF_TAG = 're-ref'
    RE_OP_TAG = 're-op'
    RE_FLAGS_TAG = 're-flags'

    ELLIPSIS = Element('code', '...', css_class='variable-ellipsis')
    LINEWRAP = Element('symbol', 'crarr')
    UNKNOWN_REPR = Element('code', '??', css_class='variable-unknown')

    GENERIC_OBJECT_RE = re.compile(r'^<.* at 0x[0-9a-f]+>$', re.IGNORECASE)

    def _str_escape(self, s):
        def enc(c):
            if c == "'":
                return r"\'"
            elif ord(c) <= 0xff:
                return c.encode('unicode-escape').decode('utf-8')
            else:
                return c
        return ''.join(map(enc, s))

    def _bytes_escape(self, b):
        return repr(b)[2:-1]

    def _unicode_escape(self, u):
        return u

    #////////////////////////////////////////////////////////////
    # Entry Point
    #////////////////////////////////////////////////////////////

    def colorize(self, pyval: Any, min_score: Optional[int] = None) -> ColorizedPyvalRepr:
        """
        @return: A L{ColorizedPyvalRepr} describing the given pyval.
        """

        # Create an object to keep track of the colorization.
        state = _ColorizerState()
        state.linebreakok = self.linebreakok
        # Colorize the value.  If we reach maxlines, then add on an
        # ellipsis marker and call it a day.
        try:
            self._colorize(pyval, state)
            is_complete = True
        except (_Maxlines, _Linebreak):
            if self.linebreakok:
                state.result.append('\n')
                state.result.append(self.ELLIPSIS)
            else:
                if state.result[-1] is self.LINEWRAP:
                    state.result.pop()
                self._trim_result(state.result, 3)
                state.result.append(self.ELLIPSIS)
            is_complete = False
        # If we didn't score high enough, then use UNKNOWN_REPR
        if (min_score is not None and state.score < min_score):
            state.result = [PyvalColorizer.UNKNOWN_REPR]
        # Put it all together.
        tree = Element('epytext', *state.result)
        return ColorizedPyvalRepr(tree, state.score, is_complete)
    
    def _colorize(self, pyval: Any, state: _ColorizerState) -> None:
        pyval_type = type(pyval)
        state.score += 1
        
        if pyval is None or pyval is True or pyval is False:
            self._output(str(pyval), self.CONST_TAG, state)
        elif issubclass(pyval_type, (int, float, complex)):
            self._output(str(pyval), self.NUMBER_TAG, state)
        elif issubclass(pyval_type, str):
            self._colorize_str(pyval, state, '', self._str_escape)
        elif issubclass(pyval_type, bytes):
            self._colorize_str(pyval, state, b'b', self._bytes_escape)
        elif issubclass(pyval_type, tuple):
            self._multiline(self._colorize_iter, pyval, state, '(', ')')
        elif issubclass(pyval_type, set):
            self._multiline(self._colorize_iter, self._sort(pyval),
                            state, 'set([', '])')
        elif issubclass(pyval_type, frozenset):
            self._multiline(self._colorize_iter, self._sort(pyval),
                            state, 'frozenset([', '])')
        elif issubclass(pyval_type, dict):
            self._multiline(self._colorize_dict,
                            self._sort(list(pyval.items())),
                            state, '{', '}')
        elif issubclass(pyval_type, list):
            self._multiline(self._colorize_iter, pyval, state, '[', ']')
        elif is_re_pattern(pyval):
            self._colorize_re(pyval, state)
        else:
            try:
                pyval_repr = repr(pyval)
                if not isinstance(pyval_repr, str):
                    pyval_repr = str(pyval_repr)
            except KeyboardInterrupt:
                raise
            except:
                state.score -= 100
                state.result.append(self.UNKNOWN_REPR)
            else:
                if self.GENERIC_OBJECT_RE.match(pyval_repr):
                    state.score -= 5
                self._output(pyval_repr, None, state)

    def _sort(self, items):
        if not self.sort: return items
        try: return sorted(items)
        except KeyboardInterrupt: raise
        except: return items

    def _trim_result(self, result, num_chars):
        while num_chars > 0:
            if not result: 
                return
            if isinstance(result[-1], Element):
                assert len(result[-1].children) == 1
                trim = min(num_chars, len(result[-1].children[0]))
                result[-1].children[0] = result[-1].children[0][:-trim]
                if not result[-1].children[0]: result.pop()
                num_chars -= trim
            else:
                trim = min(num_chars, len(result[-1]))
                result[-1] = result[-1][:-trim]
                if not result[-1]: result.pop()
                num_chars -= trim

    #////////////////////////////////////////////////////////////
    # Object Colorization Functions
    #////////////////////////////////////////////////////////////

    def _multiline(self, func, pyval, state, *args):
        """
        Helper for container-type colorizers.  First, try calling
        C{func(pyval, state, *args)} with linebreakok set to false;
        and if that fails, then try again with it set to true.
        """
        linebreakok = state.linebreakok
        mark = state.mark()

        try:
            state.linebreakok = False
            func(pyval, state, *args)
            state.linebreakok = linebreakok

        except _Linebreak:
            if not linebreakok:
                raise
            state.restore(mark)
            func(pyval, state, *args)

    def _colorize_iter(self, pyval, state, prefix, suffix):
        self._output(prefix, self.GROUP_TAG, state)
        indent = state.charpos
        for i, elt in enumerate(pyval):
            if i>=1:
                if state.linebreakok:
                    self._output(',', self.COMMA_TAG, state)
                    self._output('\n'+' '*indent, None, state)
                else:
                    self._output(', ', self.COMMA_TAG, state)
            self._colorize(elt, state)
        self._output(suffix, self.GROUP_TAG, state)

    def _colorize_dict(self, items: Mapping[Any, Any], state: _ColorizerState, prefix: str, suffix: str):
        self._output(prefix, self.GROUP_TAG, state)
        indent = state.charpos
        for i, (key, val) in enumerate(items):
            if i>=1:
                if state.linebreakok:
                    self._output(b',', self.COMMA_TAG, state)
                    self._output(b'\n'+b' '*indent, None, state)
                else:
                    self._output(b', ', self.COMMA_TAG, state)
            self._colorize(key, state)
            self._output(b': ', self.COLON_TAG, state)
            self._colorize(val, state)
        self._output(suffix, self.GROUP_TAG, state)

    def _colorize_str(self, pyval, state, prefix, escape_fcn):
        # TODO: Double check implementation bytes/str
        s = functools.partial(bytes, encoding='utf-8', errors='replace') \
            if isinstance(pyval, bytes) else str
        # Decide which quote to use.
        if s('\n') in pyval and state.linebreakok: 
            quote = s("'''")
        else: 
            quote = s("'")
        # Divide the string into lines.
        if state.linebreakok:
            lines = pyval.split(s('\n'))
        else:
            lines = [pyval]
        # Open quote.
        self._output(prefix+quote, self.QUOTE_TAG, state)
        # Body
        for i, line in enumerate(lines):
            if i>0: 
                self._output(s('\n'), None, state)
            if escape_fcn:
                line = escape_fcn(line)
            self._output(line, self.STRING_TAG, state)
        # Close quote.
        self._output(quote, self.QUOTE_TAG, state)

    def _colorize_re(self, pyval, state):
        # Extract the flag & pattern from the regexp.
        pat, flags = pyval.pattern, pyval.flags

        # Parse the regexp pattern.
        tree = sre_parse.parse(pat, flags)
        groups = dict([(num,name) for (name,num) in
                       tree.pattern.groupdict.items()])
        # Colorize it!
        self._output(b"re.compile(r'", None, state)
        self._colorize_re_flags(flags, state)
        self._colorize_re_tree(tree, state, True, groups)
        self._output(b"')", None, state)

    def _colorize_re_flags(self, flags, state):
        if flags:
            flags = [c for (c,n) in sorted(sre_parse.FLAGS.items())
                     if (n&flags)]
            flags = b'(?%s)' % b''.join(flags)
            self._output(flags, self.RE_FLAGS_TAG, state)

    def _colorize_re_tree(self, tree, state, noparen, groups):
        b = functools.partial(bytes, encoding='utf-8', errors='replace')
        assert noparen in (True, False)
        try:
            if len(tree) > 1 and not noparen:
                self._output(b'(', self.RE_GROUP_TAG, state)
        except TypeError:
            print("tree: %r" % tree)
            raise
        for elt in tree:
            op = elt[0]
            args = elt[1]

            if op == sre_constants.LITERAL:
                c = chr(args)
                # Add any appropriate escaping.
                if c in '.^$\\*+?{}[]|()\'': c = b'\\' + b(c)
                elif c == '\t': c = b'\\t'
                elif c == '\r': c = '\\r'
                elif c == '\n': c = '\\n'
                elif c == '\f': c = '\\f'
                elif c == '\v': c = '\\v'
                elif ord(c) > 0xffff: c = b(r'\U%08x') % ord(c)
                elif ord(c) > 0xff: c = b(r'\u%04x') % ord(c)
                elif ord(c)<32 or ord(c)>=127: c = b(r'\x%02x') % ord(c)
                self._output(c, self.RE_CHAR_TAG, state)

            elif op == sre_constants.ANY:
                self._output(b'.', self.RE_CHAR_TAG, state)

            elif op == sre_constants.BRANCH:
                if args[0] is not None:
                    raise ValueError('Branch expected None arg but got %s'
                                     % args[0])
                for i, item in enumerate(args[1]):
                    if i > 0:
                        self._output(b'|', self.RE_OP_TAG, state)
                    self._colorize_re_tree(item, state, True, groups)

            elif op == sre_constants.IN:
                if (len(args) == 1 and args[0][0] == sre_constants.CATEGORY):
                    self._colorize_re_tree(args, state, False, groups)
                else:
                    self._output(b'[', self.RE_GROUP_TAG, state)
                    self._colorize_re_tree(args, state, True, groups)
                    self._output(b']', self.RE_GROUP_TAG, state)

            elif op == sre_constants.CATEGORY:
                if args == sre_constants.CATEGORY_DIGIT: val = b(r'\d')
                elif args == sre_constants.CATEGORY_NOT_DIGIT: val = b(r'\D')
                elif args == sre_constants.CATEGORY_SPACE: val = b(r'\s')
                elif args == sre_constants.CATEGORY_NOT_SPACE: val = b(r'\S')
                elif args == sre_constants.CATEGORY_WORD: val = b(r'\w')
                elif args == sre_constants.CATEGORY_NOT_WORD: val = b(r'\W')
                else: raise ValueError('Unknown category %s' % args)
                self._output(val, self.RE_CHAR_TAG, state)

            elif op == sre_constants.AT:
                if args == sre_constants.AT_BEGINNING_STRING: val = b(r'\A')
                elif args == sre_constants.AT_BEGINNING: val = b(r'^')
                elif args == sre_constants.AT_END: val = b(r'$')
                elif args == sre_constants.AT_BOUNDARY: val = b(r'\b')
                elif args == sre_constants.AT_NON_BOUNDARY: val = b(r'\B')
                elif args == sre_constants.AT_END_STRING: val = b(r'\Z')
                else: raise ValueError('Unknown position %s' % args)
                self._output(val, self.RE_CHAR_TAG, state)

            elif op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT):
                minrpt = args[0]
                maxrpt = args[1]
                if maxrpt == sre_constants.MAXREPEAT:
                    if minrpt == 0:   val = b('*')
                    elif minrpt == 1: val = b('+')
                    else: val = b('{%d,}') % (minrpt)
                elif minrpt == 0:
                    if maxrpt == 1: val = b('?')
                    else: val = b('{,%d}') % (maxrpt)
                elif minrpt == maxrpt:
                    val = b('{%d}') % (maxrpt)
                else:
                    val = b('{%d,%d}') % (minrpt, maxrpt)
                if op == sre_constants.MIN_REPEAT:
                    val += b('?')

                self._colorize_re_tree(args[2], state, False, groups)
                self._output(val, self.RE_OP_TAG, state)

            elif op == sre_constants.SUBPATTERN:
                if args[0] is None:
                    self._output(b('(?:'), self.RE_GROUP_TAG, state)
                elif args[0] in groups:
                    self._output(b('(?P<'), self.RE_GROUP_TAG, state)
                    self._output(groups[args[0]], self.RE_REF_TAG, state)
                    self._output(b('>'), self.RE_GROUP_TAG, state)
                elif isinstance(args[0], int):
                    # This is cheating:
                    self._output(b('('), self.RE_GROUP_TAG, state)
                else:
                    self._output(b('(?P<'), self.RE_GROUP_TAG, state)
                    self._output(args[0], self.RE_REF_TAG, state)
                    self._output(b('>'), self.RE_GROUP_TAG, state)
                self._colorize_re_tree(args[3], state, True, groups)
                self._output(b(')'), self.RE_GROUP_TAG, state)

            elif op == sre_constants.GROUPREF:
                self._output(b('\\%d') % args, self.RE_REF_TAG, state)

            elif op == sre_constants.RANGE:
                self._colorize_re_tree( ((sre_constants.LITERAL, args[0]),),
                                        state, False, groups )
                self._output(b('-'), self.RE_OP_TAG, state)
                self._colorize_re_tree( ((sre_constants.LITERAL, args[1]),),
                                        state, False, groups )

            elif op == sre_constants.NEGATE:
                self._output(b('^'), self.RE_OP_TAG, state)

            elif op == sre_constants.ASSERT:
                if args[0] > 0:
                    self._output(b('(?='), self.RE_GROUP_TAG, state)
                else:
                    self._output(b('(?<='), self.RE_GROUP_TAG, state)
                self._colorize_re_tree(args[1], state, True, groups)
                self._output(b(')'), self.RE_GROUP_TAG, state)

            elif op == sre_constants.ASSERT_NOT:
                if args[0] > 0:
                    self._output(b('(?!'), self.RE_GROUP_TAG, state)
                else:
                    self._output(b('(?<!'), self.RE_GROUP_TAG, state)
                self._colorize_re_tree(args[1], state, True, groups)
                self._output(b(')'), self.RE_GROUP_TAG, state)

            elif op == sre_constants.NOT_LITERAL:
                self._output(b('[^'), self.RE_GROUP_TAG, state)
                self._colorize_re_tree( ((sre_constants.LITERAL, args),),
                                        state, False, groups )
                self._output(b(']'), self.RE_GROUP_TAG, state)
            else:
                raise RuntimeError("Error colorizing regexp: unknown elt %r" % elt)
        if len(tree) > 1 and not noparen:
            self._output(b(')'), self.RE_GROUP_TAG, state)

    #////////////////////////////////////////////////////////////
    # Output function
    #////////////////////////////////////////////////////////////

    def _output(self, s: Union[str, bytes], css_class: str, state):
        """
        Add the string `s` to the result list, tagging its contents
        with css class `css_class`.  Any lines that go beyond `self.linelen` will
        be line-wrapped.  If the total number of lines exceeds
        `self.maxlines`, then raise a `_Maxlines` exception.
        """
        # Make sure the string is unicode.
        if isinstance(s, bytes):
            s = decode_with_backslashreplace(s)

        # Split the string into segments.  The first segment is the
        # content to add to the current line, and the remaining
        # segments are new lines.
        segments = s.split('\n')

        for i, segment in enumerate(segments):
            # If this isn't the first segment, then add a newline to
            # split it from the previous segment.
            if i > 0:
                if (state.lineno+1) > self.maxlines:
                    raise _Maxlines()
                if not state.linebreakok:
                    raise _Linebreak()
                state.result.append('\n')
                state.lineno += 1
                state.charpos = 0

            # If the segment fits on the current line, then just call
            # markup to tag it, and store the result.
            if state.charpos + len(segment) <= self.linelen:
                state.charpos += len(segment)
                if css_class:
                    segment = Element('code', segment, css_class=css_class)
                state.result.append(segment)

            # If the segment doesn't fit on the current line, then
            # line-wrap it, and insert the remainder of the line into
            # the segments list that we're iterating over.  (We'll go
            # the the beginning of the next line at the start of the
            # next iteration through the loop.)
            else:
                split = self.linelen-state.charpos
                segments.insert(i+1, segment[split:])
                segment = segment[:split]
                if css_class:
                    segment = Element('code', segment, css_class=css_class)
                state.result += [segment, self.LINEWRAP]
