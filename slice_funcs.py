#!/usr/bin/env python3
"""Slice a Python file by relationships between functions.

The file is parsed once into :class:`Index`.  Independent edge providers then
add relationships to the graph, so a new relationship kind can be implemented
and registered without changing traversal code.
"""
import argparse
import ast
import os
import re
import subprocess
import sys
from dataclasses import dataclass


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def diff_lines(diff_text, filename):
    wanted = os.path.normpath(filename).lstrip("./")
    changed = set()
    active = False
    in_hunk = False
    new_line = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            active = False
            in_hunk = False
        elif line.startswith("+++ "):
            path = line[4:].split("\t", 1)[0]
            if path.startswith("b/"):
                path = path[2:]
            path = os.path.normpath(path).lstrip("./")
            active = path == wanted or wanted.endswith("/" + path) or path.endswith("/" + wanted)
            in_hunk = False
        elif active:
            match = _HUNK.match(line)
            if match:
                new_line = int(match.group(1))
                in_hunk = True
            elif in_hunk:
                if line.startswith("\\"):
                    continue
                if line.startswith("+"):
                    changed.add(new_line)
                    new_line += 1
                elif line.startswith("-"):
                    continue
                else:
                    new_line += 1
    return changed


def _is_function(node):
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))


def _func_range(node):
    return min([node.lineno] + [d.lineno for d in node.decorator_list]), node.end_lineno


@dataclass(frozen=True)
class Symbol:
    name: str
    short_name: str
    kind: str
    node: object
    start: int
    end: int
    parent_function: str = None
    owner_class: str = None


@dataclass(frozen=True)
class Edge:
    caller: str
    callee: str
    kind: str
    lineno: int


