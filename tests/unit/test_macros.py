#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# input-remapper - GUI for device specific keyboard mappings
# Copyright (C) 2024 sezanzeb <b8x45ygc9@mozmail.com>
#
# This file is part of input-remapper.
#
# input-remapper is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# input-remapper is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with input-remapper.  If not, see <https://www.gnu.org/licenses/>.


import asyncio
import multiprocessing
import re
import time
import unittest
from unittest.mock import patch

from evdev.ecodes import (
    EV_REL,
    EV_KEY,
    ABS_Y,
    REL_Y,
    REL_HWHEEL,
    REL_HWHEEL_HI_RES,
    KEY_A,
    KEY_B,
    KEY_C,
    KEY_E,
    KEY_1,
    KEY_2,
    LED_CAPSL,
    LED_NUML,
)

from inputremapper.configs.keyboard_layout import keyboard_layout
from inputremapper.configs.preset import Preset
from inputremapper.configs.validation_errors import (
    MacroError,
    SymbolNotAvailableInTargetError,
)
from inputremapper.injection.context import Context
from inputremapper.injection.global_uinputs import GlobalUInputs, UInput
from inputremapper.injection.macros.argument import Argument, ArgumentConfig
from inputremapper.injection.macros.macro import Macro, macro_variables
from inputremapper.injection.macros.parse import Parser
from inputremapper.injection.macros.raw_value import RawValue
from inputremapper.injection.macros.task import Task
from inputremapper.injection.macros.tasks.hold_keys import HoldKeysTask
from inputremapper.injection.macros.tasks.if_tap import IfTapTask
from inputremapper.injection.macros.tasks.key import KeyTask
from inputremapper.injection.macros.variable import Variable
from inputremapper.injection.mapping_handlers.mapping_parser import MappingParser
from inputremapper.input_event import InputEvent
from tests.lib.fixtures import fixtures
from tests.lib.logger import logger
from tests.lib.patches import InputDevice
from tests.lib.test_setup import test_setup


class MacroTestBase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        macro_variables.start()

    def setUp(self):
        self.result = []
        self.global_uinputs = GlobalUInputs(UInput)
        self.mapping_parser = MappingParser(self.global_uinputs)

        try:
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            # suddenly "There is no current event loop in thread 'MainThread'"
            # errors started to appear
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

        self.source_device = InputDevice(fixtures.bar_device.path)

        self.context = Context(
            Preset(),
            source_devices={fixtures.bar_device.get_device_hash(): self.source_device},
            forward_devices={},
            mapping_parser=self.mapping_parser,
        )

    def tearDown(self):
        self.result = []

    def handler(self, type_: int, code: int, value: int):
        """Where macros should write codes to."""
        logger.info(f"macro wrote{(type_, code, value)}")
        self.result.append((type_, code, value))

    async def trigger_sequence(self, macro: Macro, event):
        for listener in self.context.listeners:
            asyncio.ensure_future(listener(event))
            # this still might cause race conditions and the test to fail
            await asyncio.sleep(0)

        macro.press_trigger()
        if macro.running:
            return
        asyncio.ensure_future(macro.run(self.handler))

    async def release_sequence(self, macro: Macro, event):
        for listener in self.context.listeners:
            asyncio.ensure_future(listener(event))
            # this still might cause race conditions and the test to fail
            await asyncio.sleep(0)

        macro.release_trigger()

    def count_child_macros(self, macro) -> int:
        count = 0
        for task in macro.tasks:
            count += len(task.child_macros)
            for child_macro in task.child_macros:
                count += self.count_child_macros(child_macro)
        return count

    def count_tasks(self, macro) -> int:
        count = len(macro.tasks)
        for task in macro.tasks:
            for child_macro in task.child_macros:
                count += self.count_tasks(child_macro)
        return count


class DummyMapping:
    macro_key_sleep_ms = 10
    rel_rate = 60
    target_uinput = "keyboard + mouse"


@test_setup
class TestArgument(MacroTestBase):
    def test_resolve(self):
        self.assertEqual(Variable("a", const=True).get_value(), "a")
        self.assertEqual(Variable(1, const=True).get_value(), 1)
        self.assertEqual(Variable(None, const=True).get_value(), None)

        # $ is part of a custom string here
        self.assertEqual(Variable('"$a"', const=True).get_value(), '"$a"')
        self.assertEqual(Variable("'$a'", const=True).get_value(), "'$a'")
        self.assertEqual(Variable("$a", const=True).get_value(), "$a")

        variable = Variable("a", const=False)
        self.assertEqual(variable.get_value(), None)
        macro_variables["a"] = 1
        self.assertEqual(variable.get_value(), 1)

    def test_type_check(self):
        def test(value, types, name, position):
            argument = Argument(
                ArgumentConfig(
                    types=types,
                    name=name,
                    position=position,
                ),
                DummyMapping(),
            )
            argument.initialize_variable(RawValue(value=value))
            return argument.get_value()

        def test_variable(variable, types, name, position):
            argument = Argument(
                ArgumentConfig(
                    types=types,
                    name=name,
                    position=position,
                ),
                DummyMapping(),
            )
            argument._variable = variable
            return argument.get_value()

        # allows params that can be cast to the target type
        self.assertEqual(test("1", [str, None], "foo", 0), "1")
        self.assertEqual(test("1.2", [str], "foo", 2), "1.2")

        self.assertRaises(
            MacroError,
            lambda: test("1.2", [int], "foo", 3),
        )
        self.assertRaises(MacroError, lambda: test("a", [None], "foo", 0))
        self.assertRaises(MacroError, lambda: test("a", [int], "foo", 1))
        self.assertRaises(
            MacroError,
            lambda: test("a", [int, float], "foo", 2),
        )
        self.assertRaises(
            MacroError,
            lambda: test("a", [int, None], "foo", 3),
        )
        self.assertEqual(test("a", [int, float, None, str], "foo", 4), "a")

        # variables are expected to be of the Variable type here, not a $string
        self.assertRaises(
            MacroError,
            lambda: test("$a", [int], "foo", 4),
        )

        # We don't cast values that were explicitly set as strings back into numbers.
        variable = Variable("a", const=False)
        variable.set_value("5")
        self.assertRaises(
            MacroError,
            lambda: test_variable(variable, [int], "foo", 4),
        )

        self.assertRaises(
            MacroError,
            lambda: test("a", [Macro], "foo", 0),
        )
        self.assertRaises(MacroError, lambda: test("1", [Macro], "foo", 0))

    def test_validate_variable_name(self):
        self.assertRaises(
            MacroError,
            lambda: Variable("1a", const=False).validate_variable_name(),
        )
        self.assertRaises(
            MacroError,
            lambda: Variable("$a", const=False).validate_variable_name(),
        )
        self.assertRaises(
            MacroError,
            lambda: Variable("a()", const=False).validate_variable_name(),
        )
        self.assertRaises(
            MacroError,
            lambda: Variable("1", const=False).validate_variable_name(),
        )
        self.assertRaises(
            MacroError,
            lambda: Variable("+", const=False).validate_variable_name(),
        )
        self.assertRaises(
            MacroError,
            lambda: Variable("-", const=False).validate_variable_name(),
        )
        self.assertRaises(
            MacroError,
            lambda: Variable("*", const=False).validate_variable_name(),
        )
        self.assertRaises(
            MacroError,
            lambda: Variable("a,b", const=False).validate_variable_name(),
        )
        self.assertRaises(
            MacroError,
            lambda: Variable("a,b", const=False).validate_variable_name(),
        )
        self.assertRaises(
            MacroError,
            lambda: Variable("#", const=False).validate_variable_name(),
        )
        self.assertRaises(
            MacroError,
            lambda: Variable(1, const=False).validate_variable_name(),
        )
        self.assertRaises(
            MacroError,
            lambda: Variable(None, const=False).validate_variable_name(),
        )
        self.assertRaises(
            MacroError,
            lambda: Variable([], const=False).validate_variable_name(),
        )
        self.assertRaises(
            MacroError,
            lambda: Variable((), const=False).validate_variable_name(),
        )

        # doesn't raise
        Variable("a", const=False).validate_variable_name()
        Variable("_a", const=False).validate_variable_name()
        Variable("_A", const=False).validate_variable_name()
        Variable("A", const=False).validate_variable_name()
        Variable("Abcd", const=False).validate_variable_name()
        Variable("Abcd_", const=False).validate_variable_name()
        Variable("Abcd_1234", const=False).validate_variable_name()
        Variable("Abcd1234_", const=False).validate_variable_name()


