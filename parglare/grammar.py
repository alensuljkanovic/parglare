# -*- coding: utf-8 -*-
from __future__ import unicode_literals, print_function
from os import path
import sys
import re
import itertools
from parglare.six import add_metaclass
from parglare.exceptions import GrammarError
from parglare.actions import pass_single, pass_none, collect, collect_sep
from parglare.termui import prints, s_emph, s_header, a_print, h_print
from parglare import termui

if sys.version < '3':
    text = unicode  # NOQA
else:
    text = str

# Associativity
ASSOC_NONE = 0
ASSOC_LEFT = 1
ASSOC_RIGHT = 2

# Priority
DEFAULT_PRIORITY = 10

# Multiplicity
MULT_ONE = '1'
MULT_OPTIONAL = '0..1'
MULT_ONE_OR_MORE = '1..*'
MULT_ZERO_OR_MORE = '0..*'

RESERVED_SYMBOL_NAMES = ['EOF', 'STOP', 'EMPTY']
SPECIAL_SYMBOL_NAMES = ['KEYWORD', 'LAYOUT']


def escape(instr):
    return instr.replace('\n', r'\n').replace('\t', r'\t')


class GrammarSymbol(object):
    """
    Represents an abstract grammar symbol.

    Attributes:
    name(str): The name of this grammar symbol.
    action_name(string): Name of common/user action given in the grammar.
    action(callable): Resolved action given by the user. Overrides grammar
        action if provided. If not provided by the user defaults to
        grammar_action.
    grammar_action(callable): Resolved action given in the grammar.
    """
    def __init__(self, name):
        self.name = escape(name)
        self.action_name = None
        self.action = None
        self.grammar_action = None
        self._hash = hash(name)

    def __unicode__(self):
        return str(self)

    def __str__(self):
        return self.name

    def __repr__(self):
        return "{}({})".format(type(self).__name__, str(self))

    def __hash__(self):
        return self._hash

    def __eq__(self, other):
        return self.name == other.name

    def __ne__(self, other):
        return not self == other


class NonTerminal(GrammarSymbol):
    """Represents a non-termial symbol of the grammar.

    Attributes:
    productions(list of Production): A list of alternative productions for
        this NonTerminal.
    """
    def __init__(self, name, productions=None):
        super(NonTerminal, self).__init__(name)
        self.productions = productions if productions else []


class Terminal(GrammarSymbol):
    """Represent a terminal symbol of the grammar.

    Attributes:
    prior(int): Priority used for lexical disambiguation.
    dynamic(bool): Should dynamic disambiguation be called to resolve conflict
        involving this terminal.
    finish(bool): Used for scanning optimization. If this terminal is `finish`
        no other recognizers will be checked if this succeeds. If not provided
        in the grammar implicit rules will be used during table construction.
    prefer(bool): Prefer this recognizer in case of multiple recognizers match
        at the same place and implicit disambiguation doesn't resolve.
    keyword(bool): `True` if this Terminal represents keyword. `False` by
        default.

    recognizer(callable): Called with input list of objects and position in the
        stream. Should return a sublist of recognized objects. The sublist
        should be rooted at the given position.
    """
    def __init__(self, name, recognizer=None):
        self.prior = DEFAULT_PRIORITY
        self.recognizer = recognizer if recognizer else StringRecognizer(name)
        self.finish = None
        self.prefer = False
        self.dynamic = False
        self.keyword = False
        super(Terminal, self).__init__(name)


class Reference(object):
    """
    A name reference to a GrammarSymbol used for cross-resolving during
    grammar construction.
    """
    def __init__(self, name, module_name=None):
        self.name = name
        self.module_name = module_name

    def __repr__(self):
        if self.module_name:
            return "{}.{}".format(self.module_name, self.name)
        else:
            return self.name


class Recognizer(object):
    """
    Recognizers are callables capable of recognizing low-level patterns
    (a.k.a tokens) in the input.
    """
    def __init__(self, name):
        self.name = name


class StringRecognizer(Recognizer):
    def __init__(self, value, ignore_case=False):
        super(StringRecognizer, self).__init__(value)
        self.value = value
        self.ignore_case = ignore_case
        self.value_cmp = value.lower() if ignore_case else value

    def __call__(self, in_str, pos):
        if self.ignore_case:
            if in_str[pos:pos+len(self.value)].lower() == self.value_cmp:
                return self.value
        else:
            if in_str[pos:pos+len(self.value)] == self.value_cmp:
                return self.value


def esc_control_characters(regex):
    """
    Escape control characters in regular expressions.
    """
    unescapes = [('\a', r'\a'), ('\b', r'\b'), ('\f', r'\f'), ('\n', r'\n'),
                 ('\r', r'\r'), ('\t', r'\t'), ('\v', r'\v')]
    for val, text in unescapes:
        regex = regex.replace(val, text)
    return regex


class RegExRecognizer(Recognizer):
    def __init__(self, regex, re_flags=re.MULTILINE, ignore_case=False):
        super(RegExRecognizer, self).__init__(regex)
        self._regex = regex
        self.ignore_case = ignore_case
        if ignore_case:
            re_flags |= re.IGNORECASE
        self.re_flags = re_flags
        try:
            self.regex = re.compile(self._regex, re_flags)
        except re.error as ex:
            regex = esc_control_characters(self._regex)
            message = 'Regex compile error in /{}/ (report: "{}")'
            raise GrammarError(message.format(regex, str(ex)))

    def __call__(self, in_str, pos):
        m = self.regex.match(in_str, pos)
        if m:
            matched = m.group()
            return matched


def EMPTY_recognizer(input, pos):
    pass


def EOF_recognizer(input, pos):
    pass


def STOP_recognizer(input, pos):
    pass


# These two terminals are special terminals used internally.
AUGSYMBOL = NonTerminal("S'")
STOP = Terminal("STOP", STOP_recognizer)