class Index:
    """One-pass AST index shared by all edge providers."""

    def __init__(self, source, filename="<unknown>"):
        self.source = source
        self.filename = filename
        self.lines = source.splitlines()
        self.tree = ast.parse(source, filename=filename)
        self.symbols = {}
        self.indexed_bindings = []
        self.functions = {}
        self.classes = {}
        self.by_short = {}
        self.node_to_symbol = {}
        self.function_children = {}
        self.class_methods = {}
        self.import_names = set()
        self.module_bindings = {}
        self.module_alias_assignments = []
        self.module_dict_targets = {}
        self.function_for_node_cache = {}
        self._collect()
        self.aliases = self._resolve_aliases()
        self._collect_module_dict_targets()
        self.local_types = {}
        self.self_field_types = {}
        self._collect_types()

    def _collect(self):
        for stmt in self.tree.body:
            if isinstance(stmt, ast.Import):
                for item in stmt.names:
                    name = item.asname or item.name.split(".")[0]
                    self.import_names.add(name)
                    self.indexed_bindings.append(Symbol(name, name, "import", stmt, stmt.lineno, stmt.end_lineno))
            elif isinstance(stmt, ast.ImportFrom):
                for item in stmt.names:
                    name = item.asname or item.name
                    self.import_names.add(name)
                    self.indexed_bindings.append(Symbol(name, name, "import", stmt, stmt.lineno, stmt.end_lineno))
            self._collect_module_binding(stmt)
        def visit_body(body, class_path=(), func_path=()):
            for node in body:
                if isinstance(node, ast.ClassDef):
                    q = ".".join(class_path + (node.name,)) if not func_path else ".".join(func_path + ("<locals>", node.name))
                    sym = Symbol(q, node.name, "class", node, node.lineno, node.end_lineno,
                                 func_path[-1] if func_path else None, class_path[-1] if class_path else None)
                    self._add_symbol(sym)
                    self.classes[q] = sym
                    visit_body(node.body, class_path + (node.name,), func_path)
                elif _is_function(node):
                    if func_path:
                        q = ".".join(func_path + ("<locals>", node.name))
                    else:
                        q = ".".join(class_path + (node.name,))
                    owner = class_path[-1] if class_path else None
                    parent = func_path[-1] if func_path else None
                    start, end = _func_range(node)
                    sym = Symbol(q, node.name, "function", node, start, end, parent, owner)
                    self._add_symbol(sym)
                    self.functions[q] = sym
                    if parent:
                        self.function_children.setdefault(parent, []).append(q)
                    if owner and not func_path:
                        self.class_methods.setdefault(".".join(class_path), {}).setdefault(node.name, []).append(q)
                    visit_body(node.body, class_path, func_path + (q,))
        visit_body(self.tree.body)

    def _add_symbol(self, sym):
        self.symbols[sym.name] = sym
        self.by_short.setdefault(sym.short_name, []).append(sym.name)
        self.node_to_symbol[id(sym.node)] = sym.name

    def _collect_module_binding(self, stmt):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self.module_bindings[stmt.name] = stmt
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    self.module_bindings[target.id] = stmt
                    kind = "alias" if isinstance(stmt.value, ast.Name) else "assignment"
                    self.indexed_bindings.append(Symbol(target.id, target.id, kind, stmt, stmt.lineno, stmt.end_lineno))
                    if isinstance(stmt.value, ast.Name):
                        self.module_alias_assignments.append((stmt.lineno, target.id, stmt.value.id))
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            self.module_bindings[stmt.target.id] = stmt
            kind = "alias" if isinstance(stmt.value, ast.Name) else "assignment"
            self.indexed_bindings.append(Symbol(stmt.target.id, stmt.target.id, kind, stmt, stmt.lineno, stmt.end_lineno))
            if isinstance(stmt.value, ast.Name):
                self.module_alias_assignments.append((stmt.lineno, stmt.target.id, stmt.value.id))

    def _resolve_aliases(self):
        latest = {}
        for _, left, right in self.module_alias_assignments:
            latest[left] = right
        resolved = {}
        def one(name, trail=()):
            if name in resolved:
                return resolved[name]
            if name in trail:
                return None
            rhs = latest.get(name)
            if rhs is None:
                target = self.resolve_top_name(name)
            else:
                target = one(rhs, trail + (name,))
            resolved[name] = target
            return target
        for name in latest:
            one(name)
        return {k: v for k, v in resolved.items() if v in self.functions}

    def _collect_module_dict_targets(self):
        for stmt in self.tree.body:
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                continue
            value = stmt.value
            if not isinstance(value, ast.Dict):
                continue
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            funcs = set()
            for val in value.values:
                funcs.update(self.resolve_value_targets(val, None))
            for name in names:
                if funcs:
                    self.module_dict_targets[name] = funcs

    def _collect_types(self):
        for q, sym in self.functions.items():
            local = {}
            for node in self.scope_nodes(sym.node):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = node.value
                    cls = self.constructor_class(value)
                    if cls:
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for target in targets:
                            if isinstance(target, ast.Name):
                                local[target.id] = cls
                            if (sym.short_name == "__init__" and isinstance(target, ast.Attribute)
                                    and isinstance(target.value, ast.Name) and target.value.id == "self"):
                                self.self_field_types[(sym.owner_class, target.attr)] = cls
            self.local_types[q] = local

    def scope_nodes(self, func_node):
        """Yield nodes in one function scope, excluding nested defs/classes."""
        stack = list(reversed(func_node.body))
        while stack:
            node = stack.pop()
            yield node
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            stack.extend(reversed(list(ast.iter_child_nodes(node))))

    def resolve_top_name(self, name):
        if name in self.functions and self.functions[name].owner_class is None and self.functions[name].parent_function is None:
            return name
        choices = [q for q in self.by_short.get(name, ())
                   if q in self.functions and self.functions[q].owner_class is None and self.functions[q].parent_function is None]
        return choices[-1] if len(choices) == 1 else None

    def resolve_name(self, name, caller):
        if name in self.import_names:
            return None
        if caller:
            # Nearest nested function, then a top-level function.
            current = caller
            while current:
                for child in self.function_children.get(current, ()):
                    if self.functions[child].short_name == name:
                        return child
                sym = self.functions.get(current)
                current = sym.parent_function if sym else None
        if name in self.aliases:
            return self.aliases[name]
        return self.resolve_top_name(name)

    def class_symbol(self, short):
        choices = [q for q, sym in self.classes.items() if sym.short_name == short and sym.parent_function is None]
        return choices[-1] if len(choices) == 1 else None

    def method(self, class_name, method_name):
        if not class_name:
            return None
        class_q = self.class_symbol(class_name) or class_name
        choices = self.class_methods.get(class_q, {}).get(method_name, ())
        return choices[-1] if len(choices) == 1 else None

    def constructor_class(self, value):
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name):
            return None
        q = self.class_symbol(value.func.id)
        return q if q else None

    def constructor_target(self, class_q):
        init = self.class_methods.get(class_q, {}).get("__init__", ())
        return init[-1] if init else class_q

    def resolve_value_targets(self, node, caller):
        result = set()
        if isinstance(node, ast.Name):
            q = self.resolve_name(node.id, caller)
            if q in self.functions:
                result.add(q)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in ("self", "cls") and caller:
                owner = self.functions[caller].owner_class
                q = self.method(owner, node.attr)
                if q:
                    result.add(q)
            else:
                cq = self.class_symbol(node.value.id)
                q = self.method(cq, node.attr) if cq else None
                if q:
                    result.add(q)
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for elt in node.elts:
                result.update(self.resolve_value_targets(elt, caller))
        elif isinstance(node, ast.Dict):
            for value in node.values:
                result.update(self.resolve_value_targets(value, caller))
        return result

    def containing_function(self, lineno):
        choices = [s for s in self.functions.values() if s.start <= lineno <= s.end]
        return min(choices, key=lambda s: s.end - s.start) if choices else None

    def names(self, spec):
        requested = [x.strip() for x in spec.split(",") if x.strip()]
        found, missing = [], []
        for name in requested:
            matches = []
            if name in self.functions:
                matches = [name]
            else:
                matches = [q for q in self.by_short.get(name, ()) if q in self.functions]
            if matches:
                found.extend(matches)
            else:
                missing.append(name)
        return list(dict.fromkeys(found)), missing


class EdgeProvider:
    name = None
    def edges(self, index):
        raise NotImplementedError


class CallEdges(EdgeProvider):
    name = "call"
    def edges(self, index):
        out = []
        for caller, sym in index.functions.items():
            for node in index.scope_nodes(sym.node):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                target = None
                if isinstance(f, ast.Name):
                    if f.id not in index.aliases and not index.class_symbol(f.id):
                        target = index.resolve_name(f.id, caller)
                elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                    if f.value.id in ("self", "cls"):
                        target = index.method(sym.owner_class, f.attr)
                    else:
                        class_q = index.class_symbol(f.value.id)
                        if class_q:
                            target = index.method(class_q, f.attr)
                if target in index.functions:
                    out.append(Edge(caller, target, self.name, node.lineno))
        return out


class ConstructorEdges(EdgeProvider):
    name = "ctor"
    def edges(self, index):
        out = []
        for caller, sym in index.functions.items():
            for node in index.scope_nodes(sym.node):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    cq = index.class_symbol(node.func.id)
                    if cq:
                        out.append(Edge(caller, index.constructor_target(cq), self.name, node.lineno))
        return out


