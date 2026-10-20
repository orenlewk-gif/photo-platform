"""Move route handlers from main.py into routes/<area>.py unchanged (a refactor helper).

    .venv/bin/python dev/move_routes.py <area> <handler_name> [<handler_name> ...]

Each handler keeps its decorator (@app.x -> @router.x) and body. Names it uses that main.py
defines (helpers, settings, the photo list)
are read as main.<name> when the route runs. main.py includes the router at the bottom.
"""
import ast
import builtins
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, "main.py")
REASSIGNED = {"data", "PORTRAIT_LOCATIONS", "_dim_cache", "_watermark_cache"}


def main():
    area, names = sys.argv[1], sys.argv[2:]
    src = open(MAIN).read()
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)

    # what main.py defines at top level, and what each import line brings in
    defined, import_of = set(), {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        defined.add(n.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                nm = (a.asname or a.name).split(".")[0]
                import_of[nm] = ast.get_source_segment(src, node) if isinstance(node, ast.Import) else \
                    f"from {node.module} import {a.name}" + (f" as {a.asname}" if a.asname else "")

    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = [n for n in names if n not in funcs]
    if missing:
        sys.exit(f"not found: {missing}")

    chunks, need_main, need_imports, spans = [], set(), set(), []
    for name in names:
        fn = funcs[name]
        start = min([d.lineno for d in fn.decorator_list] + [fn.lineno]) - 1
        end = fn.end_lineno
        spans.append((start, end))
        text = "".join(lines[start:end])
        local = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                local.add(n.id)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and n is not fn:
                if not isinstance(n, ast.Lambda):
                    local.add(n.name)
                local |= {a.arg for a in n.args.args + n.args.kwonlyargs}
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                local |= {(a.asname or a.name).split(".")[0] for a in n.names}
            elif isinstance(n, ast.ExceptHandler) and n.name:
                local.add(n.name)
            elif isinstance(n, ast.comprehension):
                for t in ast.walk(n.target):
                    if isinstance(t, ast.Name):
                        local.add(t.id)
        globals_decl = {g for n in ast.walk(fn) if isinstance(n, ast.Global) for g in n.names}
        if globals_decl - REASSIGNED:
            sys.exit(f"{name} assigns globals {globals_decl - REASSIGNED}; move it by hand")
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn and
               any(isinstance(g, ast.Global) for g in ast.walk(n)) for n in ast.walk(fn)):
            sys.exit(f"{name} has an inner function with 'global'; move it by hand")
        local -= globals_decl            # `global data` + `data = ...` means main.data
        # Names main.py defines are read as main.<name> when the route runs (so later
        # reassignments and test stand-ins are seen); replaced at their exact positions.
        edits = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id in globals_decl:
                edits.append((n.lineno, n.col_offset, n.end_col_offset, "main." + n.id))
                continue
            if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)) or n.id in local:
                continue
            if n.id == "app" or n.id in dir(builtins) and n.id not in defined:
                continue
            if n.id in import_of:
                need_imports.add(import_of[n.id])
            elif n.id in defined or n.id in REASSIGNED:
                edits.append((n.lineno, n.col_offset, n.end_col_offset, "main." + n.id))
            else:
                sys.exit(f"{name}: don't know where '{n.id}' comes from")
        flines = text.splitlines(keepends=True)
        for ln, c0, c1, rep in sorted(edits, reverse=True):
            i = ln - 1 - start
            flines[i] = flines[i][:c0] + rep + flines[i][c1:]
        text = "".join(flines)
        text = re.sub(r"^[ \t]+global [\w, ]+\n", "", text, flags=re.M)   # now main.<name>
        text = re.sub(r"^@app\.", "@router.", text, flags=re.M)
        chunks.append(text)

    out = os.path.join(REPO, "routes", f"{area}.py")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    init = os.path.join(REPO, "routes", "__init__.py")
    if not os.path.exists(init):
        open(init, "w").write('"""Web routes by area, included by main.py."""\n')
    header = f'"""Routes: {area}. Moved from main.py unchanged; shared helpers still live in main.py."""\n'
    imports = sorted(i for i in need_imports if i.startswith("import ")) + \
              sorted(i for i in need_imports if i.startswith("from "))
    body = header + "\n".join(imports) + ("\n" if imports else "") + "\nfrom fastapi import APIRouter\n\nimport main\n"
    body += "\nrouter = APIRouter()\n\n\n" + "\n\n".join(c.rstrip() + "\n" for c in chunks)
    open(out, "w").write(body)

    # cut from main.py (bottom-up) and include the router at the end
    for start, end in sorted(spans, reverse=True):
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        del lines[start:end]
    src = "".join(lines).rstrip() + "\n"
    inc = f"from routes import {area} as _routes_{area}  # noqa: E402\napp.include_router(_routes_{area}.router)\n"
    if inc not in src:
        src += "\n" + inc
    open(MAIN, "w").write(src)
    print(f"moved {len(names)} handlers to routes/{area}.py")


if __name__ == "__main__":
    main()
