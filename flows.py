"""Cross-file taint analysis: follows user input from where it enters (request parameters, form fields, CLI args)
through assignments and function calls — across files — to dangerous sinks (SQL, shell, eval, templates, file paths,
outbound URLs, redirects, HTML).

How it works
1. Every Python / JavaScript / TypeScript / Java file is parsed with tree-sitter into functions (named, methods and
   anonymous callbacks such as Express handlers).
2. Inside a function, statements are walked in order and a set of tainted variable names is kept: a variable is
   tainted when it is assigned from a source, from another tainted variable, or from a call whose result is tainted.
   Wrapping a value in a sanitizer (int(), Number(), escape(), secure_filename()…) removes the taint.
3. Calls are resolved by name to functions in the same file, in files it imports, or — if the name is unique — in
   the whole project. When a tainted value is passed as an argument, the callee is analysed with that parameter
   tainted (up to MAX_DEPTH hops). A callee that returns tainted data taints the caller's variable.
4. A sink call whose relevant argument is tainted becomes a Finding with the full trace (file:line per hop).

It is deliberately approximate (name-based call resolution, no aliasing or field tracking), so it is a candidate
generator: `verify_flows` lets the LLM confirm or reject each trace, and Semgrep/Bandit still cover single-file rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAX_DEPTH = 6
MAX_FINDINGS = 300
MAX_FILE_BYTES = 400_000

LANG_BY_EXT = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
               ".ts": "typescript", ".tsx": "tsx", ".java": "java"}


GRAMMAR_HINT = ("tree-sitter grammars could not be loaded. They are downloaded once on first use (about 30 MB); "
                "on an offline machine run: python -c \"import tree_sitter_language_pack as t; "
                "t.download(['python', 'javascript', 'typescript', 'tsx', 'java'])\" while online.")


def ensure_grammars(langs: set[str]) -> None:
    """Raises RuntimeError(GRAMMAR_HINT) if a needed grammar cannot be loaded."""
    from tree_sitter_language_pack import get_parser
    for lang in sorted(langs):
        try:
            get_parser(lang)
        except Exception as e:
            raise RuntimeError(f"{GRAMMAR_HINT} ({lang}: {type(e).__name__})") from e


def available() -> bool:
    try:
        from tree_sitter_language_pack import get_parser  # noqa: F401
        return True
    except Exception:
        return False


# ------------------------------------------------------------------------------------------------ catalog
@dataclass(frozen=True)
class Sink:
    kind: str
    callee: re.Pattern
    args: tuple[int, ...] = (0,)          # which arguments matter (-1 = any)
    cwe: str = ""
    severity: str = "high"
    label: str = ""


def _s(kind, pattern, args=(0,), cwe="", severity="high", label=""):
    return Sink(kind, re.compile(pattern), args, cwe, severity, label or kind)


SOURCES = {
    "python": re.compile(r"\brequest\.(args|form|values|json|data|files|cookies|headers|get_json|GET|POST|body|FILES|"
                         r"COOKIES|META|query_params|path_params)\b|\binput\s*\(|\bsys\.argv\b"),
    "javascript": re.compile(r"\b(req|request)\.(body|query|params|cookies|headers|files?|url|originalUrl)\b|"
                             r"\bctx\.(request|query|params)\b|\blocation\.(search|hash|href)\b|"
                             r"\bdocument\.(URL|cookie|referrer|location)\b|\bwindow\.location\b|\bprocess\.argv\b|"
                             r"\b(event|e|msg)\.data\b|\bsearchParams\.get\s*\("),
    "java": re.compile(r"\b\w*[rR]equest\.(getParameter\w*|getHeader\w*|getCookies|getQueryString|getInputStream|"
                       r"getReader|getPathInfo|getRequestURI)\s*\("),
}
SOURCES["typescript"] = SOURCES["tsx"] = SOURCES["javascript"]

ROUTE_DECORATOR = re.compile(r"\.(route|get|post|put|patch|delete|api_route|websocket)\s*\(")
JAVA_PARAM_SOURCE = re.compile(r"@(RequestParam|PathVariable|RequestBody|RequestHeader|CookieValue|ModelAttribute|"
                               r"QueryParam|PathParam|FormParam|HeaderParam)\b")
NOT_INPUT_PARAMS = {"self", "cls", "request", "db", "session", "response", "res", "next", "background_tasks"}

SINKS = {
    "python": [
        _s("sql", r"(^|\.)(execute|executemany|executescript|raw|extra)$", cwe="CWE-89", label="SQL query"),
        _s("sql", r"(^|\.)text$", cwe="CWE-89", label="SQLAlchemy text()"),
        _s("command", r"^(os\.system|os\.popen|subprocess\.(call|run|Popen|check_output|check_call|getoutput)|"
                      r"commands\.getoutput)$", cwe="CWE-78", label="shell command"),
        _s("code", r"^(eval|exec|compile)$", cwe="CWE-95", label="eval/exec"),
        _s("template", r"(^|\.)(render_template_string|from_string)$|^(jinja2\.)?Template$", cwe="CWE-1336",
           label="template rendering"),
        _s("deserialization", r"^(pickle|cPickle|marshal|dill)\.loads?$|^yaml\.(load|unsafe_load)$|"
                              r"^jsonpickle\.decode$", cwe="CWE-502", label="deserialization"),
        _s("ssrf", r"^(requests|httpx)\.(get|post|put|patch|delete|head|request)$|(^|\.)urlopen$",
           cwe="CWE-918", label="outbound request"),
        _s("path", r"^open$|(^|\.)(send_file|send_from_directory)$|^os\.(remove|unlink|rmdir)$|^shutil\.\w+$",
           cwe="CWE-22", severity="medium", label="file path"),
        _s("redirect", r"(^|\.)redirect$", cwe="CWE-601", severity="medium", label="redirect"),
        _s("xss", r"(^|\.)(mark_safe|Markup)$", cwe="CWE-79", label="unescaped HTML"),
        _s("xxe", r"(^|\.)(etree\.fromstring|etree\.parse|parseString|XMLParser)$", cwe="CWE-611",
           severity="medium", label="XML parser"),
    ],
    "javascript": [
        _s("sql", r"(^|\.)(query|raw|execute|\$queryRawUnsafe|\$executeRawUnsafe)$", cwe="CWE-89", label="SQL query"),
        _s("nosql", r"(^|\.)(find|findOne|findOneAndUpdate|update|updateOne|updateMany|deleteOne|deleteMany|"
                    r"count|countDocuments|aggregate|where)$", cwe="CWE-943", label="NoSQL query"),
        _s("command", r"(^|\.)(exec|execSync|spawn|spawnSync|execFile|execFileSync)$", cwe="CWE-78",
           label="shell command"),
        _s("code", r"^(eval|Function|new Function)$|(^|\.)(runInContext|runInNewContext|runInThisContext)$|"
                   r"^vm\.\w+$", cwe="CWE-95", label="eval / vm"),
        _s("ssrf", r"^(fetch|axios|got|needle|superagent)$|^(axios|got|http|https|needle|superagent|request)\."
                   r"(get|post|put|patch|delete|request)$", cwe="CWE-918", label="outbound request"),
        _s("path", r"(^|\.)(readFile|readFileSync|writeFile|writeFileSync|createReadStream|createWriteStream|unlink|"
                   r"unlinkSync|sendFile|download|readdir|readdirSync)$", cwe="CWE-22", severity="medium",
           label="file path"),
        _s("redirect", r"(^|\.)redirect$", args=(-1,), cwe="CWE-601", severity="medium", label="redirect"),
        _s("xss", r"^document\.(write|writeln)$|(^|\.)(insertAdjacentHTML|bypassSecurityTrust\w+)$", args=(-1,),
           cwe="CWE-79", label="HTML sink"),
        _s("deserialization", r"(^|\.)(unserialize|deserialize)$|^yaml\.load$|(^|\.)parseXml$", cwe="CWE-502",
           label="deserialization"),
    ],
    "java": [
        _s("sql", r"(^|\.)(executeQuery|executeUpdate|execute|addBatch|prepareStatement|prepareCall|createQuery|"
                  r"createNativeQuery|createSQLQuery|queryForList|queryForObject|queryForMap|queryForRowSet)$",
           cwe="CWE-89", label="SQL query"),
        _s("command", r"(^|\.)exec$|^new ProcessBuilder$", args=(-1,), cwe="CWE-78", label="shell command"),
        _s("path", r"^new (File|FileInputStream|FileOutputStream|FileReader|FileWriter)$|^Paths\.get$|"
                   r"^Files\.\w+$", args=(-1,), cwe="CWE-22", severity="medium", label="file path"),
        _s("ssrf", r"^new URL$|(^|\.)(getForObject|getForEntity|postForObject|exchange|uri)$", cwe="CWE-918",
           label="outbound request"),
        _s("redirect", r"(^|\.)sendRedirect$", cwe="CWE-601", severity="medium", label="redirect"),
        _s("deserialization", r"^new ObjectInputStream$|(^|\.)fromXML$", cwe="CWE-502", label="deserialization"),
        _s("xss", r"getWriter\(\)\.(print|println|write)$", cwe="CWE-79", label="HTML response"),
    ],
}
SINKS["typescript"] = SINKS["tsx"] = SINKS["javascript"]

# property assignments that are sinks: el.innerHTML = tainted
ASSIGN_SINKS = {"javascript": [(re.compile(r"\.(innerHTML|outerHTML)$"), "xss", "CWE-79", "high", "innerHTML")]}
ASSIGN_SINKS["typescript"] = ASSIGN_SINKS["tsx"] = ASSIGN_SINKS["javascript"]

SANITIZER = re.compile(
    r"\b(int|float|bool|Number|parseInt|parseFloat|Boolean|escape|html\.escape|escape_html|escapeHtml|"
    r"quote|quote_plus|shlex\.quote|encodeURIComponent|encodeURI|secure_filename|basename|path\.basename|"
    r"sanitize\w*|DOMPurify\.sanitize|bleach\.clean|validator\.\w+|Integer\.parseInt|Long\.parseLong|"
    r"UUID\.fromString|isValid\w*|xss|purify|uuid\.UUID)\s*\(")

NOSQL_RECEIVER = re.compile(r"(?i)(collection|\bdb\b|model|mongo|repository)|(^|\.)[A-Z]\w*$")
MONGO_LIKE = re.compile(r"(?i)\b(mongoose|mongodb|marsdb|monk|nedb|mongojs)\b|collection")
OBJECT_CAPABLE_INPUT = re.compile(r"\b(req|request)\.(body|query)\b")   # JSON/qs input can be an object: {"$ne": 1}

COMMON_NAMES = {"get", "set", "find", "map", "filter", "then", "catch", "push", "join", "log", "send", "json",
                "query", "execute", "run", "call", "apply", "bind", "toString", "split", "replace", "format",
                "append", "update", "keys", "values", "items", "open", "close", "read", "write", "next", "use",
                "render", "save", "create", "delete", "remove", "add", "init", "main", "handle", "process", "parse",
                "load", "dump", "post", "put", "exec", "__init__", "constructor", "length", "equals", "trim"}


# ------------------------------------------------------------------------------------------------ model
@dataclass
class Func:
    key: str                    # "file:line"
    name: str
    file: str
    lang: str
    start: int                  # 1-based line
    node: object
    params: list[str]
    route_params: set[int] = field(default_factory=set)   # parameters that carry request data directly
    is_method: bool = False


@dataclass
class Flow:
    kind: str
    cwe: str
    severity: str
    label: str
    file: str
    line: int
    snippet: str
    trace: tuple[str, ...]
    source: str
    files_crossed: int


# ------------------------------------------------------------------------------------------------ parsing helpers
def _text(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _line(node) -> int:
    return node.start_point[0] + 1


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


_FUNC_TYPES = {
    "python": {"function_definition"},
    "javascript": {"function_declaration", "function_expression", "arrow_function", "method_definition",
                   "generator_function_declaration", "function"},
    "java": {"method_declaration", "constructor_declaration", "lambda_expression"},
}
_FUNC_TYPES["typescript"] = _FUNC_TYPES["tsx"] = _FUNC_TYPES["javascript"]


def _func_name(node, lang) -> str:
    name = node.child_by_field_name("name")
    if name is not None:
        return _text(name)
    parent = node.parent
    if parent is not None and parent.type == "variable_declarator":
        return _text(parent.child_by_field_name("name"))
    if parent is not None and parent.type in ("assignment_expression", "pair"):
        left = parent.child_by_field_name("left") or parent.child_by_field_name("key")
        return _text(left).split(".")[-1]
    return f"<anonymous@{_line(node)}>"


def _params(node, lang) -> tuple[list[str], set[int]]:
    plist = node.child_by_field_name("parameters")
    if plist is None and lang in ("javascript", "typescript", "tsx"):
        single = node.child_by_field_name("parameter")          # x => ...
        return ([_text(single)] if single is not None else []), set()
    names, route = [], set()
    if plist is None:
        return names, route
    for i, p in enumerate(c for c in plist.children if c.is_named and c.type not in ("comment",)):
        if lang == "java":
            nm = p.child_by_field_name("name")
            names.append(_text(nm))
            if JAVA_PARAM_SOURCE.search(_text(p)):
                route.add(i)
            continue
        ident = None
        for n in _walk(p):
            if n.type in ("identifier", "shorthand_property_identifier_pattern"):
                ident = n
                break
        names.append(_text(ident) if ident is not None else _text(p).split(":")[0].split("=")[0].strip("*& "))
    return names, route


def _decorators(node) -> str:
    parent = node.parent
    if parent is not None and parent.type == "decorated_definition":
        return " ".join(_text(c) for c in parent.children if c.type == "decorator")
    return ""


def parse_functions(path: str, source: bytes, lang: str) -> list[Func]:
    from tree_sitter_language_pack import get_parser
    tree = get_parser(lang).parse(source)
    funcs = []
    for n in _walk(tree.root_node):
        if n.type not in _FUNC_TYPES[lang]:
            continue
        params, route = _params(n, lang)
        is_method = bool(params) and params[0] in ("self", "cls")
        if lang == "python" and ROUTE_DECORATOR.search(_decorators(n)):
            route = {i for i, p in enumerate(params) if p not in NOT_INPUT_PARAMS}
        funcs.append(Func(f"{path}:{_line(n)}", _func_name(n, lang), path, lang, _line(n), n, params, route, is_method))
    return funcs


# ------------------------------------------------------------------------------------------------ taint
def _strip_sanitized(expr: str) -> str:
    """Removes sanitizer(...) calls (balanced parentheses) from an expression's text."""
    out, i = [], 0
    for m in SANITIZER.finditer(expr):
        if m.start() < i:
            continue
        out.append(expr[i:m.start()])
        depth, j = 0, m.end() - 1
        while j < len(expr):
            if expr[j] == "(":
                depth += 1
            elif expr[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        i = j + 1
        out.append("0")
    out.append(expr[i:])
    return "".join(out)


def _mentions(expr: str, names: set[str]) -> str | None:
    for name in names:
        if name and re.search(r"(?<![\w.$])" + re.escape(name) + r"(?![\w$])", expr):
            return name
    return None


_CALL_TYPES = {"call", "call_expression", "method_invocation", "new_expression", "object_creation_expression"}


def _callee_text(node) -> str:
    if node.type == "method_invocation":
        obj, name = node.child_by_field_name("object"), node.child_by_field_name("name")
        return (_text(obj) + "." if obj is not None else "") + _text(name)
    if node.type in ("new_expression", "object_creation_expression"):
        ctor = node.child_by_field_name("constructor") or node.child_by_field_name("type")
        return "new " + _text(ctor)
    fn = node.child_by_field_name("function")
    return re.sub(r"\s+", "", _text(fn))


def _args(node) -> list:
    a = node.child_by_field_name("arguments")
    if a is None:
        return []
    return [c for c in a.children if c.is_named and c.type != "comment"]


class Analyzer:
    def __init__(self, files: dict[str, str | bytes]):
        self.funcs: list[Func] = []
        self.sources: dict[str, str] = {}
        self.imports: dict[str, set[str]] = {}
        for path, content in files.items():
            lang = LANG_BY_EXT.get(PurePosixPath(path).suffix.lower())
            data = content.encode("utf-8") if isinstance(content, str) else content
            if not lang or len(data) > MAX_FILE_BYTES:
                continue
            try:
                self.funcs.extend(parse_functions(path, data, lang))
            except Exception:
                continue
            text = data.decode("utf-8", "replace")
            self.sources[path] = text
            self.imports[path] = {m for m in re.findall(r"[\w$]+", " ".join(
                l for l in text.splitlines() if re.match(r"\s*(import|from|const .*require|.*require\()", l)))}
        self.by_name: dict[str, list[Func]] = {}
        for f in self.funcs:
            self.by_name.setdefault(f.name, []).append(f)
        self._summary: dict[tuple, tuple] = {}

    # ---- call resolution
    def resolve(self, caller: Func, callee: str) -> list[Func]:
        name = callee.replace("new ", "").split(".")[-1]
        cands = list(self.by_name.get(name, []))
        if not cands:
            return []
        stems = {PurePosixPath(f.file).stem for f in cands}
        local = [f for f in cands if f.file == caller.file
                 or PurePosixPath(f.file).stem in self.imports.get(caller.file, set())]
        if local:
            return local[:3]
        if name in COMMON_NAMES or len(cands) > 1 or not stems:
            return []
        return cands

    # ---- one function under a given set of tainted parameters
    def analyse(self, func: Func, tainted_params: frozenset, depth: int, trace: tuple, visiting: frozenset):
        lang = func.lang
        source_re = SOURCES[lang]
        tainted: dict[str, str] = {}                  # var -> where it got tainted (trace step)
        for i, p in enumerate(func.params):
            if i in func.route_params:
                tainted[p] = f"{func.file}:{func.start} {func.name}({p}) (request parameter)"
            elif i in tainted_params:
                tainted[p] = f"{func.file}:{func.start} {func.name}({p}) (param)"
        flows: list[Flow] = []
        returns = False
        body = func.node.child_by_field_name("body") or func.node

        def expr_taint(node) -> tuple[str | None, str]:
            """(origin step or None, description) for an expression node."""
            if node is None:
                return None, ""
            txt = _strip_sanitized(_text(node))
            m = source_re.search(txt)
            if m:
                return f"{func.file}:{_line(node)} {m.group(0)} (user input)", m.group(0)
            var = _mentions(txt, set(tainted))
            if var:
                return tainted[var], var
            for call in (n for n in _walk(node) if n.type in _CALL_TYPES):   # f(x) where f returns tainted data
                if self._call_returns_taint(func, call, tainted, depth, visiting):
                    return f"{func.file}:{_line(call)} {_callee_text(call)}(...) returns user input", _callee_text(call)
            return None, ""

        nodes = [n for n in _walk(body) if n != body]
        for _pass in range(2):                        # second pass catches loop-carried taint
            for n in nodes:
                if n.type in _FUNC_TYPES[lang] and n != func.node:
                    continue
                if self._inside_nested(n, func, lang):
                    continue
                t = n.type
                target, value = None, None
                if t in ("assignment", "augmented_assignment", "assignment_expression", "augmented_assignment_expression"):
                    target, value = n.child_by_field_name("left"), n.child_by_field_name("right")
                elif t == "variable_declarator":
                    target, value = n.child_by_field_name("name"), n.child_by_field_name("value")
                elif t in ("for_statement", "for_in_statement", "enhanced_for_statement"):
                    target = n.child_by_field_name("left") or n.child_by_field_name("name")
                    value = n.child_by_field_name("right") or n.child_by_field_name("value")
                elif t in ("with_item",):
                    value = n.child_by_field_name("value")
                if target is not None and value is not None:
                    origin, _ = expr_taint(value)
                    tname = _text(target)
                    if origin:
                        for name in re.findall(r"[A-Za-z_$][\w$]*", tname) if target.type in (
                                "pattern_list", "tuple_pattern", "object_pattern", "array_pattern",
                                "list_pattern") else [tname]:
                            tainted.setdefault(name, origin)
                    if _pass == 1 and origin:
                        for rx, kind, cwe, sev, label in ASSIGN_SINKS.get(lang, []):
                            if rx.search(tname):
                                flows.append(self._flow(kind, cwe, sev, label, func, n, origin, trace))
                if _pass == 0:
                    continue
                if t == "return_statement":
                    expr = next((c for c in n.children if c.is_named), None)
                    if expr is not None and expr_taint(expr)[0]:
                        returns = True
                if t in _CALL_TYPES:
                    callee = _callee_text(n)
                    args = _args(n)
                    for sink in SINKS[lang]:
                        if not sink.callee.search(callee):
                            continue
                        check = args if -1 in sink.args else [args[i] for i in sink.args if i < len(args)]
                        for a in check:
                            origin, _ = expr_taint(a)
                            if not origin:
                                continue
                            sev = sink.severity
                            if sink.kind == "nosql":
                                receiver = callee.rsplit(".", 1)[0] if "." in callee else ""
                                if not NOSQL_RECEIVER.search(receiver):
                                    continue              # e.g. hash.update(data): not a database call
                                mongo = MONGO_LIKE.search(receiver) or MONGO_LIKE.search(self.sources.get(func.file, ""))
                                if "$where" in _text(a):
                                    sev = "high"          # string concatenated into server-side JavaScript
                                elif not mongo:
                                    continue              # SQL ORMs (Sequelize, TypeORM) don't take operators from JSON
                                elif a.type in ("identifier", "member_expression", "subscript_expression"):
                                    sev = "high"          # whole query object comes from the user
                                elif OBJECT_CAPABLE_INPUT.search(origin + " " + _text(a)):
                                    sev = "medium"        # field value may be an operator object ({"$ne": …})
                                else:
                                    continue              # literal object with string values: not injectable
                            flows.append(self._flow(sink.kind, sink.cwe, sev, sink.label, func, n, origin, trace))
                            break
                        break
                    if lang == "python" and re.search(r"(^|\.)write$", callee) and args:
                        origin, _ = expr_taint(args[0])
                        module = self.sources.get(func.file, "")
                        if origin and re.search(r"render_template\(", module) and re.search(r"\.html|templat|\.j2|\.jinja", module):
                            flows.append(self._flow("stored-template", "CWE-1336", "medium",
                                                    "user input written to a file that may be rendered as a template",
                                                    func, n, origin, trace))
                    if depth < MAX_DEPTH:
                        origins = {i: expr_taint(a)[0] for i, a in enumerate(args)}
                        tainted_idx = frozenset(i for i, o in origins.items() if o)
                        if tainted_idx:
                            first = origins[min(tainted_idx)]
                            for target_f in self.resolve(func, callee):
                                if target_f.key in visiting:
                                    continue
                                offset = 1 if target_f.is_method and "." in callee else 0
                                idx = frozenset(i + offset for i in tainted_idx)
                                step = f"{func.file}:{_line(n)} {func.name} → {callee}(...)"
                                carried = trace + ((first,) if "(param)" not in first and first not in trace else ())
                                sub, _ = self.analyse(target_f, idx, depth + 1, carried + (step,),
                                                      visiting | {func.key})
                                flows.extend(sub)
        return flows, returns

    def _inside_nested(self, node, func, lang) -> bool:
        p = node.parent
        while p is not None and p != func.node:
            if p.type in _FUNC_TYPES[lang]:
                return True
            p = p.parent
        return False

    def _call_returns_taint(self, caller: Func, call, tainted, depth, visiting) -> bool:
        if depth >= MAX_DEPTH:
            return False
        callee = _callee_text(call)
        args = _args(call)
        idx = frozenset(i for i, a in enumerate(args) if _mentions(_strip_sanitized(_text(a)), set(tainted))
                        or SOURCES[caller.lang].search(_strip_sanitized(_text(a))))
        for f in self.resolve(caller, callee):
            if f.key in visiting or f is caller:
                continue
            offset = 1 if f.is_method and "." in callee else 0
            key = (f.key, frozenset(i + offset for i in idx))
            if key not in self._summary:
                self._summary[key] = (False,)                       # recursion guard
                _, returns = self.analyse(f, key[1], depth + 1, (), visiting | {caller.key})
                self._summary[key] = (returns,)
            if self._summary[key][0]:
                return True
        return False

    def _flow(self, kind, cwe, severity, label, func, node, origin, trace) -> Flow:
        sink_step = f"{func.file}:{_line(node)} {func.name}: {label}"
        full = trace + ((origin,) if "(param)" not in origin and origin not in trace else ()) + (sink_step,)
        files = {s.split(":")[0] for s in full}
        origin = next((s for s in full if "(param)" not in s), origin)
        line_txt = self.sources.get(func.file, "").splitlines()
        ln = _line(node)
        return Flow(kind, cwe, severity, label, func.file, ln,
                    line_txt[ln - 1].strip()[:300] if 0 < ln <= len(line_txt) else "",
                    tuple(full), origin, len(files))

    # ---- entry points
    def run(self) -> list[Flow]:
        flows: list[Flow] = []
        for f in self.funcs:
            body_text = _text(f.node)
            if not (SOURCES[f.lang].search(body_text) or f.route_params):
                continue
            found, _ = self.analyse(f, frozenset(), 0, (), frozenset())
            flows.extend(found)
            if len(flows) > MAX_FINDINGS * 3:
                break
        unique: dict[tuple, Flow] = {}
        for fl in sorted(flows, key=lambda x: len(x.trace)):         # keep the shortest trace per sink
            unique.setdefault((fl.file, fl.line, fl.kind), fl)
        return list(unique.values())[:MAX_FINDINGS]


def analyse_files(files: dict[str, str | bytes]) -> list[Flow]:
    return Analyzer(files).run()


def analyse_tree(root: Path) -> list[Flow]:
    files = {}
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in LANG_BY_EXT and "node_modules" not in p.parts:
            files[p.relative_to(root).as_posix()] = p.read_bytes()
    ensure_grammars({LANG_BY_EXT[PurePosixPath(f).suffix.lower()] for f in files})
    return analyse_files(files)
