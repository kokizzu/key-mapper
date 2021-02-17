#!/usr/bin/python3
# -*- coding: utf-8 -*-
# key-mapper - GUI for device specific keyboard mappings
# Copyright (C) 2021 sezanzeb <proxima@hip70890b.de>
#
# This file is part of key-mapper.
#
# key-mapper is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# key-mapper is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with key-mapper.  If not, see <https://www.gnu.org/licenses/>.


"""Create some singleton objects that are needed for the app to work."""


import re
import json
import subprocess
import evdev

from keymapper.logger import logger
from keymapper.mapping import Mapping, DISABLE_NAME, DISABLE_CODE
from keymapper.paths import get_config_path, touch, USER


# xkb uses keycodes that are 8 higher than those from evdev
XKB_KEYCODE_OFFSET = 8

XMODMAP_FILENAME = 'xmodmap.json'


class SystemMapping:
    """Stores information about all available keycodes."""
    def __init__(self):
        """Construct the system_mapping."""
        self._mapping = {}  # str to int
        self._allocated_unknowns = {}  # int to str  # TODO test

        # this may contain more entries than _mapping, since _mapping
        # stores only one code per character, but a keyboard layout can
        # have one character mapped to multiple keys.
        self._occupied_keycodes = set()  # TODO test

        self.populate()

    def list_names(self):
        """Return an array of all possible names in the mapping."""
        return self._mapping.keys()

    def populate(self):
        """Get a mapping of all available names to their keycodes."""
        logger.debug('Gathering available keycodes')
        self.clear()
        xmodmap_dict = {}
        try:
            xmodmap = subprocess.check_output(
                ['xmodmap', '-pke'],
                stderr=subprocess.STDOUT
            ).decode()
            xmodmap = xmodmap.lower()
            print(xmodmap)
            mappings = re.findall(r'(\d+) = (.+)\n', xmodmap + '\n')
            for keycode, names in mappings:
                # there might be multiple, like:
                # keycode  64 = Alt_L Meta_L Alt_L Meta_L
                # keycode 204 = NoSymbol Alt_L NoSymbol Alt_L
                # Alt_L should map to code 64. Writing code 204 only works
                # if a modifier is applied at the same time. So take the first
                # one.
                name = names.split()[0]
                if name == 'nosymbol':
                    # TODO test
                    continue

                keycode = int(keycode) - XKB_KEYCODE_OFFSET
                xmodmap_dict[name] = keycode
                self._occupied_keycodes.add(keycode)  # TODO what about loaded mappings of the daemon?

            for keycode, names in mappings:
                # but since KP may be mapped like KP_Home KP_7 KP_Home KP_7,
                # make another pass and add all of them if they don't already
                # exist. don't overwrite any keycodes.
                for name in names.split():
                    if xmodmap_dict.get(name) is None:
                        xmodmap_dict[name] = int(keycode) - XKB_KEYCODE_OFFSET

        except (subprocess.CalledProcessError, FileNotFoundError):
            # might be within a tty
            pass

        if USER != 'root':
            # write this stuff into the key-mapper config directory, because
            # the systemd service won't know the user sessions xmodmap
            path = get_config_path(XMODMAP_FILENAME)
            touch(path)
            with open(path, 'w') as file:
                logger.debug('Writing "%s"', path)
                json.dump(xmodmap_dict, file, indent=4)

        self._mapping.update(xmodmap_dict)

        for name, ecode in evdev.ecodes.ecodes.items():
            if name.startswith('KEY') or name.startswith('BTN'):
                self._set(name, ecode)

        self._set(DISABLE_NAME, DISABLE_CODE)

        print(self._occupied_keycodes)

    def update(self, mapping):
        """Update this with new keys.

        Parameters
        ----------
        mapping : dict
            maps from name to code. Make sure your keys are lowercase.
        """
        self._mapping.update(mapping)

    def _set(self, name, code):
        """Map name to code."""
        self._mapping[str(name).lower()] = code

    def get(self, name):
        """Return the code mapped to the key."""
        return self._mapping.get(str(name).lower())

    def get_key(self, code):
        """Return the key that maps to the provided code."""
        # TODO test
        for key in self._mapping:
            if self._mapping[key] == code:
                return key
        return None

    def get_or_allocate(self, character):
        """Get a code to inject for that character.

        If that character does not exist in the systems keyboard layout,
        remember it and return a free code for that to use.

        Without modifying the keyboard layout of the device injecting the
        returned code won't do anything.

        Parameters
        ----------
        character : string
            For example F24 or odiaeresis
        """
        # TODO test
        character = str(character).lower()

        if character in self._mapping:
            # check if part of the system layout
            return self._mapping[character]

        for code, key in self._allocated_unknowns.items():
            # check if already asked to allocate before
            if key == character:
                return code

        for code in range(256):
            # find a free keycode
            # TODO test that stuff like key_zenkakuhankaku from the linux
            #  headers are not checked to find free codes. only the xmodmap
            #  layout is relevant
            if code in self._occupied_keycodes:
                continue

            self._allocated_unknowns[code] = character
            logger.debug('Using %s for "%s"', code, character)
            return code

        # raise so that it is never forgotten to check for that case
        # instead of returning None
        raise Exception('Unknown character and no keycode free')

    def clear(self):
        """Remove all mapped keys. Only needed for tests."""
        keys = list(self._mapping.keys())
        for key in keys:
            del self._mapping[key]

    def get_unknown_mappings(self):
        """Return a mapping of codes to unknown characters.

        For example, odiaeresis is unknown on US keyboards. The code
        in that case is just any code that was not used by the systems
        keyboard layout.
        """
        # TODO test
        return self._allocated_unknowns


# one mapping object for the GUI application
custom_mapping = Mapping()

# this mapping represents the xmodmap output, which stays constant
system_mapping = SystemMapping()