class TypedAttrEdges(EdgeProvider):
    name = "typed_attr"
    def edges(self, index):
        out = []
        for caller, sym in index.functions.items():
            for node in index.scope_nodes(sym.node):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                recv = node.func.value
                class_q = None
                if isinstance(recv, ast.Name) and recv.id not in ("self", "cls"):
                    class_q = index.local_types.get(caller, {}).get(recv.id)
                elif (isinstance(recv, ast.Attribute) and isinstance(recv.value, ast.Name)
                      and recv.value.id == "self"):
                    class_q = index.self_field_types.get((sym.owner_class, recv.attr))
                if class_q:
                    target = index.method(class_q, node.func.attr)
                    if target:
                        out.append(Edge(caller, target, self.name, node.lineno))
        return out


class RefEdges(EdgeProvider):
    name = "ref"
    def edges(self, index):
        out = []
        for caller, sym in index.functions.items():
            parent = {}
            nodes = list(index.scope_nodes(sym.node))
            for node in nodes:
                for child in ast.iter_child_nodes(node):
                    parent[id(child)] = node
            for node in nodes:
                if not isinstance(node, (ast.Name, ast.Attribute)) or not isinstance(getattr(node, "ctx", None), ast.Load):
                    continue
                p = parent.get(id(node))
                if isinstance(p, ast.Call) and p.func is node:
                    continue
                for target in index.resolve_value_targets(node, caller):
                    if target != caller:
                        out.append(Edge(caller, target, self.name, node.lineno))
        return out


class AliasEdges(EdgeProvider):
    name = "alias"
    def edges(self, index):
        out = []
        for caller, sym in index.functions.items():
            for node in index.scope_nodes(sym.node):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in index.aliases:
                    out.append(Edge(caller, index.aliases[node.func.id], self.name, node.lineno))
        return out


class DispatchEdges(EdgeProvider):
    name = "dispatch"
    def edges(self, index):
        out = []
        defaults_targets = set()
        # First collect every set_defaults(func=X) target.  A later args.func(...)
        # call is then a use of that dispatch table and is linked to those targets.
        for caller, sym in index.functions.items():
            for node in index.scope_nodes(sym.node):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "set_defaults":
                    for kw in node.keywords:
                        if kw.arg == "func":
                            defaults_targets.update(index.resolve_value_targets(kw.value, caller))
        for caller, sym in index.functions.items():
            loaded = set()
            for node in index.scope_nodes(sym.node):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    loaded.add(node.id)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "set_defaults":
                    for kw in node.keywords:
                        if kw.arg == "func":
                            for target in index.resolve_value_targets(kw.value, caller):
                                out.append(Edge(caller, target, self.name, node.lineno))
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "func":
                    for target in defaults_targets:
                        out.append(Edge(caller, target, self.name, node.lineno))
                if isinstance(node, ast.Dict):
                    for value in node.values:
                        for target in index.resolve_value_targets(value, caller):
                            out.append(Edge(caller, target, self.name, node.lineno))
            for name in loaded:
                for target in index.module_dict_targets.get(name, ()):
                    out.append(Edge(caller, target, self.name, sym.start))
        return out


class NestedEdges(EdgeProvider):
    name = "nested"
    def edges(self, index):
        out = []
        for outer, children in index.function_children.items():
            for child in children:
                out.append(Edge(outer, child, self.name, index.functions[child].start))
        return out


class DecoratorEdges(EdgeProvider):
    name = "decorator"
    def edges(self, index):
        out = []
        for target, sym in index.functions.items():
            caller_scope = sym.parent_function
            for dec in sym.node.decorator_list:
                base = dec.func if isinstance(dec, ast.Call) else dec
                for decorator in index.resolve_value_targets(base, caller_scope):
                    out.append(Edge(decorator, target, self.name, getattr(dec, "lineno", sym.start)))
        return out


EDGE_PROVIDERS = [CallEdges(), ConstructorEdges(), TypedAttrEdges(), RefEdges(), AliasEdges(), DispatchEdges(), NestedEdges(), DecoratorEdges()]


class ScoreProvider:
    """Independent relevance score component.

    Providers return a value in the inclusive 0..1 range.  ``context`` contains
    derived data shared by providers so expensive work is computed once.
    """
    name = None
    default_weight = 1.0

    def score(self, index, seeds, candidate, context=None):
        raise NotImplementedError


class DistanceScore(ScoreProvider):
    name = "distance"
    default_weight = 1.0

    def score(self, index, seeds, candidate, context=None):
        context = context or {}
        values = []
        for key in ("distance_down", "distance_up"):
            distance = context.get(key, {}).get(candidate)
            if distance is not None:
                values.append(1.0 / (1.0 + distance))
        if values:
            return max(values)
        distance = context.get("distance", {}).get(candidate)
        return 0.0 if distance is None else 1.0 / (1.0 + distance)


class EdgeKindScore(ScoreProvider):
    name = "edge_kind"
    default_weight = 1.0
    VALUES = {"call": 1.0, "ctor": 0.85, "typed_attr": 0.85,
              "dispatch": 0.65, "alias": 0.65, "nested": 0.65,
              "decorator": 0.65, "ref": 0.35}

    def score(self, index, seeds, candidate, context=None):
        context = context or {}
        kinds = context.get("path_edge_kinds", {}).get(candidate, ())
        return max([self.VALUES.get(kind, 0.5) for kind in kinds] or [0.0])