# These two terminals are special terminals used in the grammars.
# EMPTY will match nothing and always succeed.
# EOF will match only at the end of the input string.
EMPTY = Terminal("EMPTY", EMPTY_recognizer)
EMPTY.grammar_action = pass_none
EOF = Terminal("EOF", EOF_recognizer)
EOF.grammar_action = pass_none


class Production(object):
    """Represent production from the grammar.

    Attributes:
    symbol (GrammarSymbol):
    rhs (ProductionRHS):
    assignments(dict): Assignment instances keyed by name.
    assoc (int): Associativity. Used for ambiguity (shift/reduce) resolution.
    prior (int): Priority. Used for ambiguity (shift/reduce) resolution.
    dynamic (bool): Is dynamic disambiguation used for this production.
    nops (bool): Disable prefer_shifts strategy for this production.
        Only makes sense for GLR parser.
    nopse (bool): Disable prefer_shifts_over_empty strategy for this
        production. Only makes sense for GLR parser.
    prod_id (int): Ordinal number of the production.
    prod_symbol_id (int): A zero-based ordinal of alternative choice for this
        production grammar symbol.
    """

    def __init__(self, symbol, rhs, assignments=None, assoc=ASSOC_NONE,
                 prior=DEFAULT_PRIORITY, dynamic=False, nops=False,
                 nopse=False):
        """
        Args:
        symbol (GrammarSymbol): A grammar symbol on the LHS of the production.
        rhs (list of GrammarSymbols):
        """
        self.symbol = symbol
        self.rhs = rhs if rhs else ProductionRHS()
        self.assignments = None
        if assignments:
            self.assignments = {}
            for assignment in assignments:
                if assignment.name:
                    self.assignments[assignment.name] = assignment
        self.assoc = assoc
        self.prior = prior
        self.dynamic = dynamic
        self.nops = nops
        self.nopse = nopse

    def __str__(self):
        if hasattr(self, 'prod_id'):
            return (s_header("%d:") + " %s " + s_emph("=") +
                    " %s") % (self.prod_id, self.symbol, self.rhs)
        else:
            return ("%s " + s_emph("=") + " %s") % (self.symbol, self.rhs)

    def __repr__(self):
        return 'Production({})'.format(str(self.symbol))


class ProductionRHS(list):
    def __getitem__(self, idx):
        try:
            while True:
                symbol = super(ProductionRHS, self).__getitem__(idx)
                if symbol is not EMPTY:
                    break
                idx += 1
            return symbol
        except IndexError:
            return None

    def __len__(self):
        return super(ProductionRHS, self).__len__() - self.count(EMPTY)

    def __str__(self):
        return " ".join([str(x) for x in self])

    def __repr__(self):
        return "<ProductionRHS([{}])>".format(
            ", ".join([str(x) for x in self]))


class Assignment(object):
    """
    General assignment (`=` or `?=`, a.k.a. `named matches`) in productions.
    Used also for references as LHS and assignment operator are optional.
    """
    def __init__(self, name, op, symbol, orig_symbol, multiplicity=MULT_ONE,
                 index=None):
        """
        Attributes:
            name(str): The name on the LHS of assignment.
            op(str): Either a `=` or `?=`.
            symbol(GrammarSymbol): A grammar symbol on the RHS.
            orig_symbol(GrammarSymbol): A de-sugarred grammar symbol on the
                RHS, i.e. referenced symbol without regex operators.
            multiplicty(str): Multiplicity of the RHS reference (used for regex
                operators ?, *, +). See MULT_* constants above. By default
                multiplicity is MULT_ONE.
            index(int): Index in the production RHS
        """
        self.name = name
        self.op = op
        self.symbol = symbol
        self.orig_symbol = orig_symbol
        self.multiplicity = multiplicity
        self.index = index


class PGAttribute(object):
    """
    PGAttribute definition created by named matches.

    Attributes:
        name(str): The name of the attribute.
        multiplicity(str): Multiplicity of the attribute. See MULT_* constants.
        type_name(str): The type name of the attribute value(s). It is also the
            name of the referring grammar rule.
    """
    def __init__(self, name, multiplicity, type_name):
        self.name = name
        self.multiplicity = multiplicity
        self.type_name = type_name


class PGFile(object):
    """Objects of this class represent parglare grammar files.

    Grammar files can be imported using `import` keyword. Rules referenced from
    the imported grammar must be fully qualified by the grammar module name. By
    default the name of the target .pg file is the name of the module. `as`
    keyword can be used to override the default.

    Example:
    ```
    import `some/path/mygrammar.pg` as target
    ```

    Rules from file `mygrammar.pg` will be available under `target` namespace:

    ```
    MyRule: target.someRule+;
    ```

    Actions are by default loaded from the file named `<grammar>_actions.py`
    where `grammar` is basename of grammar file. Recognizers are loaded from
    `<grammar>_recognizers.py`. Actions and recognizers given this way are both
    optional. Furthermore, both actions and recognizers can be overriden by
    supplying actions and/or recognizers dict during grammar/parser
    instantiation.

    Attributes:

    productions (list of Production): Local productions defined in this file.
    imports (dict): Mapping imported module/file local name to PGFile object.
    file_path (str): A full canonic path to the .pg file.
    root_file (PGFile): A root/grammar file.
    recognizers (dict of callables): A dict of Python callables used as a
        terminal recognizers.

    """
    def __init__(self, productions, imports=None, file_path=None,
                 root_file=None, recognizers=None):
        self.productions = productions
        self.imports = imports
        self.file_path = path.realpath(file_path)
        self.root_file = self if root_file is None else root_file

        # TODO:
        # Load recognizers from <grammar_name>_recognizers.py
        # Override with provided recognizers

        self.collect_symbols()

    def collect_symbols(self):
        """Collect grammar symbols and str match terminals defined in this
        file."""
        self.symbols_by_name = {}
        # mapping recognizer value -> Terminal
        self.recog_to_terminals = {}
        for p in self.productions:
            new_symbol = p.symbol
            if isinstance(new_symbol, Terminal):
                prev_symbol = self.symbols_by_name.get(new_symbol.name)
                if prev_symbol:
                    if isinstance(prev_symbol, Terminal):
                        # Multiple definitions of Terminals. Consider it a
                        # non-terminal with alternative terminals.
                        new_symbol = NonTerminal(new_symbol.name)
                        for k, v in self.recog_to_terminals.items():
                            if v.name == new_symbol.name:
                                del self.recog_to_terminals[k]
                                break
                    else:
                        new_symbol = prev_symbol

                else:
                    if p.rhs:
                        rec_name = p.rhs[0].name
                        if rec_name not in SPECIAL_SYMBOL_NAMES:
                            assert new_symbol.name \
                                not in self.recog_to_terminals
                            self.recog_to_terminals[rec_name] = new_symbol

            self._resolve_action(p.symbol, new_symbol)
            self.symbols_by_name[new_symbol.name] = new_symbol


