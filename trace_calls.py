#!/usr/bin/env python3
"""Trace same-file Python calls while running a target as __main__."""
import argparse
import os
import runpy
import sys


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', required=True)
    known, command = parser.parse_known_args(argv)
    if command and command[0] == '--': command = command[1:]
    target = os.path.realpath(known.file)
    old_argv = sys.argv[:]
    sys.argv = [known.file] + command
    stack = []
    seen = set()
    def tracer(frame, event, arg):
        if os.path.realpath(frame.f_code.co_filename) != target: return tracer
        if event == 'call':
            callee = frame.f_code
            q = getattr(callee, 'co_qualname', None)
            if not q:
                q = callee.co_name
                first = callee.co_varnames[0] if callee.co_argcount else None
                obj = frame.f_locals.get(first) if first else None
                if first in ('self','cls') and obj is not None: q = type(obj).__name__ + '.' + q
            stack.append((frame, q))
            for parent, pname in reversed(stack[:-1]):
                if parent.f_code.co_filename == callee.co_filename:
                    seen.add((pname, q)); break
        elif event == 'return':
            if stack and stack[-1][0] is frame: stack.pop()
        return tracer
    try:
        sys.settrace(tracer)
        try:
            runpy.run_path(known.file, run_name='__main__')
        except BaseException:
            pass
        finally:
            sys.settrace(None)
    finally:
        sys.argv = old_argv
    for caller, callee in sorted(seen): print('{}\t{}'.format(caller, callee))
    return 0


if __name__ == '__main__':
    sys.exit(main())