@test_setup
class TestParsing(MacroTestBase):
    def test_get_macro_argument_names(self):
        self.assertEqual(
            IfTapTask.get_macro_argument_names(),
            ["then", "else", "timeout"],
        )

        self.assertEqual(
            HoldKeysTask.get_macro_argument_names(),
            ["*symbols"],
        )

    def test_get_num_parameters(self):
        self.assertEqual(IfTapTask.get_num_parameters(), (0, 3))
        self.assertEqual(KeyTask.get_num_parameters(), (1, 1))
        self.assertEqual(HoldKeysTask.get_num_parameters(), (0, float("inf")))

    def test_remove_whitespaces(self):
        self.assertEqual(Parser.remove_whitespaces('foo"bar"foo'), 'foo"bar"foo')
        self.assertEqual(Parser.remove_whitespaces('foo" bar"foo'), 'foo" bar"foo')
        self.assertEqual(
            Parser.remove_whitespaces('foo" bar"fo" "o'), 'foo" bar"fo" "o'
        )
        self.assertEqual(
            Parser.remove_whitespaces(' fo o"\nba r "f\noo'), 'foo"\nba r "foo'
        )
        self.assertEqual(Parser.remove_whitespaces(' a " b " c " '), 'a" b "c" ')

        self.assertEqual(Parser.remove_whitespaces('"""""""""'), '"""""""""')
        self.assertEqual(Parser.remove_whitespaces('""""""""'), '""""""""')

        self.assertEqual(Parser.remove_whitespaces("      "), "")
        self.assertEqual(Parser.remove_whitespaces('     " '), '" ')
        self.assertEqual(Parser.remove_whitespaces('     " " '), '" "')

        self.assertEqual(Parser.remove_whitespaces("a# ##b", delimiter="##"), "a###b")
        self.assertEqual(Parser.remove_whitespaces("a###b", delimiter="##"), "a###b")
        self.assertEqual(Parser.remove_whitespaces("a## #b", delimiter="##"), "a## #b")
        self.assertEqual(
            Parser.remove_whitespaces("a## ##b", delimiter="##"), "a## ##b"
        )

    def test_remove_comments(self):
        self.assertEqual(Parser.remove_comments("a#b"), "a")
        self.assertEqual(Parser.remove_comments('"a#b"'), '"a#b"')
        self.assertEqual(Parser.remove_comments('a"#"#b'), 'a"#"')
        self.assertEqual(Parser.remove_comments('a"#""#"#b'), 'a"#""#"')
        self.assertEqual(Parser.remove_comments('#a"#""#"#b'), "")

        self.assertEqual(
            re.sub(
                r"\s",
                "",
                Parser.remove_comments(
                    """
            # a
            b
            # c
            d
        """
                ),
            ),
            "bd",
        )

    async def test_count_brackets(self):
        self.assertEqual(Parser._count_brackets(""), 0)
        self.assertEqual(Parser._count_brackets("()"), 2)
        self.assertEqual(Parser._count_brackets("a()"), 3)
        self.assertEqual(Parser._count_brackets("a(b)"), 4)
        self.assertEqual(Parser._count_brackets("a(b())"), 6)
        self.assertEqual(Parser._count_brackets("a(b(c))"), 7)
        self.assertEqual(Parser._count_brackets("a(b(c))d"), 7)
        self.assertEqual(Parser._count_brackets("a(b(c))d()"), 7)

    def test_split_keyword_arg(self):
        self.assertTupleEqual(Parser._split_keyword_arg("_A=b"), ("_A", "b"))
        self.assertTupleEqual(Parser._split_keyword_arg("a_=1"), ("a_", "1"))
        self.assertTupleEqual(
            Parser._split_keyword_arg("a=repeat(2, KEY_A)"),
            ("a", "repeat(2, KEY_A)"),
        )
        self.assertTupleEqual(Parser._split_keyword_arg('a="=,#+."'), ("a", '"=,#+."'))

    def test_is_this_a_macro(self):
        self.assertTrue(Parser.is_this_a_macro("key(1)"))
        self.assertTrue(Parser.is_this_a_macro("key(1).key(2)"))
        self.assertTrue(Parser.is_this_a_macro("repeat(1, key(1).key(2))"))

        self.assertFalse(Parser.is_this_a_macro("1"))
        self.assertFalse(Parser.is_this_a_macro("key_kp1"))
        self.assertFalse(Parser.is_this_a_macro("btn_left"))
        self.assertFalse(Parser.is_this_a_macro("minus"))
        self.assertFalse(Parser.is_this_a_macro("k"))
        self.assertFalse(Parser.is_this_a_macro(1))
        self.assertFalse(Parser.is_this_a_macro(None))

        self.assertTrue(Parser.is_this_a_macro("a+b"))
        self.assertTrue(Parser.is_this_a_macro("a+b+c"))
        self.assertTrue(Parser.is_this_a_macro("a + b"))
        self.assertTrue(Parser.is_this_a_macro("a + b + c"))

    def test_handle_plus_syntax(self):
        self.assertEqual(Parser.handle_plus_syntax("a + b"), "hold_keys(a,b)")
        self.assertEqual(Parser.handle_plus_syntax("a + b + c"), "hold_keys(a,b,c)")
        self.assertEqual(Parser.handle_plus_syntax(" a+b+c "), "hold_keys(a,b,c)")

        # invalid. The last one with `key` should not have been a parameter
        # of this function to begin with.
        strings = ["+", "a+", "+b", "a\n+\n+\nb", "key(a + b)"]
        for string in strings:
            with self.assertRaises(MacroError):
                logger.info(f'testing "%s"', string)
                Parser.handle_plus_syntax(string)

        self.assertEqual(Parser.handle_plus_syntax("a"), "a")
        self.assertEqual(Parser.handle_plus_syntax("key(a)"), "key(a)")
        self.assertEqual(Parser.handle_plus_syntax(""), "")

    def test_parse_plus_syntax(self):
        macro = Parser.parse("a + b")
        self.assertEqual(macro.code, "hold_keys(a,b)")

        # this is not erroneously recognized as "plus" syntax
        macro = Parser.parse("key(a) # a + b")
        self.assertEqual(macro.code, "key(a)")

    async def test_extract_params(self):
        # splits strings, doesn't try to understand their meaning yet
        def expect(raw, expectation):
            self.assertListEqual(Parser._extract_args(raw), expectation)

        expect("a", ["a"])
        expect("a,b", ["a", "b"])
        expect("a,b,c", ["a", "b", "c"])

        expect("key(a)", ["key(a)"])
        expect("key(a).key(b), key(a)", ["key(a).key(b)", "key(a)"])
        expect("key(a), key(a).key(b)", ["key(a)", "key(a).key(b)"])

        expect(
            'a("foo(1,2,3)", ",,,,,,    "), , ""',
            ['a("foo(1,2,3)", ",,,,,,    ")', "", '""'],
        )

        expect(
            ",1,   ,b,x(,a(),).y().z(),,",
            ["", "1", "", "b", "x(,a(),).y().z()", "", ""],
        )

        expect("repeat(1, key(a))", ["repeat(1, key(a))"])
        expect(
            "repeat(1, key(a)), repeat(1, key(b))",
            ["repeat(1, key(a))", "repeat(1, key(b))"],
        )
        expect(
            "repeat(1, key(a)), repeat(1, key(b)), repeat(1, key(c))",
            ["repeat(1, key(a))", "repeat(1, key(b))", "repeat(1, key(c))"],
        )

        # will be parsed as None
        expect("", [""])
        expect(",", ["", ""])
        expect(",,", ["", "", ""])

    async def test_parse_params(self):
        def test(value, types):
            argument = Argument(
                ArgumentConfig(position=0, name="test", types=types),
                DummyMapping,
            )
            argument.initialize_variable(RawValue(value=value))
            return argument._variable

        self.assertEqual(
            test("", [None, int, float]),
            Variable(None, const=True),
        )

        # strings. If it is wrapped in quotes, don't parse the contents
        self.assertEqual(
            test('"foo"', [str]),
            Variable("foo", const=True),
        )
        self.assertEqual(
            test('"\tf o o\n"', [str]),
            Variable("\tf o o\n", const=True),
        )
        self.assertEqual(
            test('"foo(a,b)"', [str]),
            Variable("foo(a,b)", const=True),
        )
        self.assertEqual(
            test('",,,()"', [str]),
            Variable(",,,()", const=True),
        )

        # strings without quotes only work as long as there is no function call or
        # anything. This is only really acceptable for constants like KEY_A and for
        # variable names, which are not allowed to contain special characters that may
        # have a meaning in the macro syntax.
        self.assertEqual(
            test("foo", [str]),
            Variable("foo", const=True),
        )

        self.assertEqual(
            test("", [str, None]),
            Variable(None, const=True),
        )
        self.assertEqual(
            test("", [str]),
            Variable("", const=True),
        )
        self.assertEqual(
            test("", [None]),
            Variable(None, const=True),
        )
        self.assertEqual(
            test("None", [None]),
            Variable(None, const=True),
        )
        self.assertEqual(
            test('"None"', [str]),
            Variable("None", const=True),
        )

        self.assertEqual(
            test("5", [int]),
            Variable(5, const=True),
        )
        self.assertEqual(
            test("5", [float, int]),
            Variable(5, const=True),
        )
        self.assertEqual(
            test("5.2", [int, float]),
            Variable(5.2, const=True),
        )
        self.assertIsInstance(
            test("$foo", [str]),
            Variable,
        )
        self.assertEqual(
            test("$foo", [str]),
            Variable("foo", const=False),
        )

    async def test_raises_error(self):
        def expect_string_in_error(string: str, macro: str):
            with self.assertRaises(MacroError) as cm:
                Parser.parse(macro, self.context)
            error = str(cm.exception)
            self.assertIn(string, error)

        Parser.parse("k(1).h(k(a)).k(3)", self.context)  # No error
        expect_string_in_error("bracket", "key((1)")
        expect_string_in_error("bracket", "k(1))")
        self.assertRaises(MacroError, Parser.parse, "k((1).k)", self.context)
        self.assertRaises(MacroError, Parser.parse, "key(foo=a)", self.context)
        self.assertRaises(
            MacroError, Parser.parse, "key(symbol=a, foo=b)", self.context
        )
        self.assertRaises(MacroError, Parser.parse, "k()", self.context)
        self.assertRaises(MacroError, Parser.parse, "key(invalidkey)", self.context)
        self.assertRaises(MacroError, Parser.parse, 'key("invalidkey")', self.context)
        Parser.parse("key(1)", self.context)  # no error
        self.assertRaises(MacroError, Parser.parse, "k(1, 1)", self.context)
        Parser.parse("key($a)", self.context)  # no error
        self.assertRaises(MacroError, Parser.parse, "h(1, 1)", self.context)
        self.assertRaises(MacroError, Parser.parse, "h(hold(h(1, 1)))", self.context)
        self.assertRaises(MacroError, Parser.parse, "r(1)", self.context)
        self.assertRaises(MacroError, Parser.parse, "repeat(a, k(1))", self.context)
        Parser.parse("repeat($a, k(1))", self.context)  # no error
        Parser.parse("repeat(2, k(1))", self.context)  # no error
        self.assertRaises(MacroError, Parser.parse, 'repeat("2", k(1))', self.context)
        self.assertRaises(MacroError, Parser.parse, "r(1, 1)", self.context)
        self.assertRaises(MacroError, Parser.parse, "r(k(1), 1)", self.context)
        Parser.parse("r(1, macro=k(1))", self.context)  # no error
        self.assertRaises(MacroError, Parser.parse, "r(a=1, b=k(1))", self.context)
        self.assertRaises(
            MacroError,
            Parser.parse,
            "r(repeats=1, macro=k(1), a=2)",
            self.context,
        )
        self.assertRaises(
            MacroError,
            Parser.parse,
            "r(repeats=1, macro=k(1), repeats=2)",
            self.context,
        )
        self.assertRaises(MacroError, Parser.parse, "modify(asdf, k(a))", self.context)
        Parser.parse("if_tap(, k(a), 1000)", self.context)  # no error
        Parser.parse("if_tap(, k(a), timeout=1000)", self.context)  # no error
        Parser.parse("if_tap(, k(a), $timeout)", self.context)  # no error
        Parser.parse("if_tap(, k(a), timeout=$t)", self.context)  # no error
        Parser.parse("if_tap(, key(a))", self.context)  # no error
        Parser.parse("if_tap(k(a),)", self.context)  # no error
        self.assertRaises(MacroError, Parser.parse, "if_tap(k(a), b)", self.context)
        Parser.parse("if_single(k(a),)", self.context)  # no error
        self.assertRaises(MacroError, Parser.parse, "if_single(1,)", self.context)
        self.assertRaises(MacroError, Parser.parse, "if_single(,1)", self.context)
        Parser.parse("mouse(up, 3)", self.context)  # no error
        Parser.parse("mouse(up, speed=$a)", self.context)  # no error
        self.assertRaises(MacroError, Parser.parse, "mouse(3, up)", self.context)
        Parser.parse("wheel(left, 3)", self.context)  # no error
        self.assertRaises(MacroError, Parser.parse, "wheel(3, left)", self.context)
        Parser.parse("w(2)", self.context)  # no error
        self.assertRaises(MacroError, Parser.parse, "wait(a)", self.context)
        Parser.parse("ifeq(a, 2, k(a),)", self.context)  # no error
        Parser.parse("ifeq(a, 2, , k(a))", self.context)  # no error
        Parser.parse("ifeq(a, 2, None, k(a))", self.context)  # no error
        self.assertRaises(MacroError, Parser.parse, "ifeq(a, 2, 1,)", self.context)
        self.assertRaises(MacroError, Parser.parse, "ifeq(a, 2, , 2)", self.context)
        Parser.parse("if_eq(2, $a, k(a),)", self.context)  # no error
        Parser.parse("if_eq(2, $a, , else=k(a))", self.context)  # no error
        self.assertRaises(MacroError, Parser.parse, "if_eq(2, $a, 1,)", self.context)
        self.assertRaises(MacroError, Parser.parse, "if_eq(2, $a, , 2)", self.context)

        expect_string_in_error("blub", "if_eq(2, $a, key(a), blub=a)")

        expect_string_in_error("foo", "foo(a)")

        self.assertRaises(MacroError, Parser.parse, "set($a, 1)", self.context)
        self.assertRaises(MacroError, Parser.parse, "set(1, 2)", self.context)
        self.assertRaises(MacroError, Parser.parse, "set(+, 2)", self.context)
        self.assertRaises(MacroError, Parser.parse, "set(a(), 2)", self.context)
        self.assertRaises(MacroError, Parser.parse, "set('b,c', 2)", self.context)
        self.assertRaises(MacroError, Parser.parse, 'set("b,c", 2)', self.context)
        Parser.parse("set(A, 2)", self.context)  # no error

        self.assertRaises(MacroError, Parser.parse, "key(a)key(b)", self.context)
        self.assertRaises(MacroError, Parser.parse, "hold(key(a)key(b))", self.context)

        self.assertRaises(
            MacroError, Parser.parse, "hold_keys(a, broken, b)", self.context
        )

        Parser.parse("add(a, 1)", self.context)  # no error
        self.assertRaises(MacroError, Parser.parse, "add(a, b)", self.context)
        self.assertRaises(MacroError, Parser.parse, 'add(a, "1")', self.context)

        Parser.parse("if_capslock(else=key(KEY_A))", self.context)  # no error
        Parser.parse("if_capslock(key(KEY_A), None)", self.context)  # no error
        Parser.parse("if_capslock(key(KEY_A))", self.context)  # no error
        Parser.parse("if_capslock(then=key(KEY_A))", self.context)  # no error
        Parser.parse("if_numlock(else=key(KEY_A))", self.context)  # no error
        Parser.parse("if_numlock(key(KEY_A), None)", self.context)  # no error
        Parser.parse("if_numlock(key(KEY_A))", self.context)  # no error
        Parser.parse("if_numlock(then=key(KEY_A))", self.context)  # no error

        # wrong target for BTN_A
        self.assertRaises(
            SymbolNotAvailableInTargetError,
            Parser.parse,
            "key(BTN_A)",
            self.context,
            DummyMapping,
        )

        # passing a string parameter. This is not a macro, even though
        # it might look like it without the string quotes. Everything with
        # explicit quotes around it has to be treated as a string.
        self.assertRaises(MacroError, Parser.parse, '"modify(a, b)"', self.context)


