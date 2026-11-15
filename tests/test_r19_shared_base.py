# -*- coding: utf-8 -*-
"""E19-1-01A: the ``shared`` package is a side-effect-free boundary.

``shared`` is the package both gateway applications bundle. Importing it must not
load a host, state, credential, broker or operation module, and every source file
under ``shared/`` may only import the standard library (pure computation modules)
or ``shared`` itself.

The sys.modules assertion runs in a fresh interpreter so it stays valid when the
whole test suite imports host modules in the same process.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

AI_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = AI_GATEWAY_ROOT / "shared"
INIT_PATH = SHARED_DIR / "__init__.py"
README_PATH = SHARED_DIR / "README.md"

sys.path.insert(0, str(AI_GATEWAY_ROOT))

import shared  # noqa: E402

# Pure computation modules only: no file, network, thread, lock, process or
# logging side effects are allowed in shared sources.
ALLOWED_STDLIB_IMPORTS = frozenset({
    "__future__",
    "base64",
    "binascii",
    "collections",
    "copy",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "functools",
    "hashlib",
    "itertools",
    "json",
    "math",
    "re",
    "secrets",
    "string",
    "time",
    "types",
    "typing",
    "unicodedata",
    "uuid",
})

FORBIDDEN_EXACT_MODULES = ("usagesrv", "chat2api", "local", "cloud")
FORBIDDEN_PREFIXES = ("console_", "share_", "model_")

REQUIRED_README_TERMS = (
    "双端",
    "纯协议",
    "解析",
    "纯规则",
    "宿主",
    "状态",
    "文件",
    "网络",
    "Key",
    "broker",
    "OperationManager",
)

CLEAN_INTERPRETER_PROBE = """
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import shared

print(json.dumps({
    "file": str(Path(shared.__file__).resolve()),
    "modules": sorted(sys.modules),
}))
"""


def _is_forbidden_business_module(name):
    if name in FORBIDDEN_EXACT_MODULES:
        return True
    if any(name.startswith(token + ".") for token in FORBIDDEN_EXACT_MODULES):
        return True
    return any(name.startswith(prefix) for prefix in FORBIDDEN_PREFIXES)


def _source_files():
    return sorted(SHARED_DIR.rglob("*.py"))


def _import_violations(path):
    """Return human-readable violations for one shared source file."""
    relative = path.relative_to(SHARED_DIR)
    package_depth = len(relative.parts)
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root != "shared" and root not in ALLOWED_STDLIB_IMPORTS:
                    violations.append(
                        "{}:{}: import {}".format(
                            relative.as_posix(), node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            statement = ast.unparse(node)
            if node.level:
                if node.level > package_depth:
                    violations.append(
                        "{}:{}: relative import escapes shared: {}".format(
                            relative.as_posix(), node.lineno, statement))
                continue
            root = (node.module or "").split(".")[0]
            if root != "shared" and root not in ALLOWED_STDLIB_IMPORTS:
                violations.append(
                    "{}:{}: {}".format(
                        relative.as_posix(), node.lineno, statement))
    return violations


class SharedBaseTests(unittest.TestCase):

    def test_shared_package_imports_from_the_gateway_root(self):
        self.assertTrue(
            hasattr(shared, "__path__"),
            "shared must be an importable package",
        )
        self.assertEqual(
            Path(shared.__file__).resolve(),
            INIT_PATH.resolve(),
            "shared must resolve to ai-gateway/shared/__init__.py",
        )
        self.assertTrue(
            (shared.__doc__ or "").strip(),
            "shared/__init__.py must document the package boundary",
        )

    def test_shared_init_declares_only_a_docstring(self):
        tree = ast.parse(INIT_PATH.read_text(encoding="utf-8-sig"),
                         filename=str(INIT_PATH))
        self.assertTrue(tree.body, "shared/__init__.py must not be empty")
        first = tree.body[0]
        self.assertIsInstance(first, ast.Expr,
                              "shared/__init__.py must start with a docstring")
        self.assertIsInstance(first.value, ast.Constant,
                              "shared/__init__.py docstring must be a constant")
        self.assertIsInstance(first.value.value, str,
                              "shared/__init__.py docstring must be text")
        self.assertTrue(first.value.value.strip(),
                        "shared/__init__.py docstring must not be blank")
        self.assertEqual(
            [type(node).__name__ for node in tree.body[1:]],
            [],
            "shared/__init__.py must only contain a docstring, no imports or "
            "assignments",
        )

    def test_shared_sources_import_only_allowlisted_stdlib_or_shared(self):
        sources = _source_files()
        self.assertIn(INIT_PATH, sources, "shared/__init__.py must exist")
        violations = []
        for path in sources:
            violations.extend(_import_violations(path))
        self.assertEqual(
            violations, [],
            "shared sources import host or non-allowlisted modules: {}".format(
                "; ".join(violations)),
        )

    def test_clean_interpreter_importing_shared_loads_no_host_modules(self):
        self.assertTrue(sys.executable, "sys.executable must be available")
        env = {key: value for key, value in os.environ.items()
               if key.upper() != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory() as isolated_cwd:
            completed = subprocess.run(
                [sys.executable, "-I", "-c", CLEAN_INTERPRETER_PROBE,
                 str(AI_GATEWAY_ROOT)],
                cwd=isolated_cwd,
                env=env,
                capture_output=True,
                encoding="utf-8",
                timeout=60,
            )
        self.assertEqual(
            completed.returncode, 0,
            "clean interpreter probe failed: {}".format(completed.stderr),
        )
        output = completed.stdout.strip()
        self.assertTrue(output, "clean interpreter probe printed nothing")
        payload = json.loads(output.splitlines()[-1])
        self.assertEqual(
            Path(payload["file"]),
            INIT_PATH.resolve(),
            "the probe imported a different shared package",
        )
        modules = payload["modules"]
        self.assertIn("shared", modules)
        self.assertEqual(
            sorted(name for name in modules
                   if _is_forbidden_business_module(name)),
            [],
            "importing shared loaded host/business modules",
        )
        self.assertEqual(
            sorted(name for name in modules if name.startswith("shared.")),
            [],
            "importing shared must not import its submodules",
        )

    def test_shared_readme_documents_the_boundary(self):
        self.assertTrue(README_PATH.is_file(), "shared/README.md must exist")
        text = README_PATH.read_text(encoding="utf-8-sig")
        self.assertTrue(text.strip(), "shared/README.md must not be empty")
        missing = [term for term in REQUIRED_README_TERMS if term not in text]
        self.assertEqual(
            missing, [],
            "shared/README.md must state the boundary; missing: {}".format(
                ", ".join(missing)),
        )

    def test_shared_readme_explains_the_threading_runtime_exception(self):
        """Why ``threading`` is not in the runtime forbidden list, in writing."""
        text = README_PATH.read_text(encoding="utf-8-sig")
        self.assertIn(
            "threading", text,
            "shared/README.md must name the threading exception")
        self.assertRegex(
            text, r"sitecustomize|apport_python_hook",
            "shared/README.md must explain that the interpreter's "
            "sitecustomize/apport hook preloads threading")
        # The static property is still absolute: the README must not present the
        # runtime exception as permission to import threading in shared source.
        self.assertRegex(
            text, r"源码|source",
            "shared/README.md must distinguish the source-level rule from the "
            "runtime observation")


if __name__ == "__main__":
    unittest.main()