class Grammar(PGFile):
    """
    Grammar is a collection of production rules, nonterminals and terminals.
    First production is reserved for the augmented production (S' -> S).

    Attributes:
    start_symbol (GrammarSymbol or str): start/root symbol of the grammar or
        its name.
    nonterminals (set of NonTerminal):
    terminals(set of Terminal):

    """

    def __init__(self, productions=None, imports=None, file_path=None,
                 recognizers=None, start_symbol=None,
                 _no_check_recognizers=False, re_flags=re.MULTILINE,
                 ignore_case=False, debug=False, debug_parse=False,
                 debug_colors=False):
        """
        Grammar constructor is not meant to be called directly by the user.
        See `from_str` and `from_file` static methods instead.

        Arguments:
        see Grammar attributes.
        _no_check_recognizers (bool, internal): Used by pglr tool to circumvent
             errors for empty recognizers that will be provided in user code.
        """
        super(Grammar, self).__init__(productions=productions, imports=imports,
                                      file_path=file_path, root_file=self,
                                      recognizers=recognizers)

        self._no_check_recognizers = _no_check_recognizers

        # Determine start symbol. If name is provided search for it. If name is
        # not given use the first production LHS symbol as the start symbol.
        if start_symbol:
            if isinstance(start_symbol, str):
                for p in self.productions:
                    if p.symbol.name == start_symbol:
                        self.start_symbol = p.symbol
            else:
                self.start_symbol = start_symbol
        else:
            # By default, first production symbol is the start symbol.
            self.start_symbol = self.productions[0].symbol

        self._init_grammar()

    def _init_grammar(self):
        """
        Extracts all grammar symbol (nonterminal and terminal) from the
        grammar, resolves and check references in productions, unify all
        grammar symbol objects and enumerate production.
        """
        self.nonterminals = set()
        self.terminals = set()

        # Reserve 0 production. It is used for augmented prod. in LR
        # automata calculation.
        self.productions.insert(
            0,
            Production(AUGSYMBOL, ProductionRHS([self.start_symbol, STOP])))

        self._collect_grammar_symbols()

        # Add special terminals
        self.symbols_by_name['EMPTY'] = EMPTY
        self.symbols_by_name['EOF'] = EOF
        self.symbols_by_name['STOP'] = STOP
        self.terminals.update([EMPTY, EOF, STOP])

        # Connect recognizers, override grammar provided
        if not self._no_check_recognizers:
            self._check_connect_recognizers()

        self._resolve_references()

        # At the end remove terminal productions as those are not the real
        # productions, but just a symbolic names for terminals.
        non_term_productions = [p for p in self.productions
                                if isinstance(p.symbol, NonTerminal)
                                or p.symbol.name == 'LAYOUT']
        if len(non_term_productions) > 1:
            # We have non-terminals
            self.productions[:] = non_term_productions

        self._enumerate_productions()
        self._fix_keyword_terminals()

    def _collect_grammar_symbols(self):
        """Collect all terminal and non-terminal symbols from LHS of productions.
        Create symbols_by_name dict (names->symbol) and recog_to_terminals dict
        (str value -> terminal)

        """
        self.symbols_by_name = {}
        # mapping recognizer value -> Terminal
        self.recog_to_terminals = {}
        for p in self.productions:
            new_symbol = p.symbol
            if isinstance(new_symbol, Terminal):
                prev_symbol = self.symbols_by_name.get(new_symbol.name)
                if prev_symbol:
                    if isinstance(prev_symbol, Terminal):
                        # Multiple definitions of Terminals. Consider it a
                        # non-terminal with alternative terminals.
                        new_symbol = NonTerminal(new_symbol.name)
                        for k, v in self.recog_to_terminals.items():
                            if v.name == new_symbol.name:
                                del self.recog_to_terminals[k]
                                break
                    else:
                        new_symbol = prev_symbol

                else:
                    if p.rhs:
                        rec_name = p.rhs[0].name
                        if rec_name not in SPECIAL_SYMBOL_NAMES:
                            assert new_symbol.name \
                                not in self.recog_to_terminals
                            self.recog_to_terminals[rec_name] = new_symbol

            self._resolve_action(p.symbol, new_symbol)
            self.symbols_by_name[new_symbol.name] = new_symbol

        self.terminals = set([x for x in self.symbols_by_name.values()
                              if isinstance(x, Terminal)])
        self.nonterminals = set([x for x in self.symbols_by_name.values()
                                 if isinstance(x, NonTerminal)])

    def _check_connect_recognizers(self):
        for term in self.terminals:
            if not self.recognizers and term.recognizer is None:
                raise GrammarError(
                    'Terminal "{}" has no recognizer defined '
                    'and no recognizers are given during grammar '
                    'construction.'.format(term.name))
            if term.name not in self.recognizers:
                if term.recognizer is None:
                    raise GrammarError(
                        'Terminal "{}" has no recognizer defined.'
                        .format(term.name))
            else:
                term.recognizer = self.recognizers[term.name]

    def _resolve_action(self, old_symbol, new_symbol):
        """
        Checks and resolves common semantic actions given in the grammar.
        """
        # Get/check grammar actions for rules/symbols.
        if new_symbol.action_name:
            if new_symbol.action_name != old_symbol.action_name:
                raise GrammarError(
                    'Multiple different grammar actions for rule "{}".'
                    .format(new_symbol.name))

            # Try to find action in built-in actions module
            # If action is not given we suppose that it is a user defined
            # action that will be provided during parser instantiation
            # using `actions` param.
            import parglare.actions as actmodule
            if hasattr(actmodule, new_symbol.action_name):
                new_symbol.action = \
                    new_symbol.grammar_action = getattr(actmodule,
                                                        new_symbol.action_name)

    def _resolve_references(self):
        """
        Resolve all references and unify objects so that we have single
        instances of each terminal and non-terminal in the grammar.
        Create Terminal for user supplied Recognizer.
        """

        rec_to_term = {}

        for idx, p in enumerate(self.productions):

            if p.symbol.name in self.symbols_by_name:
                p.symbol = self.symbols_by_name[p.symbol.name]

            if type(p.symbol) is NonTerminal:
                p.symbol.productions.append(p)

            for idx_ref, ref in enumerate(p.rhs):
                ref_sym = None
                if ref.name in self.symbols_by_name:
                    ref_sym = self.symbols_by_name[ref.name]
                elif isinstance(p.symbol, NonTerminal) \
                        and ref.name in self.recog_to_terminals:
                    # If terminal is registered by str recognizer and is
                    # referenced in a RHS of some other production report
                    # error.
                    term_by_rec = self.recog_to_terminals[ref.name]
                    raise GrammarError(
                        "Terminal '{}' used in production '{}' "
                        "already exists by the name '{}'.".format(
                            text(ref.name), text(p.symbol),
                            text(term_by_rec)))
                else:
                    if not isinstance(ref, Terminal):
                        raise GrammarError(
                            "Unknown symbol '{}' used in production '{}'."
                            .format(text(ref.name), text(p.symbol)))

                    if ref.name in rec_to_term:
                        ref_sym = rec_to_term[ref.name]
                    else:
                        ref_sym = ref
                        rec_to_term[ref.name] = ref
                        self.terminals.add(ref_sym)

                if not ref_sym:
                    raise GrammarError(
                        "Unknown symbol '{}' referenced from production '{}'.".
                        format(ref.name, text(p)))

                p.rhs[idx_ref] = ref_sym

    def _enumerate_productions(self):
        """
        Enumerates all productions (prod_id) and production per symbol
        (prod_symbol_id).
        """
        idx_per_symbol = {}
        for idx, s in enumerate(self.productions):
            s.prod_id = idx
            s.prod_symbol_id = idx_per_symbol.get(s.symbol, 0)
            idx_per_symbol[s.symbol] = idx_per_symbol.get(s.symbol, 0) + 1

    def _fix_keyword_terminals(self):
        """
        If KEYWORD terminal with regex match is given fix all matching string
        recognizers to match on a word boundary.
        """
        keyword_term = self.get_terminal('KEYWORD')
        if keyword_term is None:
            return

        # KEYWORD rule must have a regex recognizer
        keyword_rec = keyword_term.recognizer
        if not isinstance(keyword_rec, RegExRecognizer):
            raise GrammarError(
                'KEYWORD rule must have a regex recognizer defined.')

        # Change each string recognizer corresponding to the KEYWORD
        # regex by the regex recognizer that match on word boundaries.
        for term in self.terminals:
            if isinstance(term.recognizer, StringRecognizer):
                match = keyword_rec(term.recognizer.value, 0)
                if match == term.recognizer.value:
                    term.recognizer = RegExRecognizer(
                        r'\b{}\b'.format(match),
                        ignore_case=term.recognizer.ignore_case)
                    term.keyword = True

    def get_terminal(self, name):
        "Returns terminal with the given name."
        for t in self.terminals:
            if t.name == name:
                return t

    def get_nonterminal(self, name):
        "Returns non-terminal with the given name."
        for n in self.nonterminals:
            if n.name == name:
                return n

    def get_symbol(self, name):
        "Returns grammar symbol with the given name."
        s = self.get_terminal(name)
        if not s:
            s = self.get_nonterminal(name)
        return s

    def __iter__(self):
        return (s for s in itertools.chain(self.nonterminals, self.terminals)
                if s not in [AUGSYMBOL, STOP])

    def get_production_id(self, name):
        "Returns first production id for the given symbol name"
        for p in self.productions:
            if p.symbol.name == name:
                return p.prod_id

    @staticmethod
    def from_struct(productions, start_symbol, recognizers=None):
        """Used internally to bootstrap grammar file parser."""
        return Grammar(productions=create_productions(productions),
                       start_symbol=start_symbol, recognizers=recognizers)

    @staticmethod
    def from_string(grammar_str, recognizers=None, ignore_case=False,
                    re_flags=re.MULTILINE, debug=False, debug_parse=False,
                    debug_colors=False, _no_check_recognizers=False):
        from .parser import Context
        context = Context()
        context.re_flags = re_flags
        context.ignore_case = ignore_case
        context.classes = {}
        imports, productions = \
            get_grammar_parser(debug_parse, debug_colors).parse(
                grammar_str, context=context)
        if imports:
            raise GrammarError('Imports can be used only in file grammars.')
        g = Grammar(productions=productions,
                    recognizers=recognizers,
                    _no_check_recognizers=_no_check_recognizers)
        g.classes = context.classes
        termui.colors = debug_colors
        if debug:
            g.print_debug()

        return g

    @staticmethod
    def from_file(file_name, recognizers=None, ignore_case=False,
                  re_flags=re.MULTILINE, debug=False, debug_parse=False,
                  debug_colors=False, _no_check_recognizers=False):
        from .parser import Context
        context = Context()
        context.re_flags = re_flags
        context.ignore_case = ignore_case
        context.classes = {}
        imports, productions = \
            get_grammar_parser(debug_parse, debug_colors).parse_file(
                file_name, context=context)

        g = Grammar(productions, imports, file_name, recognizers=recognizers,
                    _no_check_recognizers=_no_check_recognizers)
        g.classes = context.classes
        termui.colors = debug_colors
        if debug:
            g.print_debug()

        return g

    def print_debug(self):
        a_print("*** GRAMMAR ***", new_line=True)
        h_print("Terminals:")
        prints(" ".join([text(t) for t in self.terminals]))
        h_print("NonTerminals:")
        prints(" ".join([text(n) for n in self.nonterminals]))

        h_print("Productions:")
        for p in self.productions:
            prints(text(p))


