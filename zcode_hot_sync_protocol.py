"""Read-only compatibility evidence for the hot-sync protocol, not a build ID.

Compare only the inspected lock, codec and watcher implementation closure.
Unrelated bundle bytes, archive size, chunk names and package version do not
participate. A changed critical implementation is unknown until re-examined;
this is deliberately narrower than claiming compatibility for arbitrary JS.
No installed JavaScript is executed by admission.
"""
from __future__ import annotations

import hashlib
import re

_MAX_HOST_BYTES = 8 * 1024 * 1024
_MAX_REGION_CHARS = 100_000
PROTOCOL_PROFILE = {
    "lock": "9448acb5bfd59680251d41f82ba36a35e548522fb0568a6336e34095170bfe01",
    "persistence": "3cca35a7298604e0550b40ceb061181801b49fadcd60a60c69ee8ccfe1ca975a",
    "model_provider_format": "de9bd44eb69f6239ecfd0ea7fec80db3dfb515e07bcc1c34494795736ab7143d",
    "file_codec": "a40b0112e0295cdfba1e00d0f461d138d0e3485d3c14544c4a2209b5594181c8",
    "repository": "39c0f66c7910b8cfd13f9b17a8f41f42ff86afa06b8c26767913fe80ba130d28",
    "runtime_wiring": "b7de759f9e1237efed4c70393e9c045e2c5e5d4723e1451fcf672fc9c55d5972",
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
        "shared_selection_schema": _between(
            schema, "var V=", 'cS="$";'),
    }


def inspect_installed_protocol(asar, reader):
    """Return compatibility and an explicit reason, with no OS mutations."""
    try:
        raw = reader(asar, "out/host/index.js")
        if raw is None or len(raw) > _MAX_HOST_BYTES:
            return False, "宿主协议入口缺失或超过读取上界"
        host = raw.decode("utf-8")
        native = {}
        for bindings, module in re.findall(
                r'import\{([^{}]+)\}from"([^"]+)"', host):
            for item in bindings.split(","):
                pair = re.fullmatch(r'([\w$]+) as ([\w$]+)', item)
                if pair is not None:
                    native.setdefault(pair[2], []).append((module, pair[1]))
        for module, symbols in _NATIVE_BINDINGS.items():
            for local, exported in symbols.items():
                if native.get(local) != [(module, exported)]:
                    return False, "协议原生依赖绑定无法确认：" + local
        # Resolve the dependency actually imported for ModelConfig's schema.
        # A renamed chunk is acceptable; an unrelated decoy chunk is not.
        imports = re.findall(r'import\{([^{}]+)\}from"(\./[^"]+)"', host)
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
        regions = protocol_regions(host, schema)
        if set(PROTOCOL_PROFILE) != set(regions):
            return False, "协议证据表缺失或不完整"
        for name, source in regions.items():
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if digest != PROTOCOL_PROFILE[name]:
                return False, "协议兼容性待确认：" + name
        return True, "文件锁、配置格式和变更监听协议兼容"
    except (ValueError, UnicodeError, OSError, RecursionError, OverflowError):
        return False, "协议证据无法解析或不支持，继续待应用"