@test_setup
class TestMacros(MacroTestBase):
    async def test_run_plus_syntax(self):
        macro = Parser.parse("a + b + c + d", self.context, DummyMapping)

        macro.press_trigger()
        asyncio.ensure_future(macro.run(self.handler))
        await asyncio.sleep(0.2)
        self.assertTrue(macro.tasks[0].is_holding())

        # starting from the left, presses each one down
        self.assertEqual(self.result[0], (EV_KEY, keyboard_layout.get("a"), 1))
        self.assertEqual(self.result[1], (EV_KEY, keyboard_layout.get("b"), 1))
        self.assertEqual(self.result[2], (EV_KEY, keyboard_layout.get("c"), 1))
        self.assertEqual(self.result[3], (EV_KEY, keyboard_layout.get("d"), 1))

        # and then releases starting with the previously pressed key
        macro.release_trigger()
        await asyncio.sleep(0.2)
        self.assertFalse(macro.tasks[0].is_holding())
        self.assertEqual(self.result[4], (EV_KEY, keyboard_layout.get("d"), 0))
        self.assertEqual(self.result[5], (EV_KEY, keyboard_layout.get("c"), 0))
        self.assertEqual(self.result[6], (EV_KEY, keyboard_layout.get("b"), 0))
        self.assertEqual(self.result[7], (EV_KEY, keyboard_layout.get("a"), 0))

    async def test_child_macro_count(self):
        # It correctly keeps track of child-macros for both positional and keyword-args
        macro = Parser.parse(
            "hold(macro=hold(hold())).repeat(1, macro=repeat(1, hold()))",
            self.context,
            DummyMapping,
            True,
        )
        self.assertEqual(self.count_child_macros(macro), 4)
        self.assertEqual(self.count_tasks(macro), 6)

    async def test_0(self):
        macro = Parser.parse("key(1)", self.context, DummyMapping, True)
        one_code = keyboard_layout.get("1")

        await macro.run(self.handler)
        self.assertListEqual(
            self.result,
            [(EV_KEY, one_code, 1), (EV_KEY, one_code, 0)],
        )
        self.assertEqual(self.count_child_macros(macro), 0)

    async def test_named_parameter(self):
        macro = Parser.parse("key(symbol=1)", self.context, DummyMapping, True)
        one_code = keyboard_layout.get("1")

        await macro.run(self.handler)
        self.assertListEqual(
            self.result,
            [(EV_KEY, one_code, 1), (EV_KEY, one_code, 0)],
        )
        self.assertEqual(self.count_child_macros(macro), 0)

    async def test_1(self):
        macro = Parser.parse('key(1).key("KEY_A").key(3)', self.context, DummyMapping)

        await macro.run(self.handler)
        self.assertListEqual(
            self.result,
            [
                (EV_KEY, keyboard_layout.get("1"), 1),
                (EV_KEY, keyboard_layout.get("1"), 0),
                (EV_KEY, keyboard_layout.get("a"), 1),
                (EV_KEY, keyboard_layout.get("a"), 0),
                (EV_KEY, keyboard_layout.get("3"), 1),
                (EV_KEY, keyboard_layout.get("3"), 0),
            ],
        )
        self.assertEqual(self.count_child_macros(macro), 0)

    async def test_key(self):
        code_a = keyboard_layout.get("a")
        code_b = keyboard_layout.get("b")
        macro = Parser.parse("set(foo, b).key($foo).key(a)", self.context, DummyMapping)
        await macro.run(self.handler)
        self.assertListEqual(
            self.result,
            [
                (EV_KEY, code_b, 1),
                (EV_KEY, code_b, 0),
                (EV_KEY, code_a, 1),
                (EV_KEY, code_a, 0),
            ],
        )

    async def test_key_down_up(self):
        code_a = keyboard_layout.get("a")
        code_b = keyboard_layout.get("b")
        macro = Parser.parse(
            "set(foo, b).key_down($foo).key_up($foo).key_up(a).key_down(a)",
            self.context,
            DummyMapping,
        )
        await macro.run(self.handler)
        self.assertListEqual(
            self.result,
            [
                (EV_KEY, code_b, 1),
                (EV_KEY, code_b, 0),
                (EV_KEY, code_a, 0),
                (EV_KEY, code_a, 1),
            ],
        )

    async def test_modify(self):
        code_a = keyboard_layout.get("a")
        code_b = keyboard_layout.get("b")
        code_c = keyboard_layout.get("c")
        macro = Parser.parse(
            "set(foo, b).modify($foo, modify(a, key(c)))",
            self.context,
            DummyMapping,
        )
        await macro.run(self.handler)
        self.assertListEqual(
            self.result,
            [
                (EV_KEY, code_b, 1),
                (EV_KEY, code_a, 1),
                (EV_KEY, code_c, 1),
                (EV_KEY, code_c, 0),
                (EV_KEY, code_a, 0),
                (EV_KEY, code_b, 0),
            ],
        )

    async def test_hold_variable(self):
        code_a = keyboard_layout.get("a")
        macro = Parser.parse("set(foo, a).hold($foo)", self.context, DummyMapping)
        await macro.run(self.handler)
        self.assertListEqual(
            self.result,
            [
                (EV_KEY, code_a, 1),
                (EV_KEY, code_a, 0),
            ],
        )

    async def test_hold_keys(self):
        macro = Parser.parse(
            "set(foo, b).hold_keys(a, $foo, c)", self.context, DummyMapping
        )
        # press first
        macro.press_trigger()
        # then run, just like how it is going to happen during runtime
        asyncio.ensure_future(macro.run(self.handler))

        code_a = keyboard_layout.get("a")
        code_b = keyboard_layout.get("b")
        code_c = keyboard_layout.get("c")

        await asyncio.sleep(0.2)
        self.assertListEqual(
            self.result,
            [
                (EV_KEY, code_a, 1),
                (EV_KEY, code_b, 1),
                (EV_KEY, code_c, 1),
            ],
        )

        macro.release_trigger()

        await asyncio.sleep(0.2)
        self.assertListEqual(
            self.result,
            [
                (EV_KEY, code_a, 1),
                (EV_KEY, code_b, 1),
                (EV_KEY, code_c, 1),
                (EV_KEY, code_c, 0),
                (EV_KEY, code_b, 0),
                (EV_KEY, code_a, 0),
            ],
        )

    async def test_hold_keys_broken(self):
        # Won't run any of the keys when one of them is invalid
        macro = Parser.parse(
            "set(foo, broken).hold_keys(a, $foo, c)", self.context, DummyMapping
        )
        # press first
        macro.press_trigger()
        # then run, just like how it is going to happen during runtime
        asyncio.ensure_future(macro.run(self.handler))
        await asyncio.sleep(0.2)
        self.assertListEqual(self.result, [])
        macro.release_trigger()
        await asyncio.sleep(0.2)
        self.assertListEqual(self.result, [])

    async def test_hold(self):
        # repeats key(a) as long as the key is held down
        macro = Parser.parse("key(1).hold(key(a)).key(3)", self.context, DummyMapping)

        """down"""

        macro.press_trigger()
        await asyncio.sleep(0.05)
        self.assertTrue(macro.tasks[1].is_holding())

        macro.press_trigger()  # redundantly calling doesn't break anything
        asyncio.ensure_future(macro.run(self.handler))
        await asyncio.sleep(0.2)
        self.assertTrue(macro.tasks[1].is_holding())
        self.assertGreater(len(self.result), 2)

        """up"""

        macro.release_trigger()
        await asyncio.sleep(0.05)
        self.assertFalse(macro.tasks[1].is_holding())

        self.assertEqual(self.result[0], (EV_KEY, keyboard_layout.get("1"), 1))
        self.assertEqual(self.result[-1], (EV_KEY, keyboard_layout.get("3"), 0))

        code_a = keyboard_layout.get("a")
        self.assertGreater(self.result.count((EV_KEY, code_a, 1)), 2)

        self.assertEqual(self.count_child_macros(macro), 1)
        self.assertEqual(self.count_tasks(macro), 4)

    async def test_hold_failing_child(self):
        # if a child macro fails, hold will not try to run it again.
        # The exception is properly propagated through both `hold`s and the macro
        # stops. If the code is broken, this test might enter an infinite loop.
        macro = Parser.parse("hold(hold(key(a)))", self.context, DummyMapping)

        class MyException(Exception):
            pass

        def f(*_):
            raise MyException("foo")

        macro.press_trigger()
        with self.assertRaises(MyException):
            await macro.run(f)

        await asyncio.sleep(0.1)
        self.assertFalse(macro.running)

    async def test_dont_hold(self):
        macro = Parser.parse("key(1).hold(key(a)).key(3)", self.context, DummyMapping)

        asyncio.ensure_future(macro.run(self.handler))
        await asyncio.sleep(0.2)
        self.assertFalse(macro.tasks[1].is_holding())
        # press_trigger was never called, so the macro completes right away
        # and the child macro of hold is never called.
        self.assertEqual(len(self.result), 4)

        self.assertEqual(self.result[0], (EV_KEY, keyboard_layout.get("1"), 1))
        self.assertEqual(self.result[-1], (EV_KEY, keyboard_layout.get("3"), 0))

        self.assertEqual(self.count_child_macros(macro), 1)
        self.assertEqual(self.count_tasks(macro), 4)

    async def test_just_hold(self):
        macro = Parser.parse("key(1).hold().key(3)", self.context, DummyMapping)

        """down"""

        macro.press_trigger()
        asyncio.ensure_future(macro.run(self.handler))
        await asyncio.sleep(0.1)
        self.assertTrue(macro.tasks[1].is_holding())
        self.assertEqual(len(self.result), 2)
        await asyncio.sleep(0.1)
        # doesn't do fancy stuff, is blocking until the release
        self.assertEqual(len(self.result), 2)

        """up"""

        macro.release_trigger()
        await asyncio.sleep(0.05)
        self.assertFalse(macro.tasks[1].is_holding())
        self.assertEqual(len(self.result), 4)

        self.assertEqual(self.result[0], (EV_KEY, keyboard_layout.get("1"), 1))
        self.assertEqual(self.result[-1], (EV_KEY, keyboard_layout.get("3"), 0))

        self.assertEqual(self.count_child_macros(macro), 0)
        self.assertEqual(self.count_tasks(macro), 3)

    async def test_dont_just_hold(self):
        macro = Parser.parse("key(1).hold().key(3)", self.context, DummyMapping)

        asyncio.ensure_future(macro.run(self.handler))
        await asyncio.sleep(0.1)
        self.assertFalse(macro.tasks[1].is_holding())
        # since press_trigger was never called it just does the macro
        # completely
        self.assertEqual(len(self.result), 4)

        self.assertEqual(self.result[0], (EV_KEY, keyboard_layout.get("1"), 1))
        self.assertEqual(self.result[-1], (EV_KEY, keyboard_layout.get("3"), 0))

        self.assertEqual(self.count_child_macros(macro), 0)

    async def test_hold_down(self):
        # writes down and waits for the up event until the key is released
        macro = Parser.parse("hold(a)", self.context, DummyMapping)
        self.assertEqual(self.count_child_macros(macro), 0)

        """down"""

        macro.press_trigger()
        await asyncio.sleep(0.05)
        self.assertTrue(macro.tasks[0].is_holding())

        asyncio.ensure_future(macro.run(self.handler))
        macro.press_trigger()  # redundantly calling doesn't break anything
        await asyncio.sleep(0.2)
        self.assertTrue(macro.tasks[0].is_holding())
        self.assertEqual(len(self.result), 1)
        self.assertEqual(self.result[0], (EV_KEY, keyboard_layout.get("a"), 1))

        """up"""

        macro.release_trigger()
        await asyncio.sleep(0.05)
        self.assertFalse(macro.tasks[0].is_holding())

        self.assertEqual(len(self.result), 2)
        self.assertEqual(self.result[0], (EV_KEY, keyboard_layout.get("a"), 1))
        self.assertEqual(self.result[1], (EV_KEY, keyboard_layout.get("a"), 0))

    async def test_aldjfakl(self):
        repeats = 5

        macro = Parser.parse(
            f"repeat({repeats}, key(k))",
            self.context,
            DummyMapping,
        )

        self.assertEqual(self.count_child_macros(macro), 1)

    async def test_2(self):
        start = time.time()
        repeats = 20

        macro = Parser.parse(
            f"repeat({repeats}, key(k)).repeat(1, key(k))",
            self.context,
            DummyMapping,
        )
        k_code = keyboard_layout.get("k")

        await macro.run(self.handler)
        keystroke_sleep = DummyMapping.macro_key_sleep_ms
        sleep_time = 2 * repeats * keystroke_sleep / 1000
        self.assertGreater(time.time() - start, sleep_time * 0.9)
        self.assertLess(time.time() - start, sleep_time * 1.3)

        self.assertListEqual(
            self.result,
            [(EV_KEY, k_code, 1), (EV_KEY, k_code, 0)] * (repeats + 1),
        )

        self.assertEqual(self.count_child_macros(macro), 2)

        self.assertEqual(len(macro.tasks[0].child_macros), 1)
        self.assertEqual(len(macro.tasks[0].child_macros[0].tasks), 1)
        self.assertEqual(len(macro.tasks[0].child_macros[0].tasks[0].child_macros), 0)

        self.assertEqual(len(macro.tasks[1].child_macros), 1)
        self.assertEqual(len(macro.tasks[1].child_macros[0].tasks), 1)
        self.assertEqual(len(macro.tasks[1].child_macros[0].tasks[0].child_macros), 0)

    async def test_3(self):
        start = time.time()
        macro = Parser.parse("repeat(3, key(m).w(100))", self.context, DummyMapping)
        m_code = keyboard_layout.get("m")
        await macro.run(self.handler)

        keystroke_time = 6 * DummyMapping.macro_key_sleep_ms
        total_time = keystroke_time + 300
        total_time /= 1000

        self.assertGreater(time.time() - start, total_time * 0.9)
        self.assertLess(time.time() - start, total_time * 1.2)
        self.assertListEqual(
            self.result,
            [
                (EV_KEY, m_code, 1),
                (EV_KEY, m_code, 0),
                (EV_KEY, m_code, 1),
                (EV_KEY, m_code, 0),
                (EV_KEY, m_code, 1),
                (EV_KEY, m_code, 0),
            ],
        )
        self.assertEqual(self.count_child_macros(macro), 1)
        self.assertEqual(len(macro.tasks), 1)
        self.assertEqual(len(macro.tasks[0].child_macros), 1)
        self.assertEqual(len(macro.tasks[0].child_macros[0].tasks), 2)
        self.assertEqual(len(macro.tasks[0].child_macros[0].tasks[0].child_macros), 0)
        self.assertEqual(len(macro.tasks[0].child_macros[0].tasks[1].child_macros), 0)

    async def test_4(self):
        macro = Parser.parse(
            "  repeat(2,\nkey(\nr ).key(minus\n )).key(m)  ",
            self.context,
            DummyMapping,
        )

        r = keyboard_layout.get("r")
        minus = keyboard_layout.get("minus")
        m = keyboard_layout.get("m")

        await macro.run(self.handler)
        self.assertListEqual(
            self.result,
            [
                (EV_KEY, r, 1),
                (EV_KEY, r, 0),
                (EV_KEY, minus, 1),
                (EV_KEY, minus, 0),
                (EV_KEY, r, 1),
                (EV_KEY, r, 0),
                (EV_KEY, minus, 1),
                (EV_KEY, minus, 0),
                (EV_KEY, m, 1),
                (EV_KEY, m, 0),
            ],
        )
        self.assertEqual(self.count_child_macros(macro), 1)
        self.assertEqual(self.count_tasks(macro), 4)

    async def test_5(self):
        start = time.time()
        macro = Parser.parse(
            "w(200).repeat(2,modify(w,\nrepeat(2,\tkey(BtN_LeFt))).w(10).key(k))",
            self.context,
            DummyMapping,
        )

        self.assertEqual(self.count_child_macros(macro), 3)
        self.assertEqual(self.count_tasks(macro), 7)

        w = keyboard_layout.get("w")
        left = keyboard_layout.get("bTn_lEfT")
        k = keyboard_layout.get("k")

        await macro.run(self.handler)

        num_pauses = 8 + 6 + 4
        keystroke_time = num_pauses * DummyMapping.macro_key_sleep_ms
        wait_time = 220
        total_time = (keystroke_time + wait_time) / 1000

        self.assertLess(time.time() - start, total_time * 1.2)
        self.assertGreater(time.time() - start, total_time * 0.9)
        expected = [(EV_KEY, w, 1)]
        expected += [(EV_KEY, left, 1), (EV_KEY, left, 0)] * 2
        expected += [(EV_KEY, w, 0)]
        expected += [(EV_KEY, k, 1), (EV_KEY, k, 0)]
        expected *= 2
        self.assertListEqual(self.result, expected)

    async def test_6(self):
        # does nothing without .run
        macro = Parser.parse("key(a).repeat(3, key(b))", self.context)
        self.assertIsInstance(macro, Macro)
        self.assertListEqual(self.result, [])

    async def test_duplicate_run(self):
        # it won't restart the macro, because that may screw up the
        # internal state (in particular the _trigger_release_event).
        # I actually don't know at all what kind of bugs that might produce,
        # lets just avoid it. It might cause it to be held down forever.
        a = keyboard_layout.get("a")
        b = keyboard_layout.get("b")
        c = keyboard_layout.get("c")

        macro = Parser.parse(
            "key(a).modify(b, hold()).key(c)", self.context, DummyMapping
        )
        asyncio.ensure_future(macro.run(self.handler))
        self.assertFalse(macro.tasks[1].child_macros[0].tasks[0].is_holding())

        asyncio.ensure_future(macro.run(self.handler))  # ignored
        self.assertFalse(macro.tasks[1].child_macros[0].tasks[0].is_holding())

        macro.press_trigger()
        await asyncio.sleep(0.2)
        self.assertTrue(macro.tasks[1].child_macros[0].tasks[0].is_holding())

        asyncio.ensure_future(macro.run(self.handler))  # ignored
        self.assertTrue(macro.tasks[1].child_macros[0].tasks[0].is_holding())

        macro.release_trigger()
        await asyncio.sleep(0.2)
        self.assertFalse(macro.tasks[1].child_macros[0].tasks[0].is_holding())

        expected = [
            (EV_KEY, a, 1),
            (EV_KEY, a, 0),
            (EV_KEY, b, 1),
            (EV_KEY, b, 0),
            (EV_KEY, c, 1),
            (EV_KEY, c, 0),
        ]
        self.assertListEqual(self.result, expected)

        """not ignored, since previous run is over"""

        asyncio.ensure_future(macro.run(self.handler))
        macro.press_trigger()
        await asyncio.sleep(0.2)
        self.assertTrue(macro.tasks[1].child_macros[0].tasks[0].is_holding())
        macro.release_trigger()
        await asyncio.sleep(0.2)
        self.assertFalse(macro.tasks[1].child_macros[0].tasks[0].is_holding())

        expected = [
            (EV_KEY, a, 1),
            (EV_KEY, a, 0),
            (EV_KEY, b, 1),
            (EV_KEY, b, 0),
            (EV_KEY, c, 1),
            (EV_KEY, c, 0),
        ] * 2
        self.assertListEqual(self.result, expected)

    async def test_mouse(self):
        wheel_speed = 60
        macro_1 = Parser.parse("mouse(up, 4)", self.context, DummyMapping)
        macro_2 = Parser.parse(
            f"wheel(left, {wheel_speed})", self.context, DummyMapping
        )
        macro_1.press_trigger()
        macro_2.press_trigger()
        asyncio.ensure_future(macro_1.run(self.handler))
        asyncio.ensure_future(macro_2.run(self.handler))

        sleep = 0.1
        await asyncio.sleep(sleep)
        self.assertTrue(macro_1.tasks[0].is_holding())
        self.assertTrue(macro_2.tasks[0].is_holding())
        macro_1.release_trigger()
        macro_2.release_trigger()

        self.assertIn((EV_REL, REL_Y, -4), self.result)
        expected_wheel_hi_res_event_count = sleep * DummyMapping.rel_rate
        expected_wheel_event_count = int(
            expected_wheel_hi_res_event_count / 120 * wheel_speed
        )
        actual_wheel_event_count = self.result.count((EV_REL, REL_HWHEEL, 1))
        actual_wheel_hi_res_event_count = self.result.count(
            (
                EV_REL,
                REL_HWHEEL_HI_RES,
                wheel_speed,
            )
        )
        # this seems to have a tendency of injecting less wheel events,
        # especially if the sleep is short
        self.assertGreater(actual_wheel_event_count, expected_wheel_event_count * 0.8)
        self.assertLess(actual_wheel_event_count, expected_wheel_event_count * 1.1)
        self.assertGreater(
            actual_wheel_hi_res_event_count, expected_wheel_hi_res_event_count * 0.8
        )
        self.assertLess(
            actual_wheel_hi_res_event_count, expected_wheel_hi_res_event_count * 1.1
        )

    async def test_mouse_accel(self):
        macro_1 = Parser.parse("mouse(up, 10, 0.9)", self.context, DummyMapping)
        macro_1.press_trigger()
        asyncio.ensure_future(macro_1.run(self.handler))

        sleep = 0.1
        await asyncio.sleep(sleep)
        self.assertTrue(macro_1.tasks[0].is_holding())
        macro_1.release_trigger()
        self.assertEqual(
            [(2, 1, 0), (2, 1, -2), (2, 1, -3), (2, 1, -4), (2, 1, -4), (2, 1, -5)],
            self.result,
        )

    async def test_event_1(self):
        macro = Parser.parse("e(EV_KEY, KEY_A, 1)", self.context, DummyMapping)
        a_code = keyboard_layout.get("a")

        await macro.run(self.handler)
        self.assertListEqual(self.result, [(EV_KEY, a_code, 1)])
        self.assertEqual(self.count_child_macros(macro), 0)

    async def test_event_2(self):
        macro = Parser.parse(
            "repeat(1, event(type=5421, code=324, value=154))",
            self.context,
            DummyMapping,
        )
        code = 324

        await macro.run(self.handler)
        self.assertListEqual(self.result, [(5421, code, 154)])
        self.assertEqual(self.count_child_macros(macro), 1)

    async def test_macro_breaks(self):
        # the first parameter for `repeat` requires an integer, not "foo",
        # which makes `repeat` throw
        macro = Parser.parse(
            'set(a, "foo").repeat($a, key(KEY_A)).key(KEY_B)',
            self.context,
            DummyMapping,
        )

        try:
            await macro.run(self.handler)
        except MacroError as e:
            self.assertIn("foo", str(e))

        self.assertFalse(macro.running)

        # key(KEY_B) is not executed, the macro stops
        self.assertListEqual(self.result, [])

    async def test_set(self):
        """await Parser.parse('set(a, "foo")', self.context, DummyMapping).run(self.handler)
        self.assertEqual(macro_variables.get("a"), "foo")

        await Parser.parse('set( \t"b" \n, "1")', self.context, DummyMapping).run(self.handler)
        self.assertEqual(macro_variables.get("b"), "1")"""

        await Parser.parse("set(a, 1)", self.context, DummyMapping).run(self.handler)
        self.assertEqual(macro_variables.get("a"), 1)

        """await Parser.parse("set(a, )", self.context, DummyMapping).run(self.handler)
        self.assertEqual(macro_variables.get("a"), None)"""

    async def test_add(self):
        await Parser.parse("set(a, 1).add(a, 1)", self.context, DummyMapping).run(
            self.handler
        )
        self.assertEqual(macro_variables.get("a"), 2)

        await Parser.parse("set(b, 1).add(b, -1)", self.context, DummyMapping).run(
            self.handler
        )
        self.assertEqual(macro_variables.get("b"), 0)

        await Parser.parse("set(c, -1).add(c, 500)", self.context, DummyMapping).run(
            self.handler
        )
        self.assertEqual(macro_variables.get("c"), 499)

        await Parser.parse("add(d, 500)", self.context, DummyMapping).run(self.handler)
        self.assertEqual(macro_variables.get("d"), 500)

    async def test_add_invalid(self):
        # For invalid input it should do nothing (except to log to the console)
        await Parser.parse('set(e, "foo").add(e, 1)', self.context, DummyMapping).run(
            self.handler
        )
        self.assertEqual(macro_variables.get("e"), "foo")

        await Parser.parse('set(e, "2").add(e, 3)', self.context, DummyMapping).run(
            self.handler
        )
        self.assertEqual(macro_variables.get("e"), "2")

    async def test_multiline_macro_and_comments(self):
        # the parser is not confused by the code in the comments and can use hashtags
        # in strings in the actual code
        comment = '# repeat(1,key(KEY_D)).set(a,"#b")'
        macro = Parser.parse(
            f"""
            {comment}
            key(KEY_A).{comment}
            key(KEY_B). {comment}
            repeat({comment}
                1, {comment}
                key(KEY_C){comment}
            ). {comment}
            {comment}
            set(a, "#").{comment}
            if_eq($a, "#", key(KEY_E), key(KEY_F)) {comment}
            {comment}
        """,
            self.context,
            DummyMapping,
        )
        await macro.run(self.handler)
        self.assertListEqual(
            self.result,
            [
                (EV_KEY, KEY_A, 1),
                (EV_KEY, KEY_A, 0),
                (EV_KEY, KEY_B, 1),
                (EV_KEY, KEY_B, 0),
                (EV_KEY, KEY_C, 1),
                (EV_KEY, KEY_C, 0),
                (EV_KEY, KEY_E, 1),
                (EV_KEY, KEY_E, 0),
            ],
        )


