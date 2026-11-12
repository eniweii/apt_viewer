#!/usr/bin/env python3
"""Synthetic checks for apt_decompile control flow (loops, break, continue, else). Run: python test_decompile.py"""
import sys
import apt_actions as AA
import apt_decompile as D

I = lambda k: ("int", k)
R = lambda n: ("reg", n)
S = lambda s: ("str", s)
PUSH, NOT, POP, END = 0x96, 0x12, 0x17, 0x00
ALWAYS, IFTRUE, IFFALSE = 0x99, 0x9D, 0xB8
LT, GT, ADD, STORE, CALLF = 0x0F, 0x67, 0x0A, 0x87, 0x3D


def asm(prog):
    """prog items: "label" strings, or (op, args) / (op, args, "label") tuples. Targets become instruction indices."""
    labels, items = {}, []
    for it in prog:
        if isinstance(it, str):
            labels[it] = len(items)
        else:
            items.append(it)
    return [AA.Insn(k * 4, it[0], it[1], target=labels[it[2]] if len(it) > 2 else None) for k, it in enumerate(items)]


def check(name, prog, want):
    got = "\n".join(D.decompile(asm(prog))[0]).strip()
    ok = got == want.strip()
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print("--- got\n" + got + "\n--- want\n" + want.strip())
    return ok


def call(f):   return [(PUSH, [I(0)]), (PUSH, [S(f)]), (CALLF, []), (POP, [])]
def inc():     return [(PUSH, [R(2)]), (PUSH, [I(1)]), (ADD, []), (STORE, [R(2)]), (POP, [])]
def lt(n):     return [(PUSH, [R(2)]), (PUSH, [I(n)]), (LT, [])]


results = [
    # while (r2 < 5) { f(); r2 = r2 + 1 }
    check("while", ["L"] + lt(5) + [(NOT, []), (IFTRUE, [], "E")] + call("f") + inc() + [(ALWAYS, [], "L"), "E", (END, [])],
          "while ((r2 < 5)) {\n  f();\n  r2 = (r2 + 1);\n}"),
    # exit test without not: while (!(r2 > 5))
    check("while, plain exit test", ["L", (PUSH, [R(2)]), (PUSH, [I(5)]), (GT, []), (IFTRUE, [], "E")] + call("f") + [(ALWAYS, [], "L"), "E", (END, [])],
          "while (!((r2 > 5))) {\n  f();\n}"),
    # while (true) { f(); }
    check("while(true)", ["L"] + call("f") + [(ALWAYS, [], "L"), "E", (END, [])],
          "while (true) {\n  f();\n}"),
    # while (r2 < 5) { if (r2 > 2) break; f(); }  (break jump after an inverted test)
    check("break in if", ["L"] + lt(5) + [(NOT, []), (IFTRUE, [], "E"),
          (PUSH, [R(2)]), (PUSH, [I(2)]), (GT, []), (NOT, []), (IFTRUE, [], "A"), (ALWAYS, [], "E"), "A"] + call("f") + [(ALWAYS, [], "L"), "E", (END, [])],
          "while ((r2 < 5)) {\n  if ((r2 > 2)) {\n    break;\n  }\n  f();\n}"),
    # conditional jump straight to the exit
    check("break via branchIfTrue", ["L"] + lt(5) + [(NOT, []), (IFTRUE, [], "E"),
          (PUSH, [R(2)]), (PUSH, [I(2)]), (GT, []), (IFTRUE, [], "E")] + call("f") + [(ALWAYS, [], "L"), "E", (END, [])],
          "while ((r2 < 5)) {\n  if ((r2 > 2)) {\n    break;\n  }\n  f();\n}"),
    # continue: jump back to the loop start from inside an if
    check("continue", ["L"] + lt(5) + [(NOT, []), (IFTRUE, [], "E")] + inc() +
          [(PUSH, [R(2)]), (PUSH, [I(2)]), (GT, []), (NOT, []), (IFTRUE, [], "A"), (ALWAYS, [], "L"), "A"] + call("f") + [(ALWAYS, [], "L"), "E", (END, [])],
          "while ((r2 < 5)) {\n  r2 = (r2 + 1);\n  if ((r2 > 2)) {\n    continue;\n  }\n  f();\n}"),
    # do { f(); r2++ } while (r2 < 5)
    check("do while", ["L"] + call("f") + inc() + lt(5) + [(IFTRUE, [], "L"), (END, [])],
          "do {\n  f();\n  r2 = (r2 + 1);\n} while ((r2 < 5));"),
    # real if/else must stay if/else, also inside a loop
    check("if else in loop", ["L"] + lt(5) + [(NOT, []), (IFTRUE, [], "E"),
          (PUSH, [R(3)]), (NOT, []), (IFTRUE, [], "ELSE")] + call("a") + [(ALWAYS, [], "J"), "ELSE"] + call("b") + ["J"] + inc() + [(ALWAYS, [], "L"), "E", (END, [])],
          "while ((r2 < 5)) {\n  if (r3) {\n    a();\n  } else {\n    b();\n  }\n  r2 = (r2 + 1);\n}"),
    # nested loops; the inner break leaves only the inner loop
    check("nested", ["L"] + lt(5) + [(NOT, []), (IFTRUE, [], "E"),
          "M", (PUSH, [R(3)]), (NOT, []), (IFTRUE, [], "F"),
          (PUSH, [R(4)]), (NOT, []), (IFTRUE, [], "A"), (ALWAYS, [], "F"), "A"] + call("g") + [(ALWAYS, [], "M"), "F"] + inc() + [(ALWAYS, [], "L"), "E", (END, [])],
          "while ((r2 < 5)) {\n  while (r3) {\n    if (r4) {\n      break;\n    }\n    g();\n  }\n  r2 = (r2 + 1);\n}"),
]