class SharedStateScore(ScoreProvider):
    name = "shared_state"
    default_weight = 1.0

    def score(self, index, seeds, candidate, context=None):
        context = context or {}
        state = context.get("state", {})
        seed_state = set()
        for seed in seeds:
            seed_state.update(state.get(seed, ()))
        if not seed_state:
            return 0.0
        current = state.get(candidate, set())
        return min(1.0, len(seed_state & current) / float(max(1, len(seed_state))))


class DiffTermsScore(ScoreProvider):
    name = "diff_terms"
    default_weight = 0.8

    def score(self, index, seeds, candidate, context=None):
        context = context or {}
        terms = context.get("diff_terms", set())
        if not terms or candidate not in index.functions:
            return 0.0
        present = _symbol_terms(index, index.functions[candidate])
        return min(1.0, len(terms & present) / float(max(1, len(terms))))


class CochangeScore(ScoreProvider):
    name = "cochange"
    default_weight = 0.6

    def score(self, index, seeds, candidate, context=None):
        context = context or {}
        counts = context.get("cochange", {})
        maximum = max(counts.values(), default=0)
        return 0.0 if maximum <= 0 else min(1.0, counts.get(candidate, 0) / float(maximum))


SCORE_PROVIDERS = [DistanceScore(), EdgeKindScore(), SharedStateScore(), DiffTermsScore(), CochangeScore()]


def _clamp_score(value):
    return max(0.0, min(1.0, float(value)))


def _parse_weights(text, providers=None):
    providers = SCORE_PROVIDERS if providers is None else providers
    weights = {provider.name: float(provider.default_weight) for provider in providers}
    if not text:
        return weights
    available = set(weights)
    for part in text.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError("invalid weight: {}".format(part))
        name, value = (x.strip() for x in part.split("=", 1))
        if name not in available:
            raise ValueError("unknown score kind: {}".format(name))
        number = float(value)
        if number < 0:
            raise ValueError("weight must be >= 0: {}".format(name))
        weights[name] = number
    return weights


def _symbol_terms(index, sym):
    terms = set()
    for node in ast.walk(sym.node):
        if isinstance(node, ast.Name):
            terms.add(node.id)
        elif isinstance(node, ast.Attribute):
            terms.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            terms.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value))
            if node.value:
                terms.add(node.value)
    return terms


def _diff_terms(diff_text):
    terms = set()
    if not diff_text:
        return terms
    for line in diff_text.splitlines():
        if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
            continue
        text = line[1:]
        try:
            tree = ast.parse(text.strip())
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    terms.add(node.id)
                elif isinstance(node, ast.Attribute):
                    terms.add(node.attr)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    terms.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value))
                    if node.value:
                        terms.add(node.value)
        terms.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))
        for match in re.findall(r"(['\"])(.*?)\1", text):
            if match[1]:
                terms.add(match[1])
    return terms


def _state_keys(index, sym):
    keys = set()
    locals_defined = {arg.arg for arg in sym.node.args.args}
    locals_defined.update(arg.arg for arg in getattr(sym.node.args, "posonlyargs", ()))
    locals_defined.update(arg.arg for arg in sym.node.args.kwonlyargs)
    for node in index.scope_nodes(sym.node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Param)):
            locals_defined.add(node.id)
    for node in index.scope_nodes(sym.node):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "self"):
            keys.add("self." + node.attr)
        elif isinstance(node, ast.Name) and node.id not in locals_defined and node.id not in index.import_names:
            if node.id in index.module_bindings or node.id.isupper():
                keys.add("module." + node.id)
    return keys


def _all_distances(seeds, edges, direction):
    down, up = _adjacency(edges)
    result = {seed: 0 for seed in seeds}
    path_kinds = {seed: set() for seed in seeds}
    adjs = []
    if direction in ("down", "both"):
        adjs.append((down, False))
    if direction in ("up", "both"):
        adjs.append((up, True))
    edge_map = {}
    for edge in edges:
        edge_map.setdefault((edge.caller, edge.callee), set()).add(edge.kind)
    for adj, reverse in adjs:
        seen = set(seeds)
        frontier = set(seeds)
        level = 0
        while frontier:
            level += 1
            nxt = set()
            for item in frontier:
                for other in adj.get(item, ()):
                    if other in seen:
                        continue
                    nxt.add(other)
                    pair = (other, item) if reverse else (item, other)
                    path_kinds.setdefault(other, set()).update(edge_map.get(pair, ()))
            if not nxt:
                break
            for item in nxt:
                result[item] = min(result.get(item, level), level)
            seen.update(nxt)
            frontier = nxt
    return result, path_kinds


def _git_cochange(index, seeds, git_root, limit):
    if not git_root or not seeds or not os.path.exists(git_root):
        return {}
    try:
        rel = os.path.relpath(os.path.abspath(index.filename), os.path.abspath(git_root))
        proc = subprocess.run(["git", "-C", git_root, "log", "--format=%H", "-n", str(limit), "--", rel],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              universal_newlines=True, check=False)
        if proc.returncode != 0:
            return {}
        commits = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except (OSError, ValueError):
        return {}
    touched = {name: set() for name in index.functions}
    for commit in commits:
        try:
            shown = subprocess.run(["git", "-C", git_root, "show", "--format=", "--unified=0", commit, "--", rel],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   universal_newlines=True, check=False)
        except OSError:
            return {}
        if shown.returncode != 0:
            continue
        lines = diff_lines(shown.stdout, rel)
        for lineno in lines:
            sym = index.containing_function(lineno)
            if sym:
                touched[sym.name].add(commit)
    seed_commits = set()
    for seed in seeds:
        seed_commits.update(touched.get(seed, ()))
    return {name: len(seed_commits & commits_set) for name, commits_set in touched.items()}


