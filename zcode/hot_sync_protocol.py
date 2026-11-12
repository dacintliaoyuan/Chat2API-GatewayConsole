"""Read-only compatibility evidence for the hot-sync protocol, not a build ID.

Compare only the inspected lock, codec and watcher implementation closure.
Unrelated bundle bytes, archive size, chunk names and package version do not
participate.  A changed critical implementation is unknown until re-examined;
this is deliberately narrower than claiming compatibility for arbitrary JS.
No installed JavaScript is executed by admission.

Two evidenced layouts are recognized, most specific first:

* the legacy host layout recorded with the 3.14.5 sample (fixed minified
  anchors and raw region digests in ``PROTOCOL_PROFILE``), and
* the current layout recorded with the 3.16.2 sample (``CURRENT_PROTOCOL_PROFILE``),
  whose regions are located by the client's own runtime naming strings and
  compared after alias normalization: imported native dependencies resolve to
  ``native:<module>:<export>``, imported string constants resolve to their
  value, cross-chunk bindings fall back to their exported name, declared
  bindings are renumbered, and property names, strings and numbers stay
  literal.  A rename-only minification therefore stays recognizable while
  any semantic change fails closed.

One runtime name can be carried by more than one declaration in the same bundle
(the 3.16.2 sample names a persistence rename helper and an unrelated
telemetry rename helper alike).  Such a duplicate is only resolved when a
reference chain from the region's already resolved anchors reaches exactly one of
the candidates; the basis is reported as evidence.  Any other duplicate, a
cross-bundle split and a missing element keep pending — a candidate is never
picked by position, length or any other guess.

A partially present or unfamiliar layout keeps pending; nothing here starts or
stops a process, writes configuration or executes installed JavaScript.
"""
from __future__ import annotations

import hashlib
import re

_MAX_HOST_BYTES = 8 * 1024 * 1024
_MAX_REGION_CHARS = 100_000
_MAX_BUNDLES = 64

# ---- 旧布局（3.14.5 样本，固定压缩锚点与原始区域摘要）----

PROTOCOL_PROFILE = {
    "lock": "9448acb5bfd59680251d41f82ba36a35e548522fb0568a6336e34095170bfe01",
    "persistence": "3cca35a7298604e0550b40ceb061181801b49fadcd60a60c69ee8ccfe1ca975a",
    "model_provider_format": "b32a62d48d0fb68c46a2cd51320786a6ac4031fb201dc337d884e5ce2c8f2b99",
    "file_codec": "cbf05a4180146ec5c3c4f1997a0a6b897b6659953daa0cd4005bd2002586c077",
    "repository": "a4a2bc573234384999c9d35600d7a059bd9b1c77912919ee70a4d5cfea72abbb",
    "runtime_wiring": "7e2bf8dabb4123c95b7389763b6550fe5802db6b5503ccd6101345fc49b3d4d4",
    "shared_model_schema": "c7fec07726f6ea03805efe2c5bdd3a232e5d6a87d1cd3d4924ee69a1ca8e043e",
    "shared_selection_schema": "d167170649bad5b9725fcdea62bab34a31259bbd2631c5c55c6dd3c46981cb07",
}

_NATIVE_BINDINGS = {
    "fs/promises": {"C6": "mkdir", "fk": "readFile", "h0": "readdir",
                    "Wm": "rmdir", "ql": "rm", "$m": "stat", "b6": "writeFile",
                    "O6": "chmod", "v0": "mkdir", "D6": "readFile",
                    "N6": "rename", "L6": "rm", "S0": "writeFile", "o3": "readFile"},
    "path": {"zm": "join", "F6": "basename", "k0": "dirname", "B6": "join"},
    "timers/promises": {"I6": "setTimeout", "U6": "setTimeout"},
    "crypto": {"x6": "createHash", "r3": "createHash"},
}


def _between(text, start, end):
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError("protocol region missing or ambiguous")
    first = text.index(start)
    last = text.index(end, first) + len(end)
    if last - first > _MAX_REGION_CHARS:
        raise ValueError("protocol region too large")
    return text[first:last]