class TestDynamicTypes(MacroTestBase):
    # "Dynamic" meaning const=False
    async def test_set_type_int(self):
        await Parser.parse(
            "set(a, 1)",
            self.context,
            DummyMapping,
            True,
        ).run(lambda *_, **__: None)
        self.assertEqual(macro_variables.get("a"), 1)
        # assertEqual(1.0, 1) passes, so check for the type to be sure:
        self.assertIsInstance(macro_variables.get("a"), int)

    async def test_set_type_float(self):
        await Parser.parse(
            "set(a, 2.2)",
            self.context,
            DummyMapping,
            True,
        ).run(lambda *_, **__: None)
        self.assertEqual(macro_variables.get("a"), 2.2)

    async def test_set_type_str(self):
        await Parser.parse(
            'set(a, "3")',
            self.context,
            DummyMapping,
            True,
        ).run(lambda *_, **__: None)
        self.assertEqual(macro_variables.get("a"), "3")

    def make_test_task(self, types):
        # Make a new test task, with a different types array each time.
        class TestTask(Task):
            argument_configs = [
                ArgumentConfig(
                    name="testvalue",
                    position=0,
                    types=types,
                )
            ]

        return TestTask(
            [RawValue("$a")],
            {},
            self.context,
            DummyMapping,
        )

    async def test_dynamic_int_parsing(self):
        # set(a, 4) was used. Could be meant as an integer, or as a string
        # (just like how key(KEY_A) doesn't require string quotes to be a string)
        macro_variables["a"] = 4

        test_task = self.make_test_task([str, int])
        self.assertEqual(test_task.get_argument("testvalue").get_value(), 4)

        test_task = self.make_test_task([int])
        self.assertEqual(test_task.get_argument("testvalue").get_value(), 4)

        # Now that ints are not allowed, it will be used as a string
        test_task = self.make_test_task([str])
        self.assertEqual(test_task.get_argument("testvalue").get_value(), "4")

    async def test_dynamic_float_parsing(self):
        # set(a, 5.5) was used.
        macro_variables["a"] = 5.5

        test_task = self.make_test_task([str, float])
        self.assertEqual(test_task.get_argument("testvalue").get_value(), 5.5)

        test_task = self.make_test_task([float])
        self.assertEqual(test_task.get_argument("testvalue").get_value(), 5.5)

        test_task = self.make_test_task([str])
        self.assertEqual(test_task.get_argument("testvalue").get_value(), "5.5")

    async def test_no_float_allowed(self):
        # set(a, 6.6) was used.
        macro_variables["a"] = 6.6

        test_task = self.make_test_task([str, int])
        self.assertEqual(test_task.get_argument("testvalue").get_value(), "6.6")

        test_task = self.make_test_task([int])
        self.assertRaises(
            MacroError,
            lambda: test_task.get_argument("testvalue").get_value(),
        )

    async def test_force_string_float(self):
        # set(a, "7.7") was used. Since quotes are explicitly added, the variable is
        # not intended to be used as a float.
        macro_variables["a"] = "7.7"

        test_task = self.make_test_task([str, float])
        self.assertEqual(test_task.get_argument("testvalue").get_value(), "7.7")

        test_task = self.make_test_task([float])
        self.assertRaises(
            MacroError,
            lambda: test_task.get_argument("testvalue").get_value(),
        )

    async def test_force_string_int(self):
        # set(a, "8") was used.
        macro_variables["a"] = "8"

        test_task = self.make_test_task([int, str])
        self.assertEqual(test_task.get_argument("testvalue").get_value(), "8")

        test_task = self.make_test_task([int])
        self.assertRaises(
            MacroError,
            lambda: test_task.get_argument("testvalue").get_value(),
        )