def _score_context(index, seeds, edges, direction, diff_text, git_root, git_commits):
    distances, path_kinds = _all_distances(seeds, edges, direction)
    distance_down, _ = _all_distances(seeds, edges, "down") if direction in ("down", "both") else ({}, {})
    distance_up, _ = _all_distances(seeds, edges, "up") if direction in ("up", "both") else ({}, {})
    return {
        "distance": distances,
        "distance_down": distance_down,
        "distance_up": distance_up,
        "path_edge_kinds": path_kinds,
        "state": {name: _state_keys(index, sym) for name, sym in index.functions.items()},
        "diff_terms": _diff_terms(diff_text),
        "cochange": _git_cochange(index, seeds, git_root, git_commits),
    }


def score_symbols(index, seeds, candidates, context, weights=None, providers=None):
    providers = SCORE_PROVIDERS if providers is None else providers
    weights = _parse_weights(None, providers) if weights is None else weights
    result = {}
    for candidate in candidates:
        parts = {}
        numerator = denominator = 0.0
        for provider in providers:
            value = _clamp_score(provider.score(index, seeds, candidate, context))
            parts[provider.name] = value
            weight = weights.get(provider.name, provider.default_weight)
            numerator += value * weight
            denominator += weight
        result[candidate] = (0.0 if denominator == 0 else numerator / denominator, parts)
    return result


def build_edges(index, kinds=None, providers=None):
    providers = EDGE_PROVIDERS if providers is None else providers
    requested = set(kinds) if kinds is not None else {p.name for p in providers}
    result = set()
    for provider in providers:
        if provider.name not in requested:
            continue
        for edge in provider.edges(index):
            if edge.caller in index.functions and (edge.callee in index.functions or edge.callee in index.classes):
                result.add(edge)
    return result


def _merge(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _code_lines(source):
    return sum(1 for line in source.splitlines() if line.strip() and not line.lstrip().startswith("#"))


def _node_metric(sym, source, metric):
    if metric == "funcs":
        return 1
    if metric == "lines":
        return sym.end - sym.start + 1
    return sum(1 for line in source.splitlines()[sym.start - 1:sym.end]
               if line.strip() and not line.lstrip().startswith("#"))


def _adjacency(edges):
    down, up = {}, {}
    for e in edges:
        down.setdefault(e.caller, set()).add(e.callee)
        up.setdefault(e.callee, set()).add(e.caller)
    return down, up


def traverse(seeds, edges, depth, callers, direction):
    down, up = _adjacency(edges)
    distances = {s: 0 for s in seeds}
    sides = []
    if direction in ("down", "both"):
        sides.append((down, max(0, depth)))
    if direction in ("up", "both"):
        sides.append((up, max(0, callers)))
    # Each side gets an independent visited set.  Only final results are merged.
    for adj, limit in sides:
        seen = set(seeds)
        frontier = set(seeds)
        for level in range(1, limit + 1):
            nxt = set()
            for item in frontier:
                nxt.update(adj.get(item, ()))
            nxt -= seen
            if not nxt:
                break
            for item in nxt:
                distances[item] = min(distances.get(item, level), level)
            seen.update(nxt)
            frontier = nxt
    return distances


def _class_line_ranges(cls):
    node = cls.node if isinstance(cls, Symbol) else cls
    ranges = [(min([node.lineno] + [d.lineno for d in node.decorator_list]), node.lineno)]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, (ast.Str, ast.Constant))
            and isinstance(getattr(node.body[0].value, "value", None), str)):
        ranges.append((node.body[0].lineno, node.body[0].end_lineno))
    for member in node.body:
        if isinstance(member, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            ranges.append((member.lineno, member.end_lineno))
    return ranges


def _support_ranges(index, selected, include_function_refs=True):
    selected_syms = [index.functions[q] for q in selected if q in index.functions]
    names, attrs = set(), set()
    for sym in selected_syms:
        for node in index.scope_nodes(sym.node):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                names.add(node.id)
            elif (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                  and isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")):
                attrs.add(node.attr)
    ranges = []
    for name in names:
        stmt = index.module_bindings.get(name)
        if stmt is None:
            continue
        if isinstance(stmt, ast.ClassDef):
            cq = index.class_symbol(stmt.name)
            if cq:
                ranges.extend(_class_line_ranges(index.classes[cq]))
        elif _is_function(stmt):
            if include_function_refs:
                # Historical diff slicing includes directly referenced top-level
                # helpers even at depth 0.
                start, end = _func_range(stmt)
                ranges.append((start, end))
        else:
            ranges.append((stmt.lineno, stmt.end_lineno))
    for sym in selected_syms:
        if not sym.owner_class:
            continue
        cq = index.class_symbol(sym.owner_class)
        if cq:
            ranges.extend(_class_line_ranges(index.classes[cq]))
        # Preserve only self.x assignments used by selected code.
        class_node = index.classes[cq].node if cq else None
        if class_node:
            for member in class_node.body:
                if not _is_function(member):
                    continue
                for node in ast.walk(member):
                    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for target in targets:
                            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                                    and target.value.id == "self" and target.attr in attrs):
                                ranges.append((node.lineno, node.end_lineno))
    return ranges


def _preamble_ranges(index):
    defs = [n.lineno for n in index.tree.body if _is_function(n) or isinstance(n, ast.ClassDef)]
    end = min(defs) - 1 if defs else len(index.lines)
    ranges = [(1, end)] if end >= 1 else []
    ranges.extend((n.lineno, n.end_lineno) for n in index.tree.body if isinstance(n, (ast.Import, ast.ImportFrom)))
    return ranges


def _seed_symbols(index, diff_text, names):
    seeds = []
    changed_module_ranges = []
    if names:
        found, missing = index.names(names)
        seeds.extend(found)
    else:
        missing = []
    if diff_text is not None:
        for lineno in diff_lines(diff_text, index.filename):
            sym = index.containing_function(lineno)
            if sym:
                seeds.append(sym.name)
            else:
                stmt = next((n for n in index.tree.body if n.lineno <= lineno <= n.end_lineno), None)
                if stmt:
                    changed_module_ranges.append((stmt.lineno, stmt.end_lineno))
    return list(dict.fromkeys(seeds)), missing, changed_module_ranges


def select_ranges(index, seeds, changed_module_ranges, edges, depth=1, callers=1,
                  max_caller_lines=400, max_lines=0, direction="both", support_context=True):
    distances = traverse(seeds, edges, depth, callers, direction)
    mandatory_ranges = list(changed_module_ranges)
    for name in seeds:
        if name in index.functions:
            mandatory_ranges.append((index.functions[name].start, index.functions[name].end))
    traversal_ranges = []
    down, up = _adjacency(edges)
    # Function ranges from traversal; large callers use call-site windows.
    for name, level in distances.items():
        if name in seeds:
            continue
        if name in index.classes:
            traversal_ranges.extend(_class_line_ranges(index.classes[name]))
            continue
        if name not in index.functions:
            continue
        sym = index.functions[name]
        is_up = direction == "up" or (direction == "both" and any(name in _reachable_side({s}, up, callers) for s in seeds))
        if is_up and sym.end - sym.start + 1 > max_caller_lines:
            call_lines = sorted(e.lineno for e in edges if e.caller == name and e.callee in distances)
            if not call_lines:
                call_lines = [sym.start]
            traversal_ranges.extend((max(sym.start, ln - 20), min(sym.end, ln + 20)) for ln in call_lines)
        else:
            traversal_ranges.append((sym.start, sym.end))
    selected = {q for q in distances if q in index.functions}
    support_ranges = _support_ranges(index, selected, include_function_refs=True) if support_context else []
    preamble = _preamble_ranges(index)
    all_ranges = mandatory_ranges + traversal_ranges + support_ranges + preamble
    if max_lines > 0:
        kept = list(mandatory_ranges)
        used = set(line for a, b in kept for line in range(a, b + 1))
        for r in support_ranges + preamble + traversal_ranges:
            span = set(range(r[0], r[1] + 1))
            if len(used | span) <= max_lines:
                kept.append(r); used.update(span)
            else:
                print("slice_funcs: omitted {}-{} (max-lines)".format(*r), file=sys.stderr)
        all_ranges = kept
    return _merge(all_ranges), distances



def _function_head_ranges(sym):
    node = sym.node
    ranges = [(node.lineno, node.lineno)]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, (ast.Str, ast.Constant))
            and isinstance(getattr(node.body[0].value, "value", None), str)):
        ranges.append((node.body[0].lineno, node.body[0].end_lineno))
    return ranges


