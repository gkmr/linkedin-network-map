import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(REPO, "examples")
CONNECTIONS = os.path.join(EXAMPLES, "connections.example.csv")
TARGETS = os.path.join(EXAMPLES, "targets.example.txt")
EXPECTED = os.path.join(EXAMPLES, "expected")
EXPORT_DATE = "2026-11-01"
EXAMPLE_LIMIT = 600          # small on purpose, so the example shows split parts


def load(name):
    sys.dont_write_bytecode = True                # no __pycache__ next to the scripts
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()