@test_setup
class TestLeds(MacroTestBase):
    async def test_if_capslock(self):
        macro = Parser.parse(
            "if_capslock(key(KEY_1), key(KEY_2))",
            self.context,
            DummyMapping,
            True,
        )
        self.assertEqual(self.count_child_macros(macro), 2)

        with patch.object(self.source_device, "leds", side_effect=lambda: [LED_NUML]):
            await macro.run(self.handler)
            self.assertListEqual(self.result, [(EV_KEY, KEY_2, 1), (EV_KEY, KEY_2, 0)])

        with patch.object(self.source_device, "leds", side_effect=lambda: [LED_CAPSL]):
            self.result = []
            await macro.run(self.handler)
            self.assertListEqual(self.result, [(EV_KEY, KEY_1, 1), (EV_KEY, KEY_1, 0)])

    async def test_if_numlock(self):
        macro = Parser.parse(
            "if_numlock(key(KEY_1), key(KEY_2))",
            self.context,
            DummyMapping,
            True,
        )
        self.assertEqual(self.count_child_macros(macro), 2)

        with patch.object(self.source_device, "leds", side_effect=lambda: [LED_NUML]):
            await macro.run(self.handler)
            self.assertListEqual(self.result, [(EV_KEY, KEY_1, 1), (EV_KEY, KEY_1, 0)])

        with patch.object(self.source_device, "leds", side_effect=lambda: [LED_CAPSL]):
            self.result = []
            await macro.run(self.handler)
            self.assertListEqual(self.result, [(EV_KEY, KEY_2, 1), (EV_KEY, KEY_2, 0)])

    async def test_if_numlock_no_else(self):
        macro = Parser.parse(
            "if_numlock(key(KEY_1))",
            self.context,
            DummyMapping,
            True,
        )
        self.assertEqual(self.count_child_macros(macro), 1)

        with patch.object(self.source_device, "leds", side_effect=lambda: [LED_CAPSL]):
            await macro.run(self.handler)
            self.assertListEqual(self.result, [])

        with patch.object(self.source_device, "leds", side_effect=lambda: [LED_NUML]):
            self.result = []
            await macro.run(self.handler)
            self.assertListEqual(self.result, [(EV_KEY, KEY_1, 1), (EV_KEY, KEY_1, 0)])

    async def test_if_capslock_no_then(self):
        macro = Parser.parse(
            "if_capslock(None, key(KEY_1))",
            self.context,
            DummyMapping,
            True,
        )
        self.assertEqual(self.count_child_macros(macro), 1)

        with patch.object(self.source_device, "leds", side_effect=lambda: [LED_CAPSL]):
            await macro.run(self.handler)
            self.assertListEqual(self.result, [])

        with patch.object(self.source_device, "leds", side_effect=lambda: [LED_NUML]):
            self.result = []
            await macro.run(self.handler)
            self.assertListEqual(self.result, [(EV_KEY, KEY_1, 1), (EV_KEY, KEY_1, 0)])