def _relevance_symbol_ranges(index, name, edges, distances, large_lines, context_lines):
    if name in index.classes:
        return _class_line_ranges(index.classes[name]), False
    sym = index.functions[name]
    if sym.end - sym.start + 1 <= large_lines:
        return [(sym.start, sym.end)], False
    ranges = _function_head_ranges(sym)
    my_distance = distances.get(name, 10 ** 9)
    link_lines = []
    for edge in edges:
        if edge.caller != name:
            continue
        if distances.get(edge.callee, 10 ** 9) < my_distance:
            link_lines.append(edge.lineno)
    for lineno in sorted(set(link_lines)):
        ranges.append((max(sym.start, lineno - context_lines), min(sym.end, lineno + context_lines)))
    return _merge(ranges), True


def _class_header_ranges(cls):
    node = cls.node if isinstance(cls, Symbol) else cls
    ranges = [(min([node.lineno] + [d.lineno for d in node.decorator_list]), node.lineno)]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, (ast.Str, ast.Constant))
            and isinstance(getattr(node.body[0].value, "value", None), str)):
        ranges.append((node.body[0].lineno, node.body[0].end_lineno))
    return ranges


def _relevance_support_ranges(index, selected):
    """Only definitions actually referenced by selected function symbols."""
    names, attrs = set(), set()
    for name in selected:
        sym = index.functions.get(name)
        if not sym:
            continue
        for node in index.scope_nodes(sym.node):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                names.add(node.id)
            elif (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                  and isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")):
                attrs.add(node.attr)
    ranges = []
    for stmt in index.tree.body:
        if isinstance(stmt, ast.Import):
            used = any((item.asname or item.name.split(".")[0]) in names for item in stmt.names)
            if used:
                ranges.append((stmt.lineno, stmt.end_lineno))
        elif isinstance(stmt, ast.ImportFrom):
            used = any((item.asname or item.name) in names for item in stmt.names)
            if used:
                ranges.append((stmt.lineno, stmt.end_lineno))
    for name in names:
        stmt = index.module_bindings.get(name)
        if stmt is None or _is_function(stmt):
            continue
        if isinstance(stmt, ast.ClassDef):
            cq = index.class_symbol(stmt.name)
            if cq:
                ranges.extend(_class_header_ranges(index.classes[cq]))
        else:
            ranges.append((stmt.lineno, stmt.end_lineno))
    for symbol_name in selected:
        sym = index.functions.get(symbol_name)
        if not sym or not sym.owner_class:
            continue
        cq = index.class_symbol(sym.owner_class)
        if cq:
            ranges.extend(_class_header_ranges(index.classes[cq]))
            class_node = index.classes[cq].node
            for member in class_node.body:
                if not _is_function(member):
                    continue
                for node in ast.walk(member):
                    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for target in targets:
                            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                                    and target.value.id == "self" and target.attr in attrs):
                                ranges.append((node.lineno, node.end_lineno))
    return _merge(ranges)