class PGFiles(dict):
    """
    A collection of .pg files (PGFile instances) keyed by absolute file path.
    In charge of lazy loading/parsing of .pg files.
    """


class PGFileImport(object):
    """
    Represents import of grammar file.

    Attributes:
    pg_files (PGFiles): A reference to a registry of loaded PG files.
    file_path (str): A canonical full path of the imported .pg file.
    pg_file (PGFile instance or None):

    """
    def __init__(self, pg_files, file_path):
        self.pg_files = pg_files
        self.file_path = file_path
        self._pg_file = None

    @property
    def pg_file(self):
        """Returns an instance of PGFile this import imports.
        If file is not imported yet parses target file and caches it
        for later use.
        """
        if self._pg_file is None:
            if self.file_path not in self.pg_files:
                # First time import.
                pg_file = get_grammar_parser().parse(self.file_path)
                self.pg_files[self.file_path] = pg_file
                self._pg_file = pg_file
            else:
                # File is already imported somewhere else
                self._pg_file = self.pg_files[self.file_path]
        return self._pg_file


def create_productions(productions):
    """Creates Production instances from the list of productions given in
    the form:
    [LHS, RHS, optional ASSOC, optional PRIOR].
    Where LHS is grammar symbol and RHS is a list or tuple of grammar
    symbols from the right-hand side of the production.
    """
    gp = []
    for p in productions:
        assoc = ASSOC_NONE
        prior = DEFAULT_PRIORITY
        symbol = p[0]
        if not isinstance(symbol, NonTerminal):
            raise GrammarError("Invalid production symbol '{}' "
                               "for production '{}'".format(symbol,
                                                            text(p)))
        rhs = ProductionRHS(p[1])
        if len(p) > 2:
            assoc = p[2]
        if len(p) > 3:
            prior = p[3]

        # Convert strings to Terminals with string recognizers
        for idx, t in enumerate(rhs):
            if isinstance(t, text):
                rhs[idx] = Terminal(t)

        gp.append(Production(symbol, rhs, assoc=assoc, prior=prior))

    return gp


