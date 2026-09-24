import contextlib
import io
import os
import tempfile
import unittest

import trace_calls


class TraceCallsTests(unittest.TestCase):
    def test_records_same_file_calls_and_returns_zero_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, 'sample.py')
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('class C:\n    def f(self):\n        helper()\ndef helper():\n    pass\nc = C()\nc.f()\nraise RuntimeError()\n')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                result = trace_calls.main(['--file', path, '--', 'arg'])
            self.assertEqual(result, 0)
            rows = out.getvalue().splitlines()
            self.assertIn('C.f\thelper', rows)
            self.assertIn('<module>\tC.f', rows)
            self.assertIn('<module>\tC', rows)


if __name__ == '__main__':
    unittest.main()