@test_setup
class TestIfEq(MacroTestBase):
    async def test_ifeq_runs(self):
        # deprecated ifeq function, but kept for compatibility reasons
        macro = Parser.parse(
            "set(foo, 2).ifeq(foo, 2, key(a), key(b))",
            self.context,
            DummyMapping,
        )
        code_a = keyboard_layout.get("a")
        code_b = keyboard_layout.get("b")

        await macro.run(self.handler)
        self.assertListEqual(self.result, [(EV_KEY, code_a, 1), (EV_KEY, code_a, 0)])
        self.assertEqual(self.count_child_macros(macro), 2)

    async def test_ifeq_none(self):
        code_a = keyboard_layout.get("a")

        # first param None
        macro = Parser.parse(
            "set(foo, 2).ifeq(foo, 2, None, key(b))", self.context, DummyMapping
        )
        self.assertEqual(self.count_child_macros(macro), 1)
        await macro.run(self.handler)
        self.assertListEqual(self.result, [])

        # second param None
        self.result = []
        macro = Parser.parse(
            "set(foo, 2).ifeq(foo, 2, key(a), None)", self.context, DummyMapping
        )
        self.assertEqual(self.count_child_macros(macro), 1)
        await macro.run(self.handler)
        self.assertListEqual(self.result, [(EV_KEY, code_a, 1), (EV_KEY, code_a, 0)])

        """Old syntax, use None instead"""

        # first param ""
        self.result = []
        macro = Parser.parse(
            "set(foo, 2).ifeq(foo, 2, , key(b))", self.context, DummyMapping
        )
        self.assertEqual(self.count_child_macros(macro), 1)
        await macro.run(self.handler)
        self.assertListEqual(self.result, [])

        # second param ""
        self.result = []
        macro = Parser.parse(
            "set(foo, 2).ifeq(foo, 2, key(a), )", self.context, DummyMapping
        )
        self.assertEqual(self.count_child_macros(macro), 1)
        await macro.run(self.handler)
        self.assertListEqual(self.result, [(EV_KEY, code_a, 1), (EV_KEY, code_a, 0)])

    async def test_ifeq_unknown_key(self):
        macro = Parser.parse("ifeq(qux, 2, key(a), key(b))", self.context, DummyMapping)
        code_a = keyboard_layout.get("a")
        code_b = keyboard_layout.get("b")

        await macro.run(self.handler)
        self.assertListEqual(self.result, [(EV_KEY, code_b, 1), (EV_KEY, code_b, 0)])
        self.assertEqual(self.count_child_macros(macro), 2)

    async def test_if_eq(self):
        """new version of ifeq"""
        code_a = keyboard_layout.get("a")
        code_b = keyboard_layout.get("b")
        a_press = [(EV_KEY, code_a, 1), (EV_KEY, code_a, 0)]
        b_press = [(EV_KEY, code_b, 1), (EV_KEY, code_b, 0)]

        async def test(macro, expected):
            """Run the macro and compare the injections with an expectation."""
            logger.info("Testing %s", macro)
            # cleanup
            macro_variables._clear()
            self.assertIsNone(macro_variables.get("a"))
            self.result.clear()

            # test
            macro = Parser.parse(macro, self.context, DummyMapping)
            await macro.run(self.handler)
            self.assertListEqual(self.result, expected)

        await test("if_eq(1, 1, key(a), key(b))", a_press)
        await test("if_eq(1, 2, key(a), key(b))", b_press)
        await test("if_eq(value_1=1, value_2=1, then=key(a), else=key(b))", a_press)
        await test('set(a, "foo").if_eq($a, "foo", key(a), key(b))', a_press)
        await test('set(a, "foo").if_eq("foo", $a, key(a), key(b))', a_press)
        await test('set(a, "foo").if_eq("foo", $a, , key(b))', [])
        await test('set(a, "foo").if_eq("foo", $a, None, key(b))', [])
        await test('set(a, "qux").if_eq("foo", $a, key(a), key(b))', b_press)
        await test('set(a, "qux").if_eq($a, "foo", key(a), key(b))', b_press)
        await test('set(a, "qux").if_eq($a, "foo", key(a), )', [])
        await test('set(a, "x").set(b, "y").if_eq($b, $a, key(a), key(b))', b_press)
        await test('set(a, "x").set(b, "y").if_eq($b, $a, key(a), )', [])
        await test('set(a, "x").set(b, "y").if_eq($b, $a, key(a), None)', [])
        await test('set(a, "x").set(b, "y").if_eq($b, $a, key(a), else=None)', [])
        await test('set(a, "x").set(b, "x").if_eq($b, $a, key(a), key(b))', a_press)
        await test('set(a, "x").set(b, "x").if_eq($b, $a, , key(b))', [])
        await test("if_eq($q, $w, key(a), else=key(b))", a_press)  # both None
        await test("set(q, 1).if_eq($q, $w, key(a), else=key(b))", b_press)
        await test("set(q, 1).set(w, 1).if_eq($q, $w, key(a), else=key(b))", a_press)
        await test('set(q, " a b ").if_eq($q, " a b ", key(a), key(b))', a_press)
        await test('if_eq("\t", "\n", key(a), key(b))', b_press)

        # treats values in quotes as strings, not as code
        await test('set(q, "$a").if_eq($q, "$a", key(a), key(b))', a_press)
        await test('set(q, "a,b").if_eq("a,b", $q, key(a), key(b))', a_press)
        await test('set(q, "c(1, 2)").if_eq("c(1, 2)", $q, key(a), key(b))', a_press)
        await test('set(q, "c(1, 2)").if_eq("c(1, 2)", "$q", key(a), key(b))', b_press)
        await test('if_eq("value_1=1", 1, key(a), key(b))', b_press)

        # won't compare strings and int, be similar to python
        await test('set(a, "1").if_eq($a, 1, key(a), key(b))', b_press)
        await test('set(a, 1).if_eq($a, "1", key(a), key(b))', b_press)

    async def test_if_eq_runs_multiprocessed(self):
        """ifeq on variables that have been set in other processes works."""
        macro = Parser.parse(
            "if_eq($foo, 3, key(a), key(b))", self.context, DummyMapping
        )
        code_a = keyboard_layout.get("a")
        code_b = keyboard_layout.get("b")

        self.assertEqual(self.count_child_macros(macro), 2)

        def set_foo(value):
            # will write foo = 2 into the shared dictionary of macros
            macro_2 = Parser.parse(f"set(foo, {value})", self.context, DummyMapping)
            loop = asyncio.new_event_loop()
            loop.run_until_complete(macro_2.run(lambda: None))

        """foo is not 3"""

        process = multiprocessing.Process(target=set_foo, args=(2,))
        process.start()
        process.join()
        await macro.run(self.handler)
        self.assertListEqual(self.result, [(EV_KEY, code_b, 1), (EV_KEY, code_b, 0)])

        """foo is 3"""

        process = multiprocessing.Process(target=set_foo, args=(3,))
        process.start()
        process.join()
        await macro.run(self.handler)
        self.assertListEqual(
            self.result,
            [
                (EV_KEY, code_b, 1),
                (EV_KEY, code_b, 0),
                (EV_KEY, code_a, 1),
                (EV_KEY, code_a, 0),
            ],
        )