def check_name(context, name):
    """
    Used in actions to check for reserved names usage.
    """

    if name in RESERVED_SYMBOL_NAMES:
            from parglare.parser import pos_to_line_col
            raise GrammarError('Rule name "{}" at {} is reserved.'.format(
                name, pos_to_line_col(context.input_str,
                                      context.start_position)))


# Grammar for grammars

(PGFILE,
 IMPORTS,
 IMPORT,
 RULES,
 RULE,
 PRODUCTION_RULE,
 PRODUCTION_RULE_RHS,
 PRODUCTION,
 TERMINAL_RULE,
 PROD_DIS_RULE,
 PROD_DIS_RULES,
 TERM_DIS_RULE,
 TERM_DIS_RULES,

 ASSIGNMENT,
 ASSIGNMENTS,
 PLAIN_ASSIGNMENT,
 BOOL_ASSIGNMENT,

 REPEATABLE_GSYMBOL,
 REPEATABLE_GSYMBOLS,
 OPT_REP_OPERATOR,
 REP_OPERATOR_ZERO,
 REP_OPERATOR_ONE,
 REP_OPERATOR_OPTIONAL,
 OPT_REP_MODIFIERS_EXP,
 OPT_REP_MODIFIERS,
 OPT_REP_MODIFIER,

 GSYMBOL,
 RECOGNIZER,
 LAYOUT,
 LAYOUT_ITEM,
 COMMENT,
 CORNC,
 CORNCS) = [NonTerminal(name) for name in [
     'PGFile',
     'Imports',
     'Import',
     'Rules',
     'Rule',
     'ProductionRule',
     'ProductionRuleRHS',
     'Production',
     'TerminalRule',
     'ProductionDisambiguationRule',
     'ProductionDisambiguationRules',
     'TerminalDisambiguationRule',
     'TerminalDisambiguationRules',

     'Assignment',
     'Assignments',
     'PlainAssignment',
     'BoolAssignment',

     'RepeatableGrammarSymbol',
     'RepeatableGrammarSymbols',
     'OptRepeatOperator',
     'RepeatOperatorZero',
     'RepeatOperatorOne',
     'RepeatOperatorOptional',
     'OptionalRepeatModifiersExpression',
     'OptionalRepeatModifiers',
     'OptionalRepeatModifier',

     'GrammarSymbol',
     'Recognizer',
     'LAYOUT',
     'LAYOUT_ITEM',
     'Comment',
     'CORNC',
     'CORNCS']]

(NAME,
 STR_TERM,
 REGEX_TERM,
 PRIOR,
 ACTION,
 WS,
 COMMENTLINE,
 NOTCOMMENT) = [Terminal(name, RegExRecognizer(regex)) for name, regex in
                [
                    ('Name', r'[a-zA-Z0-9_]+'),
                    ('StrTerm', r'''(?s)('[^'\\]*(?:\\.[^'\\]*)*')|'''
                     r'''("[^"\\]*(?:\\.[^"\\]*)*")'''),
                    ('RegExTerm', r'''\/((\\/)|[^/])*\/'''),
                    ('Prior', r'\d+'),
                    ('Action', r'@[a-zA-Z0-9_]+'),
                    ('WS', r'\s+'),
                    ('CommentLine', r'\/\/.*'),
                    ('NotComment', r'((\*[^\/])|[^\s*\/]|\/[^\*])+'),
                ]]