STRICT = 0x66
def sw_cases(vals):
    """tests for switch(r2): first test stores the value, later tests push r0 again"""
    code = [(PUSH, [R(2)]), (0x87, [I(0)])]
    for n, (v, lab) in enumerate(vals):
        code += ([(PUSH, [v])] if n == 0 else [(PUSH, [R(0), v])]) + [(STRICT, []), (IFTRUE, [], lab)]
    return code

results += [
    # switch (r2) { case 1: a(); break; case 2: b(); break; default: c(); }
    check("switch with default", sw_cases([(I(1), "C1"), (I(2), "C2")]) + [(ALWAYS, [], "D"), "C1"] + call("a") + [(ALWAYS, [], "E"), "C2"] + call("b") +
          [(ALWAYS, [], "E"), "D"] + call("c") + ["E", (END, [])],
          "switch (r2) {\ncase 1:\n  a();\n  break;\ncase 2:\n  b();\n  break;\ndefault:\n  c();\n}"),
    # no default, case value is an expression, fall-through from case 1 into case 2
    check("switch, expression case, fall through", sw_cases([(S("x"), "C1"), (S("y"), "C2")]) + [(ALWAYS, [], "E"), "C1"] + call("a") + ["C2"] + call("b") +
          [(ALWAYS, [], "E"), "E", (END, [])],
          'switch (r2) {\ncase "x":\n  a();\ncase "y":\n  b();\n}'),
    # if (a || b) { f() }   A; brIfTrue L; B; brIfTrue L; brAlways S; L: f(); S:
    check("if with or", [(PUSH, [R(3)]), (IFTRUE, [], "L"), (PUSH, [R(4)]), (IFTRUE, [], "L"), (ALWAYS, [], "S"), "L"] + call("f") + ["S", (END, [])],
          "if ((r3 || r4)) {\n  f();\n}"),
    # if (c) { a() } else { b() } compiled with a jump-to-body test
    check("if else, jump to body", [(PUSH, [R(3)]), (IFTRUE, [], "L"), (ALWAYS, [], "S"), "L"] + call("a") + [(ALWAYS, [], "E"), "S"] + call("b") + ["E", (END, [])],
          "if (r3) {\n  a();\n} else {\n  b();\n}"),
    # r4 = r3 ? 1 : 2
    check("ternary", [(PUSH, [S("v")]), (PUSH, [R(3)]), (NOT, []), (IFTRUE, [], "B"), (PUSH, [I(1)]), (ALWAYS, [], "E"), "B", (PUSH, [I(2)]), "E", (0x1D, []), (END, [])],
          'v = (r3 ? 1 : 2);'),
]
results += [
    check("trace pops its message", [(PUSH, [S("a")]), (PUSH, [S("b")]), (0x47, []), (0x26, []), (PUSH, [S("x")]), (PUSH, [I(1)]), (0x1D, []), (END, [])],
          'trace(("a" + "b"));\nx = 1;'),
    check("bit shifts", [(PUSH, [S("v")]), (PUSH, [R(2)]), (PUSH, [I(3)]), (0x63, []), (PUSH, [I(2)]), (0x64, []), (PUSH, [I(1)]), (0x65, []), (0x1D, []), (END, [])],
          'v = (((r2 << 3) >> 2) >>> 1);'),
]
results += [
    check("defineLocal2 pops only the name", [(PUSH, [S("a")]), (0x41, []), (PUSH, [S("b")]), (PUSH, [I(2)]), (0x3C, []), (END, [])],
          'var a;\nvar b = 2;'),
    check("named function is a declaration", [(PUSH, [I(0)]), (PUSH, [S("f")]), (CALLF, []), (POP, [])], 'f();'),
]
VAR = lambda n: [(PUSH, [S(n)]), (0x1C, [])]
results += [
    check("extends", VAR("Sub") + VAR("Sup") + [(0x69, []), (END, [])], "Sub extends Sup;"),
    check("delete", [(PUSH, [R(2)]), (PUSH, [S("x")]), (0x3A, []), (POP, []), (END, [])], "delete r2.x;"),
    check("object literal", [(PUSH, [S("v")]), (PUSH, [S("a"), I(1), S("b"), I(2), I(2)]), (0x43, []), (0x1D, []), (END, [])], "v = {b: 2, a: 1};"),
    check("array literal", [(PUSH, [S("v")]), (PUSH, [I(1), I(2), I(2)]), (0x42, []), (0x1D, []), (END, [])], "v = [2, 1];"),
    check("super call", [(PUSH, [I(0), R(2), ("undef",)]), (0x52, []), (POP, []), (END, [])], "r2();"),
    check("for in", [(PUSH, [R(3)]), (0x55, []), "L", (0x87, [I(0)]), (PUSH, [("undef",)]), (0x49, []), (IFTRUE, [], "E"),
          (PUSH, [R(0)]), (0x87, [I(4)]), (POP, [])] + call("f") + [(ALWAYS, [], "L"), "E", (END, [])],
          "for (r4 in r3) {\n  f();\n}"),
]
sys.exit(0 if all(results) else 1)