def _range_lines(ranges):
    return {line for start, end in ranges for line in range(start, end + 1)}


def _parse_budget(text, total_lines):
    if text is None:
        raise ValueError("--budget is required with --select relevance")
    value = text.strip()
    if value.endswith("%"):
        percent = float(value[:-1])
        if percent <= 0:
            raise ValueError("budget percentage must be > 0")
        return max(1, int(total_lines * percent / 100.0))
    lines = int(value)
    if lines <= 0:
        raise ValueError("budget lines must be > 0")
    return lines


def select_relevance(index, seeds, changed_module_ranges, edges, budget, direction="both",
                     diff_text=None, weights=None, providers=None, git_root=None, git_commits=200,
                     large_lines=150, context_lines=15, support_context=True, explain=False):
    providers = SCORE_PROVIDERS if providers is None else providers
    context = _score_context(index, seeds, edges, direction, diff_text, git_root, git_commits)
    candidates = set(context["distance"])
    candidates.update(seed for seed in seeds if seed in index.functions or seed in index.classes)
    scores = score_symbols(index, seeds, candidates, context, weights, providers)

    selected = set(seed for seed in seeds if seed in candidates)
    symbol_ranges = {}
    trimmed = {}
    for name in candidates:
        if name in index.functions or name in index.classes:
            if name in seeds and name in index.functions:
                sym = index.functions[name]
                symbol_ranges[name], trimmed[name] = [(sym.start, sym.end)], False
            else:
                symbol_ranges[name], trimmed[name] = _relevance_symbol_ranges(
                    index, name, edges, context["distance"], large_lines, context_lines)

    mandatory = list(changed_module_ranges)
    for seed in selected:
        mandatory.extend(symbol_ranges.get(seed, ()))
    kept = list(mandatory)
    used = _range_lines(kept)

    if support_context and selected:
        for item in _relevance_support_ranges(index, selected):
            span = _range_lines([item])
            if len(used | span) <= budget:
                kept.append(item)
                used.update(span)

    ranked = sorted((name for name in candidates if name not in selected and name in symbol_ranges),
                    key=lambda name: (-scores[name][0], context["distance"].get(name, 10 ** 9), name))
    for name in ranked:
        tentative_selected = set(selected)
        tentative_selected.add(name)
        additions = list(symbol_ranges[name])
        if support_context:
            support = _relevance_support_ranges(index, tentative_selected)
            additions.extend(r for r in support if not _range_lines([r]).issubset(used))
        span = _range_lines(additions)
        if len(used | span) <= budget:
            selected.add(name)
            kept.extend(additions)
            used.update(span)

    if explain:
        for name in sorted(selected, key=lambda item: (-scores.get(item, (0.0, {}))[0], item)):
            total, parts = scores.get(name, (0.0, {}))
            detail = ",".join("{}={:.3f}".format(provider.name, parts.get(provider.name, 0.0))
                              for provider in providers)
            print("slice_funcs: explain {} score={:.3f} {} trimmed={}".format(
                name, total, detail, "yes" if trimmed.get(name, False) else "no"), file=sys.stderr)
    return _merge(kept), scores, selected



def _reachable_side(seeds, adj, limit):
    seen = set(seeds); frontier = set(seeds)
    for _ in range(max(0, limit)):
        nxt = set()
        for item in frontier:
            nxt.update(adj.get(item, ()))
        nxt -= seen
        seen.update(nxt); frontier = nxt
        if not frontier:
            break
    return seen


def _parse_kinds(text, providers=None):
    providers = EDGE_PROVIDERS if providers is None else providers
    available = {p.name for p in providers}
    if not text:
        return available
    kinds = {x.strip() for x in text.split(",") if x.strip()}
    unknown = kinds - available
    if unknown:
        raise ValueError("unknown edge kind(s): {}".format(",".join(sorted(unknown))))
    return kinds


def _normalize_trace_name(name, index):
    if name in ("<module>", "<lambda>") or "<listcomp>" in name or "<dictcomp>" in name or "<setcomp>" in name or "<genexpr>" in name:
        return None
    name = name.replace(".<locals>.", ".<locals>.")
    if name in index.functions:
        return name
    # Python co_qualname and our nested naming intentionally use the same form.
    choices = [q for q in index.functions if q.endswith("." + name) or q == name]
    if len(choices) == 1:
        return choices[0]
    short = name.split(".")[-1]
    matches = [q for q in index.by_short.get(short, ()) if q in index.functions]
    return matches[0] if len(matches) == 1 else name