pg_productions = [
    [PGFILE, [RULES, EOF]],
    [PGFILE, [IMPORTS, RULES, EOF]],
    [IMPORTS, [IMPORTS, IMPORT]],
    [IMPORTS, [IMPORT]],
    [IMPORT, ['import', STR_TERM, ';']]
    [IMPORT, ['import', STR_TERM, 'as', NAME, ';']]
    [RULES, [RULES, RULE]],
    [RULES, [RULE]],
    [RULE, [PRODUCTION_RULE]],
    [RULE, [ACTION, PRODUCTION_RULE]],
    [RULE, [TERMINAL_RULE]],
    [RULE, [ACTION, TERMINAL_RULE]],

    [PRODUCTION_RULE, [NAME, ':', PRODUCTION_RULE_RHS, ';']],
    [PRODUCTION_RULE_RHS, [PRODUCTION_RULE_RHS, '|', PRODUCTION],
     ASSOC_LEFT, 5],
    [PRODUCTION_RULE_RHS, [PRODUCTION], ASSOC_LEFT, 5],
    [PRODUCTION, [ASSIGNMENTS]],
    [PRODUCTION, [ASSIGNMENTS, '{', PROD_DIS_RULES, '}']],

    [TERMINAL_RULE, [NAME, ':', RECOGNIZER, ';'], ASSOC_LEFT, 15],
    [TERMINAL_RULE, [NAME, ':', ';'], ASSOC_LEFT, 15],
    [TERMINAL_RULE, [NAME, ':', RECOGNIZER, '{', TERM_DIS_RULES, '}', ';'],
     ASSOC_LEFT, 15],
    [TERMINAL_RULE, [NAME, ':', '{', TERM_DIS_RULES, '}', ';'],
     ASSOC_LEFT, 15],

    [PROD_DIS_RULE, ['left']],
    [PROD_DIS_RULE, ['reduce']],
    [PROD_DIS_RULE, ['right']],
    [PROD_DIS_RULE, ['shift']],
    [PROD_DIS_RULE, ['dynamic']],
    [PROD_DIS_RULE, ['nops']],   # no prefer shifts
    [PROD_DIS_RULE, ['nopse']],  # no prefer shifts over empty
    [PROD_DIS_RULE, [PRIOR]],
    [PROD_DIS_RULES, [PROD_DIS_RULES, ',', PROD_DIS_RULE], ASSOC_LEFT],
    [PROD_DIS_RULES, [PROD_DIS_RULE]],

    [TERM_DIS_RULE, ['prefer']],
    [TERM_DIS_RULE, ['finish']],
    [TERM_DIS_RULE, ['nofinish']],
    [TERM_DIS_RULE, ['dynamic']],
    [TERM_DIS_RULE, [PRIOR]],
    [TERM_DIS_RULES, [TERM_DIS_RULES, ',', TERM_DIS_RULE]],
    [TERM_DIS_RULES, [TERM_DIS_RULE]],

    # Assignments
    [ASSIGNMENT, [PLAIN_ASSIGNMENT]],
    [ASSIGNMENT, [BOOL_ASSIGNMENT]],
    [ASSIGNMENT, [REPEATABLE_GSYMBOL]],
    [ASSIGNMENTS, [ASSIGNMENTS, ASSIGNMENT]],
    [ASSIGNMENTS, [ASSIGNMENT]],
    [PLAIN_ASSIGNMENT, [NAME, '=', REPEATABLE_GSYMBOL]],
    [BOOL_ASSIGNMENT, [NAME, '?=', REPEATABLE_GSYMBOL]],

    # Regex-like repeat operators
    [REPEATABLE_GSYMBOL, [GSYMBOL, OPT_REP_OPERATOR]],
    [OPT_REP_OPERATOR, [REP_OPERATOR_ZERO]],
    [OPT_REP_OPERATOR, [REP_OPERATOR_ONE]],
    [OPT_REP_OPERATOR, [REP_OPERATOR_OPTIONAL]],
    [OPT_REP_OPERATOR, [EMPTY]],
    [REP_OPERATOR_ZERO, ['*', OPT_REP_MODIFIERS_EXP]],
    [REP_OPERATOR_ONE, ['+', OPT_REP_MODIFIERS_EXP]],
    [REP_OPERATOR_OPTIONAL, ['?', OPT_REP_MODIFIERS_EXP]],
    [OPT_REP_MODIFIERS_EXP, ['[', OPT_REP_MODIFIERS, ']']],
    [OPT_REP_MODIFIERS_EXP, [EMPTY]],
    [OPT_REP_MODIFIERS, [OPT_REP_MODIFIERS, ',', OPT_REP_MODIFIER]],
    [OPT_REP_MODIFIERS, [OPT_REP_MODIFIER]],
    [OPT_REP_MODIFIER, [NAME]],

    [GSYMBOL, [NAME]],
    [GSYMBOL, [RECOGNIZER]],
    [RECOGNIZER, [STR_TERM]],
    [RECOGNIZER, [REGEX_TERM]],

    # Support for comments,
    [LAYOUT, [LAYOUT_ITEM]],
    [LAYOUT, [LAYOUT, LAYOUT_ITEM]],
    [LAYOUT_ITEM, [WS]],
    [LAYOUT_ITEM, [COMMENT]],
    [LAYOUT_ITEM, [EMPTY]],
    [COMMENT, ['/*', CORNCS, '*/']],
    [COMMENT, [COMMENTLINE]],
    [CORNCS, [CORNC]],
    [CORNCS, [CORNCS, CORNC]],
    [CORNCS, [EMPTY]],
    [CORNC, [COMMENT]],
    [CORNC, [NOTCOMMENT]],
    [CORNC, [WS]]
]