@test_setup
class TestWait(MacroTestBase):
    async def assert_time_randomized(
        self,
        macro: Macro,
        min_: float,
        max_: float,
    ):
        for _ in range(100):
            start = time.time()
            await macro.run(self.handler)
            time_taken = time.time() - start

            # Any of the runs should be within the defined range, to prove that they
            # are indeed random.
            if min_ < time_taken < max_:
                return

        raise AssertionError("`wait` was not randomized")

    async def test_wait_1_core(self):
        mapping = DummyMapping()
        mapping.macro_key_sleep_ms = 0
        macro = Parser.parse("repeat(5, wait(50))", self.context, mapping, True)

        start = time.time()
        await macro.run(self.handler)
        time_per_iteration = (time.time() - start) / 5

        self.assertLess(abs(time_per_iteration - 0.05), 0.005)

    async def test_wait_2_ranged(self):
        mapping = DummyMapping()
        mapping.macro_key_sleep_ms = 0
        macro = Parser.parse("wait(1, 100)", self.context, mapping, True)
        await self.assert_time_randomized(macro, 0.02, 0.08)

    async def test_wait_3_ranged_single_get(self):
        mapping = DummyMapping()
        mapping.macro_key_sleep_ms = 0
        macro = Parser.parse("set(a, 100).wait(1, $a)", self.context, mapping, True)
        await self.assert_time_randomized(macro, 0.02, 0.08)

    async def test_wait_4_ranged_double_get(self):
        mapping = DummyMapping()
        mapping.macro_key_sleep_ms = 0
        macro = Parser.parse(
            "set(a, 1).set(b, 100).wait($a, $b)", self.context, mapping, True
        )
        await self.assert_time_randomized(macro, 0.02, 0.08)