def compare_trace(index, edges, trace_text):
    actual = set()
    for line in trace_text.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2:
            continue
        a = _normalize_trace_name(parts[0], index)
        b = _normalize_trace_name(parts[1], index)
        if a and b and a in index.functions and b in index.functions:
            actual.add((a, b))
    static = {(e.caller, e.callee) for e in edges if e.caller in index.functions and e.callee in index.functions}
    hit = actual & static
    missing = sorted(actual - static)
    static_only = static - actual
    ratio = 100.0 * len(hit) / max(1, len(actual))
    rows = [
        "trace_pairs\t{}".format(len(actual)),
        "matched_pairs\t{}".format(len(hit)),
        "matched_ratio\t{:.2f}%".format(ratio),
        "static_only_pairs\t{}".format(len(static_only)),
        "missing_trace_pairs\t{}".format(len(missing)),
    ]
    rows.extend("missing\t{}\t{}".format(a, b) for a, b in missing)
    return "\n".join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--diff")
    parser.add_argument("--names")
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--callers", type=int, default=1)
    parser.add_argument("--max-caller-lines", type=int, default=400)
    parser.add_argument("--max-lines", type=int, default=0)
    parser.add_argument("--toc", action="store_true")
    parser.add_argument("--attr-calls", action="store_true")
    parser.add_argument("--layers", action="store_true")
    parser.add_argument("--direction", choices=("down", "up", "both"), default="both")
    parser.add_argument("--metric", choices=("funcs", "code", "lines"), default="code")
    parser.add_argument("--edges", action="store_true")
    parser.add_argument("--path-prefix")
    parser.add_argument("--kinds")
    parser.add_argument("--compare-trace")
    parser.add_argument("--select", choices=("legacy", "relevance"), default="legacy")
    parser.add_argument("--budget")
    parser.add_argument("--weights")
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--git-root")
    parser.add_argument("--git-commits", type=int, default=200)
    parser.add_argument("--large-symbol-lines", type=int, default=150)
    parser.add_argument("--context-lines", type=int, default=15)
    args = parser.parse_args(argv)
    if args.diff is None and args.names is None and not args.edges and not args.toc and not args.compare_trace:
        parser.error("--diff or --names is required (unless using --edges, --toc, or --compare-trace)")
    try:
        with open(args.file, encoding="utf-8") as handle:
            source = handle.read()
        index = Index(source, args.file)
        kinds = _parse_kinds(args.kinds)
        # Compatibility: --attr-calls is retained as a spelling for enabling
        # typed_attr when an explicit kind subset was requested.
        if args.attr_calls:
            kinds.add("typed_attr")
        edges = build_edges(index, kinds)
        diff_text = None
        if args.diff is not None:
            with open(args.diff, encoding="utf-8") as handle:
                diff_text = handle.read()
        seeds, missing, module_ranges = _seed_symbols(index, diff_text, args.names)
        weights = _parse_weights(args.weights)
        budget = _parse_budget(args.budget, len(index.lines)) if args.select == "relevance" else None
        if args.git_commits < 0 or args.large_symbol_lines <= 0 or args.context_lines < 0:
            raise ValueError("invalid relevance limit")
        for name in missing:
            print("slice_funcs: name not found: {}".format(name), file=sys.stderr)
    except (SyntaxError, UnicodeError, OSError, ValueError) as exc:
        print("slice_funcs: {}".format(exc), file=sys.stderr)
        return 2

    if args.compare_trace:
        try:
            with open(args.compare_trace, encoding="utf-8") as handle:
                trace_text = handle.read()
        except OSError as exc:
            print("slice_funcs: {}".format(exc), file=sys.stderr)
            return 2
        print(compare_trace(index, edges, trace_text))
        return 0

    if args.edges:
        rows = sorted({(e.caller, e.callee, e.kind) for e in edges})
        for caller, callee, kind in rows:
            print("{}\t{}\t{}".format(caller, callee, kind))
        return 0

    if args.toc:
        entries = [(sym.start, sym.name) for sym in index.symbols.values()]
        print("\n".join("{}: {}".format(line, name) for line, name in sorted(entries)))
        return 0

    if args.select == "relevance" and not args.layers:
        ranges, _, _ = select_relevance(
            index, seeds, module_ranges, edges, budget, args.direction, diff_text, weights,
            git_root=args.git_root, git_commits=args.git_commits,
            large_lines=args.large_symbol_lines, context_lines=args.context_lines,
            support_context=(args.diff is not None), explain=args.explain)
        distances = {}
    else:
        ranges, distances = select_ranges(index, seeds, module_ranges, edges, args.depth,
                                          args.callers, args.max_caller_lines, args.max_lines,
                                          args.direction, support_context=(args.diff is not None))
    if args.layers:
        # Layer reports describe shortest distance to saturation, independent of
        # the slice depth limits.  Each direction is still traversed separately.
        saturation = max(1, len(index.functions) + len(index.classes))
        distances = traverse(seeds, edges, saturation, saturation, args.direction)
        total = len(index.functions) if args.metric == "funcs" else (_code_lines(source) if args.metric == "code" else len(index.lines))
        print("層 関数数 行数 割合%")
        for level in range(max(distances.values(), default=0) + 1):
            chosen = [index.functions[q] for q, d in distances.items() if d == level and q in index.functions]
            val = len(chosen) if args.metric == "funcs" else sum(_node_metric(s, source, args.metric) for s in chosen)
            print("{} {} {} {:.2f}%".format(level, len(chosen), val, 100.0 * val / max(1, total)))
        print("飽和: {}層".format(max(distances.values(), default=0)))
        return 0

    path = args.path_prefix if args.path_prefix is not None else args.file
    print(",".join("{}:{}-{}".format(path, start, end) for start, end in ranges))
    return 0


if __name__ == "__main__":
    sys.exit(main())