grammar_parser = None


def get_grammar_parser(debug, debug_colors):
    global grammar_parser
    if not grammar_parser:
        from parglare import Parser
        grammar_parser = Parser(Grammar.from_struct(pg_productions, PGFILE),
                                actions=pg_actions, debug=debug,
                                debug_colors=debug_colors)
    EMPTY.action = pass_none
    EOF.action = pass_none
    return grammar_parser


def act_pgfile(context, nodes):
    if len(nodes) > 1:
        imports, productions = nodes
    else:
        imports, productions = [], nodes[0]
    if hasattr(context, 'new_productions'):
        for _, (nt, prods) in context.new_productions.items():
            productions.extend(prods)
    return [imports, productions]


def act_import(context, nodes):
    path = nodes[1]
    if len(nodes) > 3:
        module_name = nodes[3]
    return PGFileImport(path, module_name)


def act_rules(_, nodes):
    e1, e2 = nodes
    e1.extend(e2)
    return e1


def act_rule_with_action(_, nodes):
    action, productions = nodes

    # Strip @ char
    action = action[1:]

    productions[0].symbol.action_name = action
    return productions


def act_production_rule(context, nodes):
    name, _, rhs_prods, __ = nodes

    check_name(context, name)

    symbol = NonTerminal(name)

    # Collect all productions for this rule
    prods = []
    attrs = {}
    for prod in rhs_prods:
        assignments, disrules = prod
        # Here we know the indexes of assignments
        for idx, a in enumerate(assignments):
            if a.name:
                a.index = idx
        gsymbols = (a.symbol for a in assignments)
        assoc = disrules.get('assoc', ASSOC_NONE)
        prior = disrules.get('priority', DEFAULT_PRIORITY)
        dynamic = disrules.get('dynamic', False)
        nops = disrules.get('nops', False)
        nopse = disrules.get('nopse', False)
        prods.append(Production(symbol,
                                ProductionRHS(gsymbols),
                                assignments=assignments,
                                assoc=assoc,
                                prior=prior,
                                dynamic=dynamic,
                                nops=nops,
                                nopse=nopse))

        for a in assignments:
            if a.name:
                attrs[a.name] = PGAttribute(a.name, a.multiplicity,
                                            a.orig_symbol.name)
            # TODO: check/handle multiple assignments to the same attribute
            #       If a single production have multiple assignment of the
            #       same attribute, multiplicity must be set to many.

    # If named matches are used create Python class that will be used
    # for object instantiation.
    if attrs:
        class ParglareMetaClass(type):

            def __repr__(cls):
                return '<parglare:{} class at {}>'.format(name, id(cls))

        @add_metaclass(ParglareMetaClass)
        class ParglareClass(object):
            """Dynamicaly created class. Each parglare rule that uses named
            matches by default uses this action that will create Python object
            of this class.

            Attributes:
                _pg_attrs(dict): A dict of meta-attributes keyed by name.
                    Used by common rules.
                _pg_position(int): A position in the input string where
                    this class is defined.
                _pg_position_end(int): A position in the input string where
                    this class ends.

            """

            _pg_attrs = attrs

            def __init__(self, **attrs):
                for attr_name, attr_value in attrs.items():
                    setattr(self, attr_name, attr_value)

            def __repr__(self):
                if hasattr(self, 'name'):
                    return "<{}:{}>".format(name, self.name)
                else:
                    return "<parglare:{} instance at {}>"\
                        .format(name, hex(id(self)))

        ParglareClass.__name__ = str(name)
        if name in context.classes:
            # If rule has multiple definition merge attributes.
            context.classes[name]._pg_attrs.update(attrs)
        else:
            context.classes[name] = ParglareClass

        symbol.action_name = 'obj'

    return prods


def act_production(_, nodes):
    assignments = nodes[0]
    disrules = {}
    if len(nodes) > 1:
        rules = nodes[2]
        for rule in rules:
            if rule in ['left', 'reduce']:
                disrules['assoc'] = ASSOC_LEFT
            elif rule in ['right', 'shift']:
                disrules['assoc'] = ASSOC_RIGHT
            elif rule == 'dynamic':
                disrules['dynamic'] = True
            elif rule == 'nops':
                disrules['nops'] = True
            elif rule == 'nopse':
                disrules['nopse'] = True
            elif type(rule) is int:
                disrules['priority'] = rule

    return (assignments, disrules)


def _set_term_props(term, props):
    for t in props:
        if type(t) is int:
            term.prior = t
        elif t == 'finish':
            term.finish = True
        elif t == 'nofinish':
            term.finish = False
        elif t == 'prefer':
            term.prefer = True
        elif t == 'dynamic':
            term.dynamic = True
        else:
            print(t)
            assert False


def act_term_rule(context, nodes):

    name = nodes[0]
    rhs_term = nodes[2]

    check_name(context, name)

    term = Terminal(name, rhs_term.recognizer)
    if len(nodes) > 4:
        _set_term_props(term, nodes[4])
    return [Production(term, ProductionRHS([rhs_term]))]


def act_term_rule_empty_body(context, nodes):
    name = nodes[0]

    check_name(context, name)

    term = Terminal(name)
    term.recognizer = None
    if len(nodes) > 3:
        _set_term_props(term, nodes[3])
    return [Production(term, ProductionRHS([]))]


def make_repetition(context, gsymbol, sep_ref, suffix,
                    action, prod_callable):
    new_gsymbol_name = gsymbol.name + suffix
    if sep_ref:
        new_gsymbol_name += '_' + sep_ref.name

    if not hasattr(context, 'new_productions'):
        # symbol_name -> (NonTerminal, [productions])
        context.new_productions = {}

    if new_gsymbol_name in context.new_productions:
        return context.new_productions[new_gsymbol_name][0]

    new_nt = NonTerminal(new_gsymbol_name)
    if type(action) is text:
        new_nt.action_name = action
    else:
        new_nt.action = action
    new_productions = prod_callable(new_nt)
    context.new_productions[new_gsymbol_name] = (new_nt, new_productions)

    return new_nt