@test_setup
class TestIfSingle(MacroTestBase):
    async def test_if_single(self):
        macro = Parser.parse("if_single(key(x), key(y))", self.context, DummyMapping)
        self.assertEqual(self.count_child_macros(macro), 2)

        a = keyboard_layout.get("a")
        x = keyboard_layout.get("x")

        await self.trigger_sequence(macro, InputEvent.key(a, 1))
        await asyncio.sleep(0.1)
        await self.release_sequence(macro, InputEvent.key(a, 0))
        # the key that triggered the macro is released
        await asyncio.sleep(0.1)

        self.assertListEqual(self.result, [(EV_KEY, x, 1), (EV_KEY, x, 0)])
        self.assertFalse(macro.running)

    async def test_if_single_ignores_releases(self):
        # the timeout won't break the macro, everything happens well within that
        # timeframe.
        macro = Parser.parse(
            "if_single(key(x), else=key(y), timeout=100000)",
            self.context,
            DummyMapping,
        )
        self.assertEqual(self.count_child_macros(macro), 2)

        a = keyboard_layout.get("a")
        b = keyboard_layout.get("b")

        x = keyboard_layout.get("x")
        y = keyboard_layout.get("y")

        # pressing the macro key
        await self.trigger_sequence(macro, InputEvent.key(a, 1))
        await asyncio.sleep(0.05)

        # if_single only looks out for newly pressed keys,
        # it doesn't care if keys were released that have been
        # pressed before if_single. This was decided because it is a lot
        # less tricky and more fluently to use if you type fast
        for listener in self.context.listeners:
            asyncio.ensure_future(listener(InputEvent.key(b, 0)))
        await asyncio.sleep(0.05)
        self.assertListEqual(self.result, [])

        # releasing the actual key triggers if_single
        await asyncio.sleep(0.05)
        await self.release_sequence(macro, InputEvent.key(a, 0))
        await asyncio.sleep(0.05)
        self.assertListEqual(self.result, [(EV_KEY, x, 1), (EV_KEY, x, 0)])
        self.assertFalse(macro.running)

    async def test_if_not_single(self):
        # Will run the `else` macro if another key is pressed.
        # Also works if if_single is a child macro, i.e. the event is passed to it
        # from the outside macro correctly.
        macro = Parser.parse(
            "repeat(1, if_single(then=key(x), else=key(y)))",
            self.context,
            DummyMapping,
        )
        self.assertEqual(self.count_child_macros(macro), 3)
        self.assertEqual(self.count_tasks(macro), 4)

        a = keyboard_layout.get("a")
        b = keyboard_layout.get("b")

        x = keyboard_layout.get("x")
        y = keyboard_layout.get("y")

        # press the trigger key
        await self.trigger_sequence(macro, InputEvent.key(a, 1))
        await asyncio.sleep(0.1)
        # press another key
        for listener in self.context.listeners:
            asyncio.ensure_future(listener(InputEvent.key(b, 1)))
        await asyncio.sleep(0.1)

        self.assertListEqual(self.result, [(EV_KEY, y, 1), (EV_KEY, y, 0)])
        self.assertFalse(macro.running)

    async def test_if_not_single_none(self):
        macro = Parser.parse("if_single(key(x),)", self.context, DummyMapping)
        self.assertEqual(self.count_child_macros(macro), 1)

        a = keyboard_layout.get("a")
        b = keyboard_layout.get("b")

        x = keyboard_layout.get("x")

        # press trigger key
        await self.trigger_sequence(macro, InputEvent.key(a, 1))
        await asyncio.sleep(0.1)
        # press another key
        for listener in self.context.listeners:
            asyncio.ensure_future(listener(InputEvent.key(b, 1)))
        await asyncio.sleep(0.1)

        self.assertListEqual(self.result, [])
        self.assertFalse(macro.running)

    async def test_if_single_times_out(self):
        macro = Parser.parse(
            "set(t, 300).if_single(key(x), key(y), timeout=$t)",
            self.context,
            DummyMapping,
        )
        self.assertEqual(self.count_child_macros(macro), 2)

        a = keyboard_layout.get("a")
        y = keyboard_layout.get("y")

        await self.trigger_sequence(macro, InputEvent.key(a, 1))

        # no timeout yet
        await asyncio.sleep(0.2)
        self.assertListEqual(self.result, [])
        self.assertTrue(macro.running)

        # times out now
        await asyncio.sleep(0.2)
        self.assertListEqual(self.result, [(EV_KEY, y, 1), (EV_KEY, y, 0)])
        self.assertFalse(macro.running)

    async def test_if_single_ignores_joystick(self):
        """Triggers else + delayed_handle_keycode."""
        # Integration test style for if_single.
        # If a joystick that is mapped to a button is moved, if_single stops
        macro = Parser.parse(
            "if_single(k(a), k(KEY_LEFTSHIFT))", self.context, DummyMapping
        )
        code_shift = keyboard_layout.get("KEY_LEFTSHIFT")
        code_a = keyboard_layout.get("a")
        trigger = 1

        await self.trigger_sequence(macro, InputEvent.key(trigger, 1))
        await asyncio.sleep(0.1)
        for listener in self.context.listeners:
            asyncio.ensure_future(listener(InputEvent.abs(ABS_Y, 10)))
        await asyncio.sleep(0.1)
        await self.release_sequence(macro, InputEvent.key(trigger, 0))
        await asyncio.sleep(0.1)
        self.assertFalse(macro.running)
        self.assertListEqual(self.result, [(EV_KEY, code_a, 1), (EV_KEY, code_a, 0)])


@test_setup
class TestIfTap(MacroTestBase):
    async def test_if_tap(self):
        macro = Parser.parse("if_tap(key(x), key(y), 100)", self.context, DummyMapping)
        self.assertEqual(self.count_child_macros(macro), 2)

        x = keyboard_layout.get("x")
        y = keyboard_layout.get("y")

        # this is the regular routine of how a macro is started. the tigger is pressed
        # already when the macro runs, and released during if_tap within the timeout.
        macro.press_trigger()
        asyncio.ensure_future(macro.run(self.handler))
        await asyncio.sleep(0.05)
        macro.release_trigger()
        await asyncio.sleep(0.05)

        self.assertListEqual(self.result, [(EV_KEY, x, 1), (EV_KEY, x, 0)])
        self.assertFalse(macro.running)

    async def test_if_tap_2(self):
        # when the press arrives shortly after run.
        # a tap will happen within the timeout even if the tigger is not pressed when
        # it does into if_tap
        macro = Parser.parse("if_tap(key(a), key(b), 100)", self.context, DummyMapping)
        asyncio.ensure_future(macro.run(self.handler))

        await asyncio.sleep(0.01)
        macro.press_trigger()
        await asyncio.sleep(0.01)
        macro.release_trigger()
        await asyncio.sleep(0.2)
        self.assertListEqual(self.result, [(EV_KEY, KEY_A, 1), (EV_KEY, KEY_A, 0)])
        self.assertFalse(macro.running)
        self.result.clear()

    async def test_if_double_tap(self):
        macro = Parser.parse(
            "if_tap(if_tap(key(a), key(b), 100), key(c), 100)",
            self.context,
            DummyMapping,
        )
        self.assertEqual(self.count_child_macros(macro), 4)
        self.assertEqual(self.count_tasks(macro), 5)

        asyncio.ensure_future(macro.run(self.handler))

        # first tap
        macro.press_trigger()
        await asyncio.sleep(0.05)
        macro.release_trigger()

        # second tap
        await asyncio.sleep(0.04)
        macro.press_trigger()
        await asyncio.sleep(0.04)
        macro.release_trigger()

        await asyncio.sleep(0.05)
        self.assertListEqual(self.result, [(EV_KEY, KEY_A, 1), (EV_KEY, KEY_A, 0)])
        self.assertFalse(macro.running)
        self.result.clear()

        """If the second tap takes too long, runs else there"""

        asyncio.ensure_future(macro.run(self.handler))

        # first tap
        macro.press_trigger()
        await asyncio.sleep(0.05)
        macro.release_trigger()

        # second tap
        await asyncio.sleep(0.06)
        macro.press_trigger()
        await asyncio.sleep(0.06)
        macro.release_trigger()

        await asyncio.sleep(0.05)
        self.assertListEqual(self.result, [(EV_KEY, KEY_B, 1), (EV_KEY, KEY_B, 0)])
        self.assertFalse(macro.running)
        self.result.clear()

    async def test_if_tap_none(self):
        # first param none
        macro = Parser.parse("if_tap(, key(y), 100)", self.context, DummyMapping)
        self.assertEqual(self.count_child_macros(macro), 1)
        y = keyboard_layout.get("y")
        macro.press_trigger()
        asyncio.ensure_future(macro.run(self.handler))
        await asyncio.sleep(0.05)
        macro.release_trigger()
        await asyncio.sleep(0.05)
        self.assertListEqual(self.result, [])

        # second param none
        macro = Parser.parse("if_tap(key(y), , 50)", self.context, DummyMapping)
        self.assertEqual(self.count_child_macros(macro), 1)
        y = keyboard_layout.get("y")
        macro.press_trigger()
        asyncio.ensure_future(macro.run(self.handler))
        await asyncio.sleep(0.1)
        macro.release_trigger()
        await asyncio.sleep(0.05)
        self.assertListEqual(self.result, [])

        self.assertFalse(macro.running)

    async def test_if_not_tap(self):
        macro = Parser.parse("if_tap(key(x), key(y), 50)", self.context, DummyMapping)
        self.assertEqual(self.count_child_macros(macro), 2)

        y = keyboard_layout.get("y")

        macro.press_trigger()
        asyncio.ensure_future(macro.run(self.handler))
        await asyncio.sleep(0.1)
        macro.release_trigger()
        await asyncio.sleep(0.05)

        self.assertListEqual(self.result, [(EV_KEY, y, 1), (EV_KEY, y, 0)])
        self.assertFalse(macro.running)

    async def test_if_not_tap_named(self):
        macro = Parser.parse(
            "if_tap(key(x), key(y), timeout=50)", self.context, DummyMapping
        )
        self.assertEqual(self.count_child_macros(macro), 2)

        x = keyboard_layout.get("x")
        y = keyboard_layout.get("y")

        macro.press_trigger()
        asyncio.ensure_future(macro.run(self.handler))
        await asyncio.sleep(0.1)
        macro.release_trigger()
        await asyncio.sleep(0.05)

        self.assertListEqual(self.result, [(EV_KEY, y, 1), (EV_KEY, y, 0)])
        self.assertFalse(macro.running)


if __name__ == "__main__":
    unittest.main()
