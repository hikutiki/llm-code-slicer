import contextlib
import io
import os
import subprocess
import tempfile
import unittest

import slice_funcs


class SliceFuncsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source_path = os.path.join(self.temp.name, "sample.py")
        self.diff_path = os.path.join(self.temp.name, "change.diff")

    def run_cli(self, source, diff=None, depth=1, callers=None, max_caller_lines=None,
                names=None, max_lines=None, toc=False, attr_calls=False, layers=False,
                direction="both", metric="code", edges=False, kinds=None, compare_trace=None,
                select=None, budget=None, weights=None, explain=False, git_root=None,
                git_commits=None, large_symbol_lines=None, context_lines=None):
        with open(self.source_path, "w", encoding="utf-8") as handle:
            handle.write(source)
        if diff is not None:
            with open(self.diff_path, "w", encoding="utf-8") as handle:
                handle.write(diff)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            args = ["--file", self.source_path, "--depth", str(depth)]
            if diff is not None:
                args.extend(["--diff", self.diff_path])
            if names is not None:
                args.extend(["--names", names])
            if callers is not None:
                args.extend(["--callers", str(callers)])
            if max_caller_lines is not None:
                args.extend(["--max-caller-lines", str(max_caller_lines)])
            if max_lines is not None:
                args.extend(["--max-lines", str(max_lines)])
            if toc:
                args.append("--toc")
            if attr_calls:
                args.append("--attr-calls")
            if layers:
                args.append("--layers")
            args.extend(["--direction", direction, "--metric", metric])
            if edges: args.append("--edges")
            if kinds is not None:
                args.extend(["--kinds", kinds])
            if compare_trace is not None:
                trace_path = os.path.join(self.temp.name, "trace.txt")
                with open(trace_path, "w", encoding="utf-8") as handle:
                    handle.write(compare_trace)
                args.extend(["--compare-trace", trace_path])
            if select is not None:
                args.extend(["--select", select])
            if budget is not None:
                args.extend(["--budget", str(budget)])
            if weights is not None:
                args.extend(["--weights", weights])
            if explain:
                args.append("--explain")
            if git_root is not None:
                args.extend(["--git-root", git_root])
            if git_commits is not None:
                args.extend(["--git-commits", str(git_commits)])
            if large_symbol_lines is not None:
                args.extend(["--large-symbol-lines", str(large_symbol_lines)])
            if context_lines is not None:
                args.extend(["--context-lines", str(context_lines)])
            code = slice_funcs.main(args)
        return code, stdout.getvalue().strip(), stderr.getvalue()

    def selected_lines(self, output):
        lines = set()
        for part in output.split(","):
            _, span = part.rsplit(":", 1)
            start, end = map(int, span.split("-"))
            lines.update(range(start, end + 1))
        return lines

    def test_method_change_selects_method_not_class(self):
        source = "import os\nCONST = 1\n\nclass C:\n    def first(self):\n        return 1\n\n    def changed(self):\n        return 2\n"
        diff = "--- a/sample.py\n+++ b/sample.py\n@@ -8 +8 @@\n-        return 2\n+        return 3\n"
        code, output, _ = self.run_cli(source, diff)
        self.assertEqual(code, 0)
        self.assertEqual(output.split(","), [self.source_path + ":1-4", self.source_path + ":8-9"])

    def test_called_function_added_only_at_depth_one(self):
        source = "import os\nCONST = 1\n\ndef helper():\n    return 1\n\ndef main():\n    return helper()\n"
        diff = "--- a/sample.py\n+++ b/sample.py\n@@ -8 +8 @@\n-    return helper()\n+    return helper() + 1\n"
        code1, out1, _ = self.run_cli(source, diff, 1)
        code0, out0, _ = self.run_cli(source, diff, 0)
        self.assertEqual(code1, 0)
        self.assertEqual(out1.split(","), [self.source_path + ":1-5", self.source_path + ":7-8"])
        self.assertEqual(code0, 0)
        self.assertEqual(out0.split(","), [self.source_path + ":1-5", self.source_path + ":7-8"])

    def test_adjacent_ranges_merge(self):
        source = "x = 1\ndef a():\n    pass\ndef b():\n    pass\n"
        diff = "--- a/sample.py\n+++ b/sample.py\n@@ -2 +2 @@\n-def a():\n+def a():\n@@ -4 +4 @@\n-def b():\n+def b():\n"
        code, output, _ = self.run_cli(source, diff, 0)
        self.assertEqual(code, 0)
        self.assertEqual(output, self.source_path + ":1-5")

    def test_module_statement_change(self):
        source = "import os\nCONST = 1\n\nx = 2\ny = 3\n"
        diff = "--- a/sample.py\n+++ b/sample.py\n@@ -4 +4 @@\n-x = 2\n+x = 4\n"
        code, output, _ = self.run_cli(source, diff)
        self.assertEqual(code, 0)
        self.assertEqual(output, self.source_path + ":1-5")

    def test_caller_included_by_default(self):
        source = "def changed():\n    return 1\n\ndef caller():\n    return changed()\n"
        diff = "--- a/sample.py\n+++ b/sample.py\n@@ -2 +2 @@\n-    return 1\n+    return 2\n"
        code, output, _ = self.run_cli(source, diff, depth=0)
        self.assertEqual(code, 0)
        self.assertEqual(output.split(","), [self.source_path + ":1-2",
                                               self.source_path + ":4-5"])

    def test_callers_zero_excludes_caller(self):
        source = "def changed():\n    return 1\n\ndef caller():\n    return changed()\n"
        diff = "--- a/sample.py\n+++ b/sample.py\n@@ -2 +2 @@\n-    return 1\n+    return 2\n"
        code, output, _ = self.run_cli(source, diff, depth=0, callers=0)
        self.assertEqual(code, 0)
        self.assertEqual(output.split(","), [self.source_path + ":1-2"])

    def test_large_caller_includes_only_context_window(self):
        body = ["def changed():", "    return 1", "", "def caller():"]
        body.extend("    value_{0} = {0}".format(i) for i in range(245))
        body.append("    return changed()")
        body.extend("    value_{0} = {0}".format(i) for i in range(245, 490))
        source = "\n".join(body) + "\n"
        diff = "--- a/sample.py\n+++ b/sample.py\n@@ -2 +2 @@\n-    return 1\n+    return 2\n"
        code, output, _ = self.run_cli(source, diff, depth=0, max_caller_lines=100)
        self.assertEqual(code, 0)
        ranges = output.split(",")
        self.assertIn(self.source_path + ":230-270", ranges)
        self.assertNotIn(self.source_path + ":4-495", ranges)

    def test_empty_diff_only_preamble(self):
        source = "import os\nCONST = 1\ndef f():\n    pass\n"
        code, output, _ = self.run_cli(source, "")
        self.assertEqual(code, 0)
        self.assertEqual(output, self.source_path + ":1-2")

    def test_syntax_error_returns_two(self):
        code, output, error = self.run_cli("def broken(:\n", "")
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("invalid syntax", error)

    def test_names_selects_all_methods_by_bare_name(self):
        source = "CONST = 1\nclass A:\n    def run(self):\n        pass\nclass B:\n    async def run(self):\n        pass\n"
        code, output, _ = self.run_cli(source, names="run")
        self.assertEqual(code, 0)
        self.assertEqual(output.split(","), [self.source_path + ":1-1",
                                               self.source_path + ":3-4",
                                               self.source_path + ":6-7"])

    def test_names_selects_qualified_method(self):
        source = "CONST = 1\nclass A:\n    def run(self):\n        pass\nclass B:\n    def run(self):\n        pass\n"
        code, output, _ = self.run_cli(source, names="A.run")
        self.assertEqual(code, 0)
        self.assertEqual(output.split(","), [self.source_path + ":1-1",
                                               self.source_path + ":3-4"])

    def test_names_missing_reported_to_stderr(self):
        source = "def present():\n    pass\n"
        code, _, error = self.run_cli(source, names="absent")
        self.assertEqual(code, 0)
        self.assertIn("absent", error)

    def test_names_works_without_diff(self):
        source = "CONST = 1\ndef selected():\n    pass\n"
        code, output, _ = self.run_cli(source, names="selected")
        self.assertEqual(code, 0)
        self.assertEqual(output, self.source_path + ":1-3")

    def test_late_module_constant_included(self):
        source = "def f():\n    return LATE\n\nLATE = 42\n"
        diff = "--- a/sample.py\n+++ b/sample.py\n@@ -2 +2 @@\n-    return LATE\n+    return LATE + 1\n"
        _, output, _ = self.run_cli(source, diff, depth=0, callers=0)
        self.assertIn(self.source_path + ":4-4", output.split(","))

    def test_self_assignment_only_and_class_header_lines(self):
        source = "class C:\n    \"\"\"doc\"\"\"\n    BASE = 3\n    def __init__(self):\n        self.x = 1\n        self.y = 2\n    def f(self):\n        return self.x\n    def unrelated(self):\n        return 9\n"
        diff = "--- a/sample.py\n+++ b/sample.py\n@@ -7 +7 @@\n-        return self.x\n+        return self.x + 1\n"
        _, output, _ = self.run_cli(source, diff, depth=0, callers=0)
        lines = set()
        for part in output.split(","):
            _, span = part.rsplit(":", 1)
            a, b = map(int, span.split("-"))
            lines.update(range(a, b + 1))
        self.assertTrue({1, 2, 3, 5, 8}.issubset(lines))
        self.assertNotIn(6, lines)
        self.assertNotIn(9, lines)

    def test_imports_after_definition_are_included(self):
        source = "def f():\n    return 1\n\nimport os\n"
        diff = "--- a/sample.py\n+++ b/sample.py\n@@ -2 +2 @@\n-    return 1\n+    return 2\n"
        _, output, _ = self.run_cli(source, diff, depth=0, callers=0)
        self.assertIn(self.source_path + ":4-4", output.split(","))

    def test_max_lines_drops_far_context(self):
        source = "def old():\n    return 0\ndef changed():\n    return 1\ndef caller():\n    return changed()\n"
        diff = "--- a/sample.py\n+++ b/sample.py\n@@ -4 +4 @@\n-    return 1\n+    return 2\n"
        _, output, error = self.run_cli(source, diff, depth=0, callers=1, max_lines=2)
        self.assertIn(self.source_path + ":3-4", output)
        self.assertIn("omitted", error)

    def test_toc(self):
        source = "class C:\n    def f(self):\n        pass\ndef g():\n    pass\n"
        _, output, _ = self.run_cli(source, names="f", toc=True)
        self.assertEqual(output.splitlines(), ["1: C", "2: C.f", "4: g"])

    def test_typed_attr_is_default_and_can_be_filtered(self):
        source = "class Cls:\n    def run(self):\n        return 1\ndef main():\n    runner = Cls()\n    return runner.run()\n"
        diff = "--- a/sample.py\n+++ b/sample.py\n@@ -3 +3 @@\n-        return 1\n+        return 2\n"
        _, default, _ = self.run_cli(source, diff, depth=0, callers=1)
        _, calls_only, _ = self.run_cli(source, diff, depth=0, callers=1, kinds="call")
        _, compat, _ = self.run_cli(source, diff, depth=0, callers=1, kinds="call", attr_calls=True)
        self.assertIn(6, self.selected_lines(default))
        self.assertNotIn(6, self.selected_lines(calls_only))
        self.assertIn(6, self.selected_lines(compat))

    def test_names_expand_call_layers(self):
        source = "def leaf():\n    return 1\ndef middle():\n    return leaf()\ndef top():\n    return middle()\n"
        _, output, _ = self.run_cli(source, names="leaf", depth=0, callers=2)
        self.assertTrue({3, 4, 5, 6}.issubset(self.selected_lines(output)))

    def test_layers_report_growth_and_saturation(self):
        source = "def leaf():\n    return 1\ndef caller():\n    return leaf()\n"
        _, output, _ = self.run_cli(source, names="leaf", depth=1, callers=0, layers=True)
        rows = output.splitlines()
        self.assertEqual(rows[0], "層 関数数 行数 割合%")
        # The order6 report includes both function count and selected metric
        # count before the percentage.
        self.assertRegex(rows[1], r"^0 \d+ \d+ \d+\.\d{2}%$")
        self.assertRegex(rows[-1], r"^飽和: \d+層$")

    def test_directions_do_not_mix(self):
        src = "def leaf():\n    return main()\ndef caller():\n    leaf()\ndef main():\n    pass\ndef unrelated():\n    main()\n"
        _, out, _ = self.run_cli(src, names="leaf", depth=2, callers=2, direction="up")
        self.assertNotIn(6, self.selected_lines(out))

    def test_attr_type_resolution_and_edges(self):
        src = "import subprocess\nclass Cls:\n    def f(self): pass\ndef go():\n    x = Cls()\n    x.f()\n    subprocess.run([])\n"
        _, out, _ = self.run_cli(src, names="Cls.f", depth=0, callers=1, attr_calls=True)
        self.assertIn(4, self.selected_lines(out))
        _, edgeout, _ = self.run_cli(src, names="Cls.f", edges=True, attr_calls=True)
        self.assertEqual(edgeout.splitlines(), ["go\tCls\tctor", "go\tCls.f\ttyped_attr"])

    def test_layer_metric_and_shortest_distance(self):
        src = "def a():\n    b()\ndef b():\n    c()\ndef c():\n    pass\ndef z():\n    pass\n"
        _, out, _ = self.run_cli(src, names="a", layers=True, direction="down", metric="funcs")
        self.assertIn("0 1 1 25.00%", out)
        self.assertIn("1 1 1 25.00%", out)
        self.assertIn("2 1 1 25.00%", out)

    def test_call_edges_cover_plain_self_cls_and_class_method(self):
        src = ("class C:\n"
               "    @classmethod\n    def cm(cls): return cls.helper()\n"
               "    def helper(self): return 1\n"
               "def f(): return 1\n"
               "def main():\n    f()\n    C.helper(None)\n")
        index = slice_funcs.Index(src, self.source_path)
        got = {(e.caller, e.callee) for e in slice_funcs.build_edges(index, {"call"})}
        self.assertIn(("main", "f"), got)
        self.assertIn(("main", "C.helper"), got)
        self.assertIn(("C.cm", "C.helper"), got)

    def test_ctor_edges_init_or_class(self):
        src = "class A:\n    def __init__(self): pass\nclass B:\n    pass\ndef main():\n    A()\n    B()\n"
        index = slice_funcs.Index(src, self.source_path)
        got = {(e.caller, e.callee, e.kind) for e in slice_funcs.build_edges(index, {"ctor"})}
        self.assertIn(("main", "A.__init__", "ctor"), got)
        self.assertIn(("main", "B", "ctor"), got)

    def test_typed_attr_local_and_self_field(self):
        src = ("class Worker:\n    def run(self): pass\n"
               "class Holder:\n    def __init__(self): self.w = Worker()\n    def go(self): self.w.run()\n"
               "def main():\n    x = Worker()\n    x.run()\n")
        index = slice_funcs.Index(src, self.source_path)
        got = {(e.caller, e.callee) for e in slice_funcs.build_edges(index, {"typed_attr"})}
        self.assertIn(("Holder.go", "Worker.run"), got)
        self.assertIn(("main", "Worker.run"), got)

    def test_ref_callback_value(self):
        src = "def cb(): pass\ndef register(x): pass\ndef main():\n    register(cb)\n"
        index = slice_funcs.Index(src, self.source_path)
        got = {(e.caller, e.callee) for e in slice_funcs.build_edges(index, {"ref"})}
        self.assertIn(("main", "cb"), got)

    def test_alias_last_assignment_and_chain(self):
        src = "def a(): pass\ndef b(): pass\nx = a\ny = x\nx = b\ndef main(): y(); x()\n"
        index = slice_funcs.Index(src, self.source_path)
        got = {(e.caller, e.callee) for e in slice_funcs.build_edges(index, {"alias"})}
        self.assertEqual(got, {("main", "b")})

    def test_dispatch_set_defaults_and_dict(self):
        src = ("def cmd_a(args=None): pass\ndef cmd_b(args=None): pass\n"
               "TABLE = {'b': cmd_b}\n"
               "def build(p):\n    sub = p.add_parser('a')\n    sub.set_defaults(func=cmd_a)\n"
               "def use(name):\n    return TABLE[name]\n")
        index = slice_funcs.Index(src, self.source_path)
        got = {(e.caller, e.callee) for e in slice_funcs.build_edges(index, {"dispatch"})}
        self.assertIn(("build", "cmd_a"), got)
        self.assertIn(("use", "cmd_b"), got)

    def test_dispatch_dynamic_func_use(self):
        src = ("def cmd(args=None): pass\n"
               "def build(p): p.set_defaults(func=cmd)\n"
               "def main(args): args.func(args)\n")
        index = slice_funcs.Index(src, self.source_path)
        got = {(e.caller, e.callee) for e in slice_funcs.build_edges(index, {"dispatch"})}
        self.assertIn(("main", "cmd"), got)

    def test_index_includes_import_assignment_and_alias_bindings(self):
        src = "import os as operating\ndef f(): pass\nCONST = 1\na = f\n"
        index = slice_funcs.Index(src, self.source_path)
        kinds = {(s.name, s.kind) for s in index.indexed_bindings}
        self.assertIn(("operating", "import"), kinds)
        self.assertIn(("CONST", "assignment"), kinds)
        self.assertIn(("a", "alias"), kinds)

    def test_nested_edge_and_qualified_name(self):
        src = "def outer():\n    def inner(): return 1\n    return inner()\n"
        index = slice_funcs.Index(src, self.source_path)
        self.assertIn("outer.<locals>.inner", index.functions)
        got = {(e.caller, e.callee) for e in slice_funcs.build_edges(index, {"nested"})}
        self.assertIn(("outer", "outer.<locals>.inner"), got)

    def test_decorator_edge(self):
        src = "def deco(fn): return fn\n@deco\ndef target(): pass\n"
        index = slice_funcs.Index(src, self.source_path)
        got = {(e.caller, e.callee) for e in slice_funcs.build_edges(index, {"decorator"})}
        self.assertIn(("deco", "target"), got)

    def test_imported_receiver_does_not_link_same_named_method(self):
        src = "import subprocess\nclass X:\n    def run(self): pass\ndef main(): subprocess.run([])\n"
        index = slice_funcs.Index(src, self.source_path)
        got = {(e.caller, e.callee) for e in slice_funcs.build_edges(index)}
        self.assertNotIn(("main", "X.run"), got)

    def test_both_direction_does_not_cross_expand(self):
        src = "def up(): mid()\ndef mid(): down()\ndef down(): pass\ndef up2(): up()\ndef lateral(): down()\n"
        index = slice_funcs.Index(src, self.source_path)
        edges = slice_funcs.build_edges(index, {"call"})
        dist = slice_funcs.traverse(["mid"], edges, depth=2, callers=2, direction="both")
        self.assertIn("up", dist)
        self.assertIn("down", dist)
        self.assertIn("up2", dist)
        # No upward traversal starts from the down-side result, so unrelated callers of down stay out.
        self.assertNotIn("lateral", dist)

    def test_shortest_distance(self):
        src = "def a(): b(); c()\ndef b(): d()\ndef c(): d()\ndef d(): pass\n"
        index = slice_funcs.Index(src, self.source_path)
        edges = slice_funcs.build_edges(index, {"call"})
        dist = slice_funcs.traverse(["a"], edges, 5, 0, "down")
        self.assertEqual(dist["d"], 2)

    def test_kinds_filters_edges(self):
        src = "def target(): pass\ndef main():\n    x = target\n    target()\n"
        index = slice_funcs.Index(src, self.source_path)
        calls = slice_funcs.build_edges(index, {"call"})
        refs = slice_funcs.build_edges(index, {"ref"})
        self.assertEqual({e.kind for e in calls}, {"call"})
        self.assertEqual({e.kind for e in refs}, {"ref"})

    def test_compare_trace(self):
        src = "def a(): b()\ndef b(): pass\ndef c(): pass\n"
        trace = "<module>\ta\na\tb\na\tc\n<lambda>\tb\n"
        code, out, _ = self.run_cli(src, names=None, compare_trace=trace, kinds="call")
        self.assertEqual(code, 0)
        self.assertIn("trace_pairs\t2", out)
        self.assertIn("matched_pairs\t1", out)
        self.assertIn("missing\ta\tc", out)

    def test_custom_provider_registration_only(self):
        class Custom(slice_funcs.EdgeProvider):
            name = "custom"
            def edges(self, index):
                return [slice_funcs.Edge("a", "b", self.name, 1)]
        src = "def a(): pass\ndef b(): pass\n"
        index = slice_funcs.Index(src, self.source_path)
        providers = slice_funcs.EDGE_PROVIDERS + [Custom()]
        kinds = slice_funcs._parse_kinds("custom", providers)
        got = slice_funcs.build_edges(index, kinds, providers)
        self.assertEqual({(e.caller, e.callee, e.kind) for e in got}, {("a", "b", "custom")})

    def test_relevance_distance_score_prefers_near(self):
        src = "def a(): b()\ndef b(): c()\ndef c(): pass\n"
        index = slice_funcs.Index(src, self.source_path)
        edges = slice_funcs.build_edges(index, {"call"})
        context = slice_funcs._score_context(index, ["a"], edges, "down", None, None, 200)
        provider = slice_funcs.DistanceScore()
        self.assertGreater(provider.score(index, ["a"], "b", context),
                           provider.score(index, ["a"], "c", context))
        self.assertEqual(context["distance_down"]["c"], 2)

    def test_relevance_edge_kind_order(self):
        src = "def seed(): pass\ndef via_call(): pass\ndef via_ref(): pass\n"
        index = slice_funcs.Index(src, self.source_path)
        context = {"path_edge_kinds": {"via_call": {"call"}, "via_ref": {"ref"}}}
        provider = slice_funcs.EdgeKindScore()
        self.assertGreater(provider.score(index, ["seed"], "via_call", context),
                           provider.score(index, ["seed"], "via_ref", context))

    def test_relevance_shared_state_self_field(self):
        src = ("class C:\n"
               "    def seed(self): return self.x\n"
               "    def related(self): self.x = 2\n"
               "    def other(self): return self.y\n")
        index = slice_funcs.Index(src, self.source_path)
        context = {"state": {name: slice_funcs._state_keys(index, sym)
                             for name, sym in index.functions.items()}}
        provider = slice_funcs.SharedStateScore()
        self.assertGreater(provider.score(index, ["C.seed"], "C.related", context),
                           provider.score(index, ["C.seed"], "C.other", context))

    def test_relevance_diff_terms_identifier_and_string(self):
        src = ("def seed(): pass\n"
               "def related(): return getconf('TMPDIR')\n"
               "def other(): return plain\n")
        index = slice_funcs.Index(src, self.source_path)
        diff = "--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-def seed(): pass\n+def seed(): return getconf('TMPDIR')\n"
        context = {"diff_terms": slice_funcs._diff_terms(diff)}
        provider = slice_funcs.DiffTermsScore()
        self.assertGreater(provider.score(index, ["seed"], "related", context),
                           provider.score(index, ["seed"], "other", context))

    def test_relevance_cochange_from_temporary_git_repo(self):
        repo = os.path.join(self.temp.name, "repo")
        os.mkdir(repo)
        path = os.path.join(repo, "sample.py")
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(["git", "-C", repo, "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
        def commit(text, message):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            subprocess.run(["git", "-C", repo, "add", "sample.py"], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-q", "-m", message], check=True)
        commit("def seed():\n    return 1\ndef related():\n    return 1\ndef other():\n    return 1\n", "initial")
        commit("def seed():\n    return 2\ndef related():\n    return 2\ndef other():\n    return 1\n", "together")
        commit("def seed():\n    return 3\ndef related():\n    return 2\ndef other():\n    return 2\n", "split")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        index = slice_funcs.Index(source, path)
        counts = slice_funcs._git_cochange(index, ["seed"], repo, 200)
        self.assertGreater(counts["related"], 0)
        provider = slice_funcs.CochangeScore()
        context = {"cochange": counts}
        self.assertGreater(provider.score(index, ["seed"], "related", context), 0.0)

    def test_relevance_large_function_is_trimmed_to_head_and_link_window(self):
        body = ["def changed():", "    return 1", "", "def caller():"]
        body.extend("    v{0} = {0}".format(i) for i in range(90))
        body.append("    return changed()")
        body.extend("    w{0} = {0}".format(i) for i in range(90))
        src = "\n".join(body) + "\n"
        code, output, _ = self.run_cli(src, names="changed", direction="up",
                                       select="relevance", budget=80,
                                       large_symbol_lines=50, context_lines=3)
        self.assertEqual(code, 0)
        lines = self.selected_lines(output)
        call_line = 95
        self.assertIn(call_line, lines)
        self.assertIn(4, lines)
        self.assertLess(len(lines), 30)
        self.assertNotIn(len(body), lines)

    def test_relevance_budget_stops_but_seed_is_mandatory(self):
        src = ("def seed():\n    a(); b()\n"
               "def a():\n    return 1\n"
               "def b():\n    return 2\n")
        code, output, _ = self.run_cli(src, names="seed", direction="down",
                                       select="relevance", budget=4)
        self.assertEqual(code, 0)
        lines = self.selected_lines(output)
        self.assertTrue({1, 2}.issubset(lines))
        self.assertLessEqual(len(lines), 4)
        self.assertEqual(len(lines & {3, 4, 5, 6}), 2)

    def test_relevance_seed_can_exceed_budget(self):
        src = "def seed():\n" + "".join("    x{} = {}\n".format(i, i) for i in range(8))
        code, output, _ = self.run_cli(src, names="seed", select="relevance", budget=2)
        self.assertEqual(code, 0)
        self.assertEqual(self.selected_lines(output), set(range(1, 10)))

    def test_relevance_weights_change_selection(self):
        src = ("def special(): return 'special'\n"
               "def near(): return 2\n"
               "def seed():\n    near()\n    special()\n")
        diff = ("--- a/sample.py\n+++ b/sample.py\n@@ -4 +4 @@\n"
                "-    near()\n+    near(); token_special = 'special'\n")
        # Both candidates are one call away; with distance/edge_kind zeroed,
        # diff_terms alone makes special rank first under a two-line remainder.
        code, output, _ = self.run_cli(src, diff=diff, callers=0, direction="down",
                                       select="relevance", budget=4,
                                       weights="distance=0,edge_kind=0,shared_state=0,diff_terms=10,cochange=0")
        self.assertEqual(code, 0)
        self.assertIn(1, self.selected_lines(output))
        self.assertNotIn(2, self.selected_lines(output))

    def test_relevance_explain_has_score_parts_and_trim_flag(self):
        src = "def helper(): return 1\ndef seed(): return helper()\n"
        code, _, error = self.run_cli(src, names="seed", direction="down",
                                      select="relevance", budget=10, explain=True)
        self.assertEqual(code, 0)
        self.assertIn("explain seed score=", error)
        self.assertIn("distance=", error)
        self.assertIn("edge_kind=", error)
        self.assertIn("trimmed=no", error)

    def test_score_provider_extension_requires_only_provider_registration(self):
        class Always(slice_funcs.ScoreProvider):
            name = "always"
            default_weight = 2.0
            def score(self, index, seeds, candidate, context=None):
                return 0.75
        src = "def seed(): pass\ndef candidate(): pass\n"
        index = slice_funcs.Index(src, self.source_path)
        result = slice_funcs.score_symbols(index, ["seed"], ["candidate"], {}, providers=[Always()])
        self.assertEqual(result["candidate"][0], 0.75)
        self.assertEqual(result["candidate"][1]["always"], 0.75)
        self.assertEqual(slice_funcs._parse_weights("always=3", [Always()]), {"always": 3.0})

    def test_relevance_percentage_budget(self):
        src = "def seed():\n    a()\ndef a():\n    return 1\n" + "\n" * 6
        code, output, _ = self.run_cli(src, names="seed", direction="down",
                                       select="relevance", budget="25%")
        self.assertEqual(code, 0)
        self.assertTrue({1, 2}.issubset(self.selected_lines(output)))


if __name__ == "__main__":
    unittest.main()