def make_one_or_more(context, gsymbol, sep_ref=None):
    def prod_callable(new_nt):
        new_productions = []
        if sep_ref:
            new_productions.append(
                Production(new_nt,
                           ProductionRHS([new_nt, sep_ref, gsymbol])))
        else:
            new_productions.append(
                Production(new_nt, ProductionRHS([new_nt, gsymbol])))

        new_productions.append(
            Production(new_nt, ProductionRHS([gsymbol])))

        return new_productions

    return make_repetition(context, gsymbol, sep_ref, '_1',
                           'collect' if sep_ref is None else 'collect_sep',
                           prod_callable)


def make_zero_or_more(context, gsymbol, sep_ref=None):
    def prod_callable(new_nt):
        new_productions = []
        one_or_more = make_one_or_more(context, gsymbol, sep_ref)
        new_productions.append(
            Production(new_nt, ProductionRHS([one_or_more]), nops=True))
        new_productions.append(
            Production(new_nt, ProductionRHS([EMPTY])))

        return new_productions

    def action(_, nodes):
        if nodes:
            return nodes[0]
        else:
            return []

    return make_repetition(
        context, gsymbol, sep_ref, '_0', action, prod_callable)


def make_optional(context, gsymbol, sep_ref=None):
    def prod_callable(new_nt):
        if sep_ref:
            from parglare import pos_to_line_col
            raise GrammarError(
                'Repetition modifier not allowed for '
                'optional (?) for symbol "{}" at {}.'
                .format(gsymbol.name,
                        pos_to_line_col(context.input_str,
                                        context.start_position)))
        # Optional
        new_productions = [Production(new_nt, ProductionRHS([gsymbol])),
                           Production(new_nt, ProductionRHS([EMPTY]))]

        return new_productions

    return make_repetition(
        context, gsymbol, sep_ref, '_opt', 'optional',
        prod_callable)


def act_repeatable_gsymbol(context, nodes):
    """Repetition operators (`*`, `+`, `?`) will create additional productions in
    the grammar with name generated from original symbol name and suffixes:
    - `_0` - for `*`
    - `_1` - for `+`
    - `_opt` - for `?`

    Zero or more produces `one or more` productions and additional productions
    of the form:

    ```
    somerule_0: somerule_1 | EMPTY;
    ```

    In addition if separator is used another suffix is added which is the name
    of the separator rule, for example:

    ```
    spam*[comma] --> spam_0_comma and spam_1_comma
    spam+[comma] --> spam_1_comma
    spam* --> spam_0 and spam_1
    spam? --> spam_opt
    ```

    """
    gsymbol, rep_op = nodes

    if not rep_op:
        return gsymbol, gsymbol, MULT_ONE

    if len(rep_op) > 1:
        rep_op, modifiers = rep_op
    else:
        rep_op = rep_op[0]
        modifiers = None

    sep_ref = None
    if modifiers:
        sep_ref = modifiers[1]
        sep_ref = Reference(sep_ref)

    if rep_op == '*':
        new_nt = make_zero_or_more(context, gsymbol, sep_ref)
        multiplicity = MULT_ZERO_OR_MORE
    elif rep_op == '+':
        new_nt = make_one_or_more(context, gsymbol, sep_ref)
        multiplicity = MULT_ONE_OR_MORE
    else:
        new_nt = make_optional(context, gsymbol, sep_ref)
        multiplicity = MULT_OPTIONAL

    return new_nt, gsymbol, multiplicity


def act_assignment(_, nodes):
    repeatable_gsymbol = nodes[0]
    if isinstance(repeatable_gsymbol[0], GrammarSymbol) or \
       isinstance(repeatable_gsymbol[0], Reference):
        symbol, orig_symbol, multiplicity = repeatable_gsymbol
        name, op = None, None
    else:
        # Named match
        name, op, repeatable_gsymbol = repeatable_gsymbol
        symbol, orig_symbol, multiplicity = repeatable_gsymbol

    return Assignment(name, op, symbol, orig_symbol, multiplicity)


def act_recognizer_str(context, nodes):
    value = nodes[0][1:-1]
    value = value.replace(r'\"', '"')\
                 .replace(r"\'", "'")\
                 .replace(r"\\", "\\")\
                 .replace(r"\n", "\n")\
                 .replace(r"\t", "\t")
    return Terminal(value, StringRecognizer(value,
                                            ignore_case=context.ignore_case))


def act_recognizer_regex(context, nodes):
    value = nodes[0][1:-1]
    return Terminal(value, RegExRecognizer(value,
                                           re_flags=context.re_flags,
                                           ignore_case=context.ignore_case))


pg_actions = {
    "PGFile": act_pgfile,
    "Rules": [act_rules, pass_single],
    "Rule": [pass_single,
             act_rule_with_action,
             pass_single,
             act_rule_with_action],

    'ProductionRule': act_production_rule,
    'ProductionRuleRHS': collect_sep,
    'Production': act_production,

    'TerminalRule': [act_term_rule,
                     act_term_rule_empty_body,
                     act_term_rule,
                     act_term_rule_empty_body],

    "ProductionDisambiguationRules": collect_sep,
    "TerminalDisambiguationRules": collect_sep,

    "Assignment": act_assignment,
    "Assignments": collect,

    'RepeatableGrammarSymbol': act_repeatable_gsymbol,
    'RepeatableGrammarSymbols': collect,

    'GrammarSymbol': [lambda _, nodes: Reference(nodes[0]),
                      pass_single],

    'Recognizer': [act_recognizer_str, act_recognizer_regex],

    # Terminals
    "Prior": lambda _, value: int(value),

}