def _class(text, name):
    pattern = (r'var [\w$]+=class\{static\{[\w$]+\(this,"'
               + re.escape(name) + r'"\)\}')
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise ValueError("watcher class missing or ambiguous")
    first = matches[0].start()
    opening = text.index("{", first)
    depth = 0
    quote = None
    escaped = False
    for pos in range(opening, min(len(text), first + _MAX_REGION_CHARS)):
        char = text[pos]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ('"', "'", "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[first:pos + 1]
    raise ValueError("watcher class is incomplete")


def protocol_regions(host, schema):
    """Extract the inspected dependency closure; reject unfamiliar layouts."""
    return {
        "lock": _between(host, "function m0()", 'i(Gl,"acquireFileLock");'),
        "persistence": _between(host, "var z6=", 'i(vr,"atomicWritePrivateTextFile");'),
        "model_provider_format": _between(
            host, "var qm=", 'i(K0,"createProviderRules");'),
        "file_codec": _between(host, "var du=", 'i(Zk,"isRecord");'),
        "repository": _between(host, "var fg=class{", 'i(Oi,"snapshotFromUpdate");'),
        "runtime_wiring": _class(host, "NodeProviderConfigRuntime"),
        "shared_model_schema": _between(
            schema, "function At(", '.strict();d();var zv='),
        "shared_selection_schema": _between(schema, "var V=", 'cS="$";'),
    }


# ---- 当前布局（3.16.2 样本；区域事实与 3.15.1 已审闭包逐条等价）----
#
# 区域按客户端自己的运行时命名字符串定位（压缩符号与 chunk 划分会变化），
# 函数体取平衡大括号；闭包按被引用的同文件声明展开。规范化把绑定标识符
# 重编号、原生导入按真实绑定解析、跨 chunk 常量解析成值，属性名/字符串/
# 数字保持字面量——等价改名不改变摘要，任何语义变化改变摘要。

CURRENT_PROTOCOL_PROFILE = {
    "lock": "45ae03a45a811373d1d4bed32c3f763a682ced71b8e014986adfe70a5721915d",
    "persistence": "06a71a226682bda8e07e3353fdb9e03193342bc08f6353d50ffe00a748191ffe",
    "model_provider_format": "513cefbe1661c8ecf85e360f086ae56943fe38ac49aac23e1586cff993048d58",
    "file_codec": "75855d0d0f288a329cc9e66d1e004d146ed455244fd7685403ede68ad506e289",
    "repository": "6ef8244493147a948f314215007b81a2a8bfc1e2257c01c0506598777be23512",
    "runtime_wiring": "a8e11814396c607e033f929a45c6eaac7f4beb0cf989b4bf56446ce177766d3b",
    "shared_selection_schema": "e742a3e3aadaab1d9f8705299e6b81fb780a0690b3c419977c9ed7ab1307908d",
    "shared_model_schema": "d31fb5d84c403bf0a5d858cfbe5379cbdc96ae1b188e49462257fe1695912692",
}

_CURRENT_REGIONS = {
    "lock": {
        "roots": ["createLockInstanceObserver", "isFileExistsError",
                   "parseLockTimestamp", "parseLockMetadata",
                   "isProcessAlive", "isOwnerFileReclaimable",
                   "removeAbandonedLock", "createFileLockTimeoutError",
                   "acquireFileLock"],
        "classes": [],
        "constants": [r"var [\w$]+=5\*6e4;"],
        "depth": 1,
    },
    "persistence": {
        "roots": ["isRetryableRenameError", "renameWithRetry",
                   "withFileLock", "atomicWritePrivateTextFile"],
        "classes": [],
        "constants": [r"var [\w$]+=\[25,50,100,200,400\],",
                      r"\[50,100,200,400,800\]"],
        "depth": 1,
    },
    "model_provider_format": {
        "roots": ["readSchemaVersion", "requireSchemaVersion",
                   "normalizeLegacyManualRules", "createProviderConfig",
                   "parseModelConfig", "createModelConfig",
                   "parseZCodeBuiltinProviderConfigRules",
                   "parsePersonalProviderConfigMap",
                   "parseZCodeBuiltinModelConfigRules",
                   "parsePersonalModelConfigRules", "createTemplateRules",
                   "createProviderRules", "decodeProviderConfigFile",
                   "encodeProviderConfigFile"],
        "classes": [],
        "constants": [r"\.literal\([\w$]+\),config:"],
        "depth": 1,
    },
    "file_codec": {
        "roots": ["readJsonFileIfExists", "emptyUpdate"],
        "classes": [],
        "constants": [],
        "depth": 1,
    },
    "repository": {
        "roots": ["snapshotFromUpdate"],
        "classes": ["NodePersonalProviderConfigRepository"],
        "constants": [],
        "depth": 1,
    },
    "runtime_wiring": {
        "roots": [],
        "classes": ["NodeProviderConfigRuntime"],
        "constants": [],
        "depth": 1,
    },
    "shared_selection_schema": {
        "roots": ["formatModelPickerValue", "parseModelPickerValue"],
        "classes": [],
        "constants": [r"providerId:[\w$]+\.string\(\)\.trim\(\)\.min\(1\),modelId:",
                      r',[\w$]+="\$";'],
        "depth": 1,
    },
    "shared_model_schema": {
        "roots": [],
        "classes": [],
        "constants": [r"properties:[\w$]+\.nullable\(\)\.optional\(\),"
                       r"optionSpecs:[\w$]+\.nullable\(\)\.optional\(\)"],
        "depth": 1,
    },
}

_KEYWORDS = frozenset("""
break case catch class const continue debugger default delete do else export extends
finally for function if import in instanceof let new return static super switch this throw
try typeof var void while with yield async await of get set true false null undefined
""".split())

_BUILTINS = frozenset("""
Object Array String Number Boolean Symbol BigInt Math JSON Date RegExp Error TypeError
RangeError SyntaxError ReferenceError EvalError URIError AggregateError Promise Map Set
WeakMap WeakSet WeakRef Proxy Reflect Function Iterator AsyncIterator Generator
console process globalThis Infinity NaN isNaN parseInt parseFloat
encodeURIComponent decodeURIComponent encodeURI decodeURI
setTimeout clearTimeout setInterval clearInterval setImmediate clearImmediate
queueMicrotask structuredClone atob btoa TextEncoder TextDecoder AbortController
AbortSignal URL URLSearchParams Buffer Uint8Array Int8Array Uint16Array Int16Array
Uint32Array Int32Array Float32Array Float64Array BigInt64Array BigUint64Array
ArrayBuffer SharedArrayBuffer DataView performance crypto navigator fetch Request
Response Headers FormData Blob File WebSocket document window self localStorage
sessionStorage arguments eval
""".split())

_MULTI_PUNCT = frozenset((
    "===", "!==", "**=", ">>>", "<<=", ">>=", "...", "&&=", "||=", "??=",
    "=>", "?.", "??", "==", "!=", "<=", ">=", "&&", "||", "++", "--", "+=",
    "-=", "*=", "/=", "**", "<<", ">>", "&=", "|=", "^=", "%=",
))


class _ProtocolUnknown(Exception):
    """The layout's structure is present but an element is missing/ambiguous."""


def _tokenize(text):
    """Tokenize JavaScript into (kind, value) pairs, template-aware."""
    tokens = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char in ('"', "'"):
            cursor = index + 1
            while cursor < length:
                if text[cursor] == "\\":
                    cursor += 2
                    continue
                if text[cursor] == char:
                    break
                cursor += 1
            tokens.append(("string", text[index:cursor + 1]))
            index = cursor + 1
            continue
        if char == "`":
            cursor = index + 1
            fragment = "`"
            while cursor < length:
                if text[cursor] == "\\":
                    fragment += text[cursor:cursor + 2]
                    cursor += 2
                    continue
                if text[cursor] == "`":
                    fragment += "`"
                    cursor += 1
                    break
                if text[cursor] == "$" and cursor + 1 < length and \
                        text[cursor + 1] == "{":
                    tokens.append(("template", fragment))
                    fragment = ""
                    depth = 1
                    inner = cursor + 2
                    cursor = inner
                    while cursor < length and depth:
                        current = text[cursor]
                        if current in ('"', "'"):
                            quote = current
                            cursor += 1
                            while cursor < length:
                                if text[cursor] == "\\":
                                    cursor += 2
                                    continue
                                if text[cursor] == quote:
                                    break
                                cursor += 1
                        elif current == "`":
                            cursor += 1
                            while cursor < length:
                                if text[cursor] == "\\":
                                    cursor += 2
                                    continue
                                if text[cursor] == "`":
                                    break
                                cursor += 1
                        elif current == "{":
                            depth += 1
                        elif current == "}":
                            depth -= 1
                            if depth == 0:
                                break
                        cursor += 1
                    tokens.extend(_tokenize(text[inner:cursor]))
                    cursor += 1
                    continue
                fragment += text[cursor]
                cursor += 1
            tokens.append(("template", fragment))
            index = cursor
            continue
        if char == "#" and index + 1 < length and (
                text[index + 1].isalpha() or text[index + 1] in "_$"):
            cursor = index + 1
            while cursor < length and (text[cursor].isalnum() or
                                      text[cursor] in "_$"):
                cursor += 1
            tokens.append(("private", text[index:cursor]))
            index = cursor
            continue
        if char.isalpha() or char in "_$":
            cursor = index
            while cursor < length and (text[cursor].isalnum() or
                                      text[cursor] in "_$"):
                cursor += 1
            tokens.append(("ident", text[index:cursor]))
            index = cursor
            continue
        if char.isdigit() or (char == "." and index + 1 < length and
                             text[index + 1].isdigit()):
            cursor = index
            while cursor < length and (text[cursor].isalnum() or
                                       text[cursor] in "._"):
                cursor += 1
            tokens.append(("number", text[index:cursor]))
            index = cursor
            continue
        for width in (3, 2):
            candidate = text[index:index + width]
            if candidate in _MULTI_PUNCT:
                tokens.append(("punct", candidate))
                index += width
                break
        else:
            tokens.append(("punct", char))
            index += 1
    return tokens


def _pattern_names(text):
    """Binding names inside a parameter or destructuring pattern."""
    names = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        part = part.lstrip(".")
        if part[:1] in ("{", "["):
            inner = part[1:-1] if part[-1:] in ("}", "]") else part[1:]
            names |= _pattern_names(inner)
            continue
        if ":" in part:
            _, _, value = part.partition(":")
            names |= _pattern_names(value)
            continue
        if "=" in part:
            part = part.split("=", 1)[0].strip()
        if re.fullmatch(r"[\w$]+", part):
            names.add(part)
    return names


def _declared_names(text):
    """All bindings the region declares (the minifier renames these)."""
    names = set()
    for match in re.finditer(r"function\s*([\w$]*)\s*\(([^)]*)\)", text):
        if match.group(1):
            names.add(match.group(1))
        names |= _pattern_names(match.group(2))
    for match in re.finditer(r"\(([^)]*)\)\s*=>", text):
        names |= _pattern_names(match.group(1))
    for match in re.finditer(r"(?:^|[^\w$.])([\w$]+)\s*=>", text):
        names.add(match.group(1))
    for match in re.finditer(r"(?:let|const|var)\s+([\w$]+)\s*=", text):
        names.add(match.group(1))
    for match in re.finditer(r",([\w$]+)=(?![=>])", text):
        names.add(match.group(1))
    for match in re.finditer(r"(?:let|const|var)\s*\{([^}]*)\}", text):
        names |= _pattern_names(match.group(1))
    for match in re.finditer(r"(?:let|const|var)\s*\[([^\]]*)\]", text):
        names |= _pattern_names(match.group(1))
    for match in re.finditer(r"catch\s*\(\s*([\w$]+)", text):
        names.add(match.group(1))
    for match in re.finditer(r"#[\w$]+", text):
        names.add(match.group(0))
    return names


def _declarable_names(text):
    """Declared names carrying an extractable function/class/var body."""
    names = set()
    for match in re.finditer(r"function\s+([\w$]+)\s*\(", text):
        names.add(match.group(1))
    for match in re.finditer(r"(?:var |let |const )([\w$]+)\s*=", text):
        names.add(match.group(1))
    for match in re.finditer(r",([\w$]+)=(?![=>])", text):
        names.add(match.group(1))
    return names


def _balanced(text, opening):
    """Extract the balanced bracket group that opens at ``opening``."""
    pairs = {"{": "}", "(": ")", "[": "]"}
    if text[opening] not in pairs:
        return None
    closer = pairs[text[opening]]
    depth = 0
    quote = None
    escaped = False
    for position in range(opening, min(len(text), opening + _MAX_REGION_CHARS)):
        char = text[position]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ('"', "'", "`"):
            quote = char
        elif char == text[opening]:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[opening:position + 1]
    return None


def _statement(text, position):
    """The statement containing ``position`` (quote-aware, bounded scan)."""
    start = text.rfind(";", 0, position) + 1
    depth = 0
    quote = None
    escaped = False
    for cursor in range(start, min(len(text), start + _MAX_REGION_CHARS)):
        char = text[cursor]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ('"', "'", "`"):
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0 and char == ";":
            return text[start:cursor + 1]
    return None


def _function_declaration(text, name):
    """Extract a uniquely defined named function's declaration, or None."""
    pattern = r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\("
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        return None
    params_open = text.index("(", matches[0].end() - 1)
    params = _balanced(text, params_open)
    if params is None:
        return None
    body_open = text.find("{", params_open + len(params))
    if body_open < 0:
        return None
    body = _balanced(text, body_open)
    if body is None:
        return None
    return text[matches[0].start():body_open + len(body)]


def _class_binding(text, match):
    """The ``var <name>=class`` binding of one class naming call.

    Returns ``(binding name, declaration)``; the binding name is ``None`` when
    the declaration is not introduced by a plain ``var`` (it then can never be
    proven reachable by name, so it stays an unresolved candidate).
    """
    head = text.rfind("=class{", 0, match.start())
    if head < 0:
        return None, None
    body = _balanced(text, head + len("=class"))
    if body is None:
        return None, None
    declaration = text[head:head + len("=class") + len(body)]
    start = text.rfind("var ", 0, head)
    if start < 0:
        return None, declaration
    name = text[start + len("var "):head]
    if not re.fullmatch(r"[\w$]+", name):
        return None, declaration
    return name, text[start:head + len("=class") + len(body)]


def _class_declaration(text, name):
    """Extract the class declaration carrying the runtime name ``name``."""
    matches = _naming_calls(text, name, class_call=True)
    if len(matches) != 1:
        return None
    return _class_binding(text, matches[0])[1]


def _class_by_name(text, name):
    """Extract ``var <name>=class{...}`` if it is uniquely defined."""
    pattern = r"(?:var |let |const )" + re.escape(name) + r"=class"
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        return None
    opening = text.index("{", matches[0].end())
    return _balanced(text, opening)


def _import_map(text):
    """Local name -> (module, exported name) for one bundle."""
    mapping = {}
    for bindings, module in re.findall(r'import\{([^{}]+)\}from"([^"]+)"',
                                      text):
        for item in bindings.split(","):
            pair = re.fullmatch(r"([\w$]+) as ([\w$]+)", item)
            if pair is not None:
                mapping[pair.group(2)] = (module, pair.group(1))
    return mapping


def _export_map(text):
    """Exported name -> local name for one bundle."""
    mapping = {}
    tail = text.rfind("export{")
    if tail < 0:
        return mapping
    closing = text.find("}", tail)
    if closing < 0:
        return mapping
    for item in text[tail + len("export{"):closing].split(","):
        pair = re.fullmatch(r"([\w$]+) as ([\w$]+)", item)
        if pair is not None:
            mapping[pair.group(2)] = pair.group(1)
    return mapping


def _bundles(asar, reader, entries):
    """Read the bounded out/host bundles once (entries are listing names)."""
    bundles = {}
    for name in list(entries)[:_MAX_BUNDLES]:
        if not name.endswith(".js"):
            continue
        raw = reader(asar, "out/host/" + name)
        if raw is None or len(raw) > _MAX_HOST_BYTES:
            continue
        bundles[name] = raw.decode("utf-8")
    return bundles


def _region_file(bundles, region):
    """Return ``(file, missing)`` for one region.

    ``(None, None)``: no element anchor is present anywhere — the layout is
    absent.  ``(None, label)``: some anchors are present but the region
    cannot be resolved to one bundle — a changed implementation.  Otherwise
    the single bundle carrying every element's anchor.
    """
    elements = _element_patterns(region)
    present = []
    for label, pattern in elements:
        candidates = {name for name, text in bundles.items()
                      if re.search(pattern, text)}
        if candidates:
            present.append((label, candidates))
    if not present:
        return None, None
    intersection = None
    for _, candidates in present:
        intersection = candidates if intersection is None else (
            intersection & candidates)
    if intersection and len(intersection) == 1:
        return intersection.pop(), None
    if len(present) != len(elements):
        missing = next(label for label, _ in elements
                       if label not in [name for name, _ in present])
        return None, missing
    return None, "协议区域所在 bundle 无法唯一确认"


def _element_patterns(region):
    """Labeled search pattern per declared element (runtime names only)."""
    patterns = [("函数 " + name,
                 r'[\w$]+\([\w$]+,"' + re.escape(name) + r'"\)')
                for name in region["roots"]]
    patterns += [("类 " + name, r'[\w$]+\(this,"' + re.escape(name) + r'"\)')
                 for name in region["classes"]]
    patterns += [("常量 " + pattern, pattern) for pattern in region["constants"]]
    return patterns


def _naming_calls(text, runtime_name, *, class_call=False):
    """Every runtime naming call of a declaration, in source order.

    ``i(fn,"acquireFileLock")`` names a function and
    ``i(this,"NodeProviderConfigRuntime")`` names a class.  A minifier may
    carry one runtime name on more than one declaration in the same bundle, so
    the caller has to decide which declaration the region means.
    """
    if class_call:
        pattern = r'[\w$]+\(this,"' + re.escape(runtime_name) + r'"\)'
    else:
        pattern = r'[\w$]+\(([\w$]+),"' + re.escape(runtime_name) + r'"\)'
    return list(re.finditer(pattern, text))


def _shadowed_names(text):
    """Bindings that shadow a module-level name inside ``text``.

    A deliberate superset of ``_declared_names``: a reference is only claimed
    when the referring declaration cannot bind that name itself, so an unproven
    reference is never used as a disambiguation basis.
    """
    names = _declared_names(text)
    names |= set(re.findall(r"\bclass\s+([\w$]+)", text))
    names |= set(re.findall(r"\b(?:let|const|var)\s+([\w$]+)\s+(?:of|in)\b",
                          text))
    for match in re.finditer(r"catch\s*\(\s*\{([^}]*)\}", text):
        names |= _pattern_names(match.group(1))
    return names


def _references(declaration, name):
    """Whether ``declaration`` references the module-level binding ``name``."""
    if name in _shadowed_names(declaration):
        return False
    return any(kind == "ident" and value == name
               for kind, value in _tokenize(declaration))


def _element_reason(kind):
    """The existing fail-closed reason for a missing or ambiguous element."""
    if kind == "class":
        return "协议类缺失或歧义："
    return "协议函数命名缺失或歧义："


def _element_candidates(text, region):
    """Labeled candidates per declared element (runtime names only).

    Each entry is ``(label, kind, name, candidates)``.  For a constant,
    ``candidates`` is empty and ``name`` is its search pattern; otherwise
    ``candidates`` is the ordered list of ``(binding name, declaration)`` pairs
    whose naming call carries the runtime name.  A declaration that cannot be
    extracted keeps its ``None`` entry in the candidate list, so an unextractable
    candidate can never be dropped in favour of a resolvable one.
    """
    elements = []
    for name in region["roots"]:
        elements.append(("root:" + name, "function", name, [
            (match.group(1), _function_declaration(text, match.group(1)))
            for match in _naming_calls(text, name)]))
    for name in region["classes"]:
        elements.append(("class:" + name, "class", name, [
            _class_binding(text, match)
            for match in _naming_calls(text, name, class_call=True)]))
    for pattern in region["constants"]:
        elements.append(("constant:" + pattern, "constant", pattern, []))
    return elements


def _region_elements(bundles, region, file):
    """Locate every declared element of a region inside its bundle.

    Returns ``(elements, notes)``.  An element whose runtime name is carried by
    exactly one declaration resolves directly.  When one runtime name is declared
    more than once in the bundle, only a reference chain from the region's already
    resolved anchors (its other roots, classes and constants) can decide which
    declaration the region means: the surviving candidate must then be unique and
    the basis is reported in ``notes``.  A duplicate that stays unresolved, a
    missing element and an unresolvable naming all fail closed — a candidate is
    never chosen by position, length or any other guess.
    """
    text = bundles[file]
    entries = _element_candidates(text, region)
    resolved = {}
    notes = []
    ambiguous = []
    for label, kind, name, candidates in entries:
        if kind == "constant":
            matches = list(re.finditer(name, text))
            if len(matches) != 1:
                raise _ProtocolUnknown("协议常量缺失或歧义：" + name)
            declaration = _statement(text, matches[0].start())
            if declaration is None:
                raise _ProtocolUnknown("协议常量声明无法提取：" + name)
            resolved[label] = declaration
            continue
        unique = []
        for candidate in candidates:
            if candidate[0] is None or all(candidate[0] != seen[0]
                                          for seen in unique):
                unique.append(candidate)
        if not unique:
            raise _ProtocolUnknown(_element_reason(kind) + name)
        if len(unique) == 1:
            if unique[0][1] is None:
                raise _ProtocolUnknown(_element_reason(kind) + name)
            resolved[label] = unique[0][1]
            continue
        ambiguous.append((label, kind, name, unique))
    while ambiguous:
        pending = []
        progress = False
        for label, kind, name, unique in ambiguous:
            reached = []
            for candidate in unique:
                if candidate[0] is None or candidate[1] is None:
                    continue
                anchors = [anchor for anchor, declaration in resolved.items()
                           if _references(declaration, candidate[0])]
                if anchors:
                    reached.append((candidate, anchors))
            if len(reached) != 1:
                pending.append((label, kind, name, unique))
                continue
            candidate, anchors = reached[0]
            resolved[label] = candidate[1]
            notes.append(
                "同名消歧：%s 在 %s 内命名 %d 次（%s）；仅 %s 被区域锚点 %s 引用"
                % (name, file, len(unique),
                   "、".join(entry[0] or "?" for entry in unique),
                   candidate[0], "、".join(sorted(anchors))))
            progress = True
        if not progress:
            break
        ambiguous = pending
    if ambiguous:
        _, _, name, unique = ambiguous[0]
        raise _ProtocolUnknown(
            "协议同名声明无法消歧：%s 在 %s 内命名 %d 次（%s）"
            % (name, file, len(unique),
               "、".join(entry[0] or "?" for entry in unique)))
    return ([(label, resolved[label]) for label, _, _, _ in entries
             if label in resolved], notes)


def _declaration_of(text, name):
    """Extract the declaration that declares ``name``, or None if ambiguous."""
    pattern = r"(?:var |let |const |,)\s*" + re.escape(name) + r"=(?![=>])"
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        return None
    return _statement(text, matches[0].start())


def _closure(bundles, cache, file, elements, depth):
    """Expand same-file referenced declarations, one level at a time.

    Names the region itself declares (parameters, locals, catch bindings) are
    shadowed inside it and never pulled in as separate declarations.
    """
    text = bundles[file]
    declarable = _bundle_analysis(bundles, cache, file)["declarable"]
    seen = set()
    for _, declaration in elements:
        seen |= _declared_names(declaration)
    collected = list(elements)
    frontier = [declaration for _, declaration in elements]
    for _ in range(depth):
        following = []
        for declaration in frontier:
            for kind, value in _tokenize(declaration):
                if kind != "ident" or value in seen or value not in declarable:
                    continue
                body_text = _function_declaration(text, value)
                if body_text is None:
                    body_text = _class_by_name(text, value)
                if body_text is None:
                    body_text = _declaration_of(text, value)
                if body_text is None:
                    continue
                seen.add(value)
                following.append(body_text)
        collected.extend(("closure:#%d" % (index + 1), declaration)
                         for index, declaration in enumerate(following))
        frontier = following
    return collected


def _bundle_analysis(bundles, cache, file):
    """Per-bundle analysis, computed once per inspection round."""
    if file not in cache:
        text = bundles[file]
        cache[file] = {
            "imports": _import_map(text),
            "exports": _export_map(text),
            "declared": _declared_names(text),
            "declarable": _declarable_names(text),
        }
    return cache[file]


def _import_token(bundles, cache, file, local):
    """Canonical token for an imported binding (identity is verified).

    The actually consumed dependency must resolve: a missing target module, a
    missing export or an implementation that cannot be confirmed is unknown
    (fail closed).  An export-name-only fallback would let a re-pointed import
    to a missing file keep the same digest.  A scalar export contributes its
    value; any other export contributes the normalized declaration it resolves
    to, so a rename of module/chunk stays stable while a changed
    implementation or a re-pointed import changes the token.
    """
    module, exported = _bundle_analysis(bundles, cache, file)["imports"][local]
    if not module.startswith("."):
        return "native:%s:%s" % (module, exported)
    target_name = module[2:] if module.startswith("./") else None
    target = bundles.get(target_name) if target_name else None
    if target is None:
        raise _ProtocolUnknown("协议依赖模块缺席：" + module)
    definition = _bundle_analysis(bundles, cache, target_name)["exports"].get(
        exported)
    if definition is None:
        raise _ProtocolUnknown("协议依赖导出未找到：%s#%s" % (module, exported))
    match = re.search(
        r"(?:var |let |const )" + re.escape(definition)
        + r'=("(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?(?:e\d+)?)[;,)]', target)
    if match is not None:
        return "const:%s" % match.group(1)
    declaration = _dependency_declaration(bundles, cache, target_name, definition)
    if declaration is None:
        raise _ProtocolUnknown("协议依赖实现无法确认：%s#%s" % (module, exported))
    digest = hashlib.sha256(_normalize(
        bundles, cache, target_name, declaration).encode("utf-8")).hexdigest()
    return "dep:%s" % digest[:16]


def _dependency_declaration(bundles, cache, file, name):
    """The declaration a cross-chunk export resolves to, or None."""
    text = bundles[file]
    declaration = _function_declaration(text, name)
    if declaration is None:
        declaration = _class_by_name(text, name)
    if declaration is None:
        declaration = _declaration_of(text, name)
    return declaration


def _normalize(bundles, cache, file, text):
    """Alpha-normalize one region body; imported identities stay verified."""
    analysis = _bundle_analysis(bundles, cache, file)
    imports = analysis["imports"]
    declared = analysis["declared"]
    placeholders = {}
    counter = [0]
    tokens = _tokenize(text)
    output = []
    for index, (kind, value) in enumerate(tokens):
        if kind not in ("ident", "private"):
            output.append(value)
            continue
        previous = tokens[index - 1] if index else (None, None)
        following = tokens[index + 1] if index + 1 < len(tokens) else (None, None)
        if kind == "ident" and (value in _KEYWORDS or value in _BUILTINS):
            output.append(value)
            continue
        if previous[0] == "punct" and previous[1] in (".", "?."):
            output.append(value)
            continue
        if following[0] == "punct" and following[1] == ":":
            output.append(value)
            continue
        if kind == "ident" and value in imports:
            output.append(_import_token(bundles, cache, file, value))
            continue
        if value in declared:
            if value not in placeholders:
                counter[0] += 1
                placeholders[value] = "#%d" % counter[0]
            output.append(placeholders[value])
            continue
        output.append(value)
    return "".join(output)


def _digest(elements):
    """Digest of the concatenated, normalized region closure."""
    material = "".join("%s:%s\n" % (label, body) for label, body in elements)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _region_digest(bundles, cache, file, elements, depth):
    """Digest of one region's extracted elements and their closure."""
    collected = _closure(bundles, cache, file, elements, depth)
    normalized = [(label, _normalize(bundles, cache, file, declaration))
                  for label, declaration in collected]
    return _digest(normalized)


def inspect_installed_regions(asar, reader, entries):
    """Read-only per-region extraction evidence for one installation.

    Returns ``None`` when no current layout is recognized, otherwise one record
    per region in declaration order with ``region``, ``file``, ``missing``,
    ``elements`` (the extracted ``(label, source)`` pairs a later semantic
    review compares against the recorded contract), ``notes`` (the basis of every
    duplicate disambiguation), ``digest``, ``expected`` and ``matches``.  This
    never admits or publishes anything and never executes installed JavaScript;
    a region that cannot be extracted keeps its fail-closed reason in ``missing``.
    """
    bundles = _bundles(asar, reader, entries)
    if not bundles:
        return None
    cache = {}
    records = []
    for name, region in _CURRENT_REGIONS.items():
        record = {"region": name, "file": None, "missing": None,
                  "elements": [], "notes": [], "digest": None,
                  "expected": CURRENT_PROTOCOL_PROFILE.get(name),
                  "matches": False}
        file, missing = _region_file(bundles, region)
        record["file"], record["missing"] = file, missing
        if file is not None:
            try:
                elements, notes = _region_elements(bundles, region, file)
                digest = _region_digest(bundles, cache, file, elements,
                                       region["depth"])
            except _ProtocolUnknown as exc:
                record["missing"] = str(exc)
            else:
                record["elements"], record["notes"] = elements, notes
                record["digest"] = digest
                record["matches"] = digest == record["expected"]
        records.append(record)
    return records


def _inspect_current_layout(asar, reader, entries):
    """Return (compatible, reason) for the current layout, or None if absent."""
    bundles = _bundles(asar, reader, entries)
    if not bundles:
        return None
    return _inspect_current_layout_bundles(bundles)


def _inspect_current_layout_bundles(bundles):
    """Run the current-layout check against an in-memory bundle map.

    Also used by the real-installation read-only regression: a mutated copy of
    the running installation's bundles must fail closed without touching disk.
    """
    cache = {}
    files = {}
    for name, region in _CURRENT_REGIONS.items():
        file, missing = _region_file(bundles, region)
        if file is None and missing is None:
            return None
        if file is None:
            return False, ("协议证据无法解析或不支持，继续待应用（%s）"
                           % missing)
        files[name] = file
    if set(_CURRENT_REGIONS) != set(CURRENT_PROTOCOL_PROFILE):
        return False, "协议证据表缺失或不完整"
    for name, region in _CURRENT_REGIONS.items():
        file = files[name]
        try:
            elements, _ = _region_elements(bundles, region, file)
            digest = _region_digest(bundles, cache, file, elements,
                                   region["depth"])
        except _ProtocolUnknown as exc:
            return False, "协议证据无法解析或不支持，继续待应用（%s）" % exc
        if digest != CURRENT_PROTOCOL_PROFILE[name]:
            return False, "协议兼容性待确认：" + name
    return True, "文件锁、配置格式和变更监听协议兼容"


def _inspect_legacy_layout(asar, reader):
    """Return (compatible, reason) for the legacy layout, or None if absent.

    The legacy host anchors decide presence: when they are absent the current
    layout is tried instead.  Once the legacy layout is present, every
    dependency and region failure is a changed implementation with its own
    reason (pending), never a silent fall-through.
    """
    try:
        host = reader(asar, "out/host/index.js")
    except (OSError, ValueError, RecursionError, OverflowError):
        return None
    if host is None or len(host) > _MAX_HOST_BYTES:
        return None
    try:
        host_text = host.decode("utf-8")
    except UnicodeError:
        return None
    if "function m0()" not in host_text:
        return None
    try:
        native = {}
        for bindings, module in re.findall(
                r'import\{([^{}]+)\}from"([^"]+)"', host_text):
            for item in bindings.split(","):
                pair = re.fullmatch(r"([\w$]+) as ([\w$]+)", item)
                if pair is not None:
                    native.setdefault(pair.group(2), []).append(
                        (module, pair.group(1)))
        for module, symbols in _NATIVE_BINDINGS.items():
            for local, exported in symbols.items():
                if native.get(local) != [(module, exported)]:
                    return False, "协议原生依赖绑定无法确认：" + local
        # Resolve the dependency actually imported for ModelConfig's schema.
        # A renamed chunk is acceptable; an unrelated decoy chunk is not.
        imports = re.findall(r'import\{([^{}]+)\}from"(\./[^"]+)"', host_text)
        owners = [(bindings, path) for bindings, path in imports
                  if re.search(r'(?:^|,)[\w$]+ as ss(?:,|$)', bindings)]
        if len(owners) != 1:
            return False, "配置格式的schema依赖无法唯一确认"
        bindings, relative = owners[0]
        if not re.fullmatch(r'\./[A-Za-z0-9_-]+\.js', relative):
            return False, "配置格式的schema依赖路径不支持"
        raw = reader(asar, "out/host/" + relative[2:])
        if raw is None or len(raw) > _MAX_HOST_BYTES:
            return False, "配置格式的schema依赖缺失或超过读取上界"
        schema = raw.decode("utf-8")
        # The external schema and timeout symbols must remain bound to the
        # inspected declarations, not just present somewhere in the archive.
        expected_symbols = {"ss": "tA", "zo": "vK", "wr": "V", "lT": "l8"}
        for local, declaration in expected_symbols.items():
            matched = re.search(r'(?:^|,)([\w$]+) as ' + local + r'(?:,|$)',
                               bindings)
            if matched is None:
                return False, "协议依赖绑定无法确认：" + local
            export = schema.rfind("export{")
            if export < 0 or not re.search(
                    r'(?:\{|,)' + declaration + r' as '
                    + re.escape(matched.group(1)) + r'(?:,|\})',
                    schema[export:]):
                return False, "协议依赖导出无法确认：" + local
        if schema.count('var l8="ZCODE_FILE_LOCK_TIMEOUT";') != 1:
            return False, "文件锁超时错误码不兼容"
        regions = protocol_regions(host_text, schema)
        if set(PROTOCOL_PROFILE) != set(regions):
            return False, "协议证据表缺失或不完整"
        for name, source in regions.items():
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if digest != PROTOCOL_PROFILE[name]:
                return False, "协议兼容性待确认：" + name
        return True, "文件锁、配置格式和变更监听协议兼容"
    except (ValueError, UnicodeError, OSError, RecursionError, OverflowError):
        return False, "协议证据无法解析或不支持，继续待应用"


def inspect_installed_protocol(asar, reader, entries):
    """Return compatibility and an explicit reason, with no OS mutations."""
    legacy = _inspect_legacy_layout(asar, reader)
    if legacy is not None:
        return legacy
    current = _inspect_current_layout(asar, reader, entries)
    if current is not None:
        return current
    return False, "协议证据无法解析或不支持，继续待应用"
