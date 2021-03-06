# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2014-2017 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Configuration files residing on disk."""

import pathlib
import types
import os.path
import sys
import textwrap
import traceback
import configparser
import contextlib

import yaml
from PyQt5.QtCore import pyqtSignal, QObject, QSettings

import qutebrowser
from qutebrowser.config import configexc, config, configdata
from qutebrowser.utils import standarddir, utils, qtutils


# The StateConfig instance
state = None


class StateConfig(configparser.ConfigParser):

    """The "state" file saving various application state."""

    def __init__(self):
        super().__init__()
        self._filename = os.path.join(standarddir.data(), 'state')
        self.read(self._filename, encoding='utf-8')
        for sect in ['general', 'geometry']:
            try:
                self.add_section(sect)
            except configparser.DuplicateSectionError:
                pass

        deleted_keys = ['fooled', 'backend-warning-shown']
        for key in deleted_keys:
            self['general'].pop(key, None)

    def init_save_manager(self, save_manager):
        """Make sure the config gets saved properly.

        We do this outside of __init__ because the config gets created before
        the save_manager exists.
        """
        save_manager.add_saveable('state-config', self._save)

    def _save(self):
        """Save the state file to the configured location."""
        with open(self._filename, 'w', encoding='utf-8') as f:
            self.write(f)


class YamlConfig(QObject):

    """A config stored on disk as YAML file.

    Class attributes:
        VERSION: The current version number of the config file.
    """

    VERSION = 1
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._filename = os.path.join(standarddir.config(auto=True),
                                      'autoconfig.yml')
        self._values = {}
        self._dirty = None

    def init_save_manager(self, save_manager):
        """Make sure the config gets saved properly.

        We do this outside of __init__ because the config gets created before
        the save_manager exists.
        """
        save_manager.add_saveable('yaml-config', self._save, self.changed)

    def __getitem__(self, name):
        return self._values[name]

    def __setitem__(self, name, value):
        self._values[name] = value
        self._mark_changed()

    def __contains__(self, name):
        return name in self._values

    def __iter__(self):
        return iter(sorted(self._values.items()))

    def _mark_changed(self):
        """Mark the YAML config as changed."""
        self._dirty = True
        self.changed.emit()

    def _save(self):
        """Save the settings to the YAML file if they've changed."""
        if not self._dirty:
            return

        data = {'config_version': self.VERSION, 'global': self._values}
        with qtutils.savefile_open(self._filename) as f:
            f.write(textwrap.dedent("""
                # DO NOT edit this file by hand, qutebrowser will overwrite it.
                # Instead, create a config.py - see :help for details.

            """.lstrip('\n')))
            utils.yaml_dump(data, f)

    def load(self):
        """Load configuration from the configured YAML file."""
        try:
            with open(self._filename, 'r', encoding='utf-8') as f:
                yaml_data = utils.yaml_load(f)
        except FileNotFoundError:
            return {}
        except OSError as e:
            desc = configexc.ConfigErrorDesc("While reading", e)
            raise configexc.ConfigFileErrors('autoconfig.yml', [desc])
        except yaml.YAMLError as e:
            desc = configexc.ConfigErrorDesc("While parsing", e)
            raise configexc.ConfigFileErrors('autoconfig.yml', [desc])

        try:
            global_obj = yaml_data['global']
        except KeyError:
            desc = configexc.ConfigErrorDesc(
                "While loading data",
                "Toplevel object does not contain 'global' key")
            raise configexc.ConfigFileErrors('autoconfig.yml', [desc])
        except TypeError:
            desc = configexc.ConfigErrorDesc("While loading data",
                                             "Toplevel object is not a dict")
            raise configexc.ConfigFileErrors('autoconfig.yml', [desc])

        if not isinstance(global_obj, dict):
            desc = configexc.ConfigErrorDesc(
                "While loading data",
                "'global' object is not a dict")
            raise configexc.ConfigFileErrors('autoconfig.yml', [desc])

        # Delete unknown values
        # (e.g. options which were removed from configdata.yml)
        for name in list(global_obj):
            if name not in configdata.DATA:
                del global_obj[name]

        self._values = global_obj
        self._dirty = False

    def unset(self, name):
        """Remove the given option name if it's configured."""
        try:
            del self._values[name]
        except KeyError:
            return
        self._mark_changed()

    def clear(self):
        """Clear all values from the YAML file."""
        self._values = []
        self._mark_changed()


class ConfigAPI:

    """Object which gets passed to config.py as "config" object.

    This is a small wrapper over the Config object, but with more
    straightforward method names (get/set call get_obj/set_obj) and a more
    shallow API.

    Attributes:
        _config: The main Config object to use.
        _keyconfig: The KeyConfig object.
        errors: Errors which occurred while setting options.
        configdir: The qutebrowser config directory, as pathlib.Path.
        datadir: The qutebrowser data directory, as pathlib.Path.
    """

    def __init__(self, conf, keyconfig):
        self._config = conf
        self._keyconfig = keyconfig
        self.errors = []
        self.configdir = pathlib.Path(standarddir.config())
        self.datadir = pathlib.Path(standarddir.data())

    @contextlib.contextmanager
    def _handle_error(self, action, name):
        try:
            yield
        except configexc.ConfigFileErrors as e:
            for err in e.errors:
                new_err = err.with_text(e.basename)
                self.errors.append(new_err)
        except configexc.Error as e:
            text = "While {} '{}'".format(action, name)
            self.errors.append(configexc.ConfigErrorDesc(text, e))

    def finalize(self):
        """Do work which needs to be done after reading config.py."""
        self._config.update_mutables()

    def load_autoconfig(self):
        with self._handle_error('reading', 'autoconfig.yml'):
            read_autoconfig()

    def get(self, name):
        with self._handle_error('getting', name):
            return self._config.get_obj(name)

    def set(self, name, value):
        with self._handle_error('setting', name):
            self._config.set_obj(name, value)

    def bind(self, key, command, mode='normal'):
        with self._handle_error('binding', key):
            self._keyconfig.bind(key, command, mode=mode)

    def unbind(self, key, mode='normal'):
        with self._handle_error('unbinding', key):
            self._keyconfig.unbind(key, mode=mode)


class ConfigPyWriter:

    """Writer for config.py files from given settings."""

    def __init__(self, options, bindings, *, commented):
        self._options = options
        self._bindings = bindings
        self._commented = commented

    def write(self, filename):
        """Write the config to the given file."""
        with open(filename, 'w', encoding='utf-8') as f:
            f.write('\n'.join(self._gen_lines()))

    def _line(self, line):
        """Get an (optionally commented) line."""
        if self._commented:
            if line.startswith('#'):
                return '#' + line
            else:
                return '# ' + line
        else:
            return line

    def _gen_lines(self):
        """Generate a config.py with the given settings/bindings.

        Yields individual lines.
        """
        yield from self._gen_header()
        yield from self._gen_options()
        yield from self._gen_bindings()

    def _gen_header(self):
        """Generate the initial header of the config."""
        yield self._line("# Autogenerated config.py")
        yield self._line("# Documentation:")
        yield self._line("#   qute://help/configuring.html")
        yield self._line("#   qute://help/settings.html")
        yield ''
        if self._commented:
            # When generated from an autoconfig.yml with commented=False,
            # we don't want to load that autoconfig.yml anymore.
            yield self._line("# This is here so configs done via the GUI are "
                             "still loaded.")
            yield self._line("# Remove it to not load settings done via the "
                             "GUI.")
            yield self._line("config.load_autoconfig()")
            yield ''
        else:
            yield self._line("# Uncomment this to still load settings "
                             "configured via autoconfig.yml")
            yield self._line("# config.load_autoconfig()")
            yield ''

    def _gen_options(self):
        """Generate the options part of the config."""
        for opt, value in self._options:
            if opt.name in ['bindings.commands', 'bindings.default']:
                continue

            for line in textwrap.wrap(opt.description):
                yield self._line("# {}".format(line))

            yield self._line("# Type: {}".format(opt.typ.get_name()))

            valid_values = opt.typ.get_valid_values()
            if valid_values is not None and valid_values.generate_docs:
                yield self._line("# Valid values:")
                for val in valid_values:
                    try:
                        desc = valid_values.descriptions[val]
                        yield self._line("#   - {}: {}".format(val, desc))
                    except KeyError:
                        yield self._line("#   - {}".format(val))

            yield self._line('c.{} = {!r}'.format(opt.name, value))
            yield ''

    def _gen_bindings(self):
        """Generate the bindings part of the config."""
        normal_bindings = self._bindings.pop('normal', {})
        if normal_bindings:
            yield self._line('# Bindings for normal mode')
        for key, command in sorted(normal_bindings.items()):
            yield self._line('config.bind({!r}, {!r})'.format(key, command))

        for mode, mode_bindings in sorted(self._bindings.items()):
            yield ''
            yield self._line('# Bindings for {} mode'.format(mode))
            for key, command in sorted(mode_bindings.items()):
                yield self._line('config.bind({!r}, {!r}, mode={!r})'.format(
                    key, command, mode))


def read_config_py(filename, raising=False):
    """Read a config.py file.

    Arguments;
        filename: The name of the file to read.
        raising: Raise exceptions happening in config.py.
                 This is needed during tests to use pytest's inspection.
    """
    assert config.instance is not None
    assert config.key_instance is not None

    api = ConfigAPI(config.instance, config.key_instance)
    container = config.ConfigContainer(config.instance, configapi=api)
    basename = os.path.basename(filename)

    module = types.ModuleType('config')
    module.config = api
    module.c = container
    module.__file__ = filename

    try:
        with open(filename, mode='rb') as f:
            source = f.read()
    except OSError as e:
        text = "Error while reading {}".format(basename)
        desc = configexc.ConfigErrorDesc(text, e)
        raise configexc.ConfigFileErrors(basename, [desc])

    try:
        code = compile(source, filename, 'exec')
    except ValueError as e:
        # source contains NUL bytes
        desc = configexc.ConfigErrorDesc("Error while compiling", e)
        raise configexc.ConfigFileErrors(basename, [desc])
    except SyntaxError as e:
        desc = configexc.ConfigErrorDesc("Syntax Error", e,
                                         traceback=traceback.format_exc())
        raise configexc.ConfigFileErrors(basename, [desc])

    try:
        # Save and restore sys variables
        with saved_sys_properties():
            # Add config directory to python path, so config.py can import
            # other files in logical places
            config_dir = os.path.dirname(filename)
            if config_dir not in sys.path:
                sys.path.insert(0, config_dir)

            exec(code, module.__dict__)
    except Exception as e:
        if raising:
            raise
        api.errors.append(configexc.ConfigErrorDesc(
            "Unhandled exception",
            exception=e, traceback=traceback.format_exc()))

    api.finalize()

    if api.errors:
        raise configexc.ConfigFileErrors('config.py', api.errors)


def read_autoconfig():
    """Read the autoconfig.yml file."""
    try:
        config.instance.read_yaml()
    except configexc.ConfigFileErrors as e:
        raise  # caught in outer block
    except configexc.Error as e:
        desc = configexc.ConfigErrorDesc("Error", e)
        raise configexc.ConfigFileErrors('autoconfig.yml', [desc])


@contextlib.contextmanager
def saved_sys_properties():
    """Save various sys properties such as sys.path and sys.modules."""
    old_path = sys.path.copy()
    old_modules = sys.modules.copy()

    try:
        yield
    finally:
        sys.path = old_path
        for module in set(sys.modules).difference(old_modules):
            del sys.modules[module]


def init():
    """Initialize config storage not related to the main config."""
    global state
    state = StateConfig()
    state['general']['version'] = qutebrowser.__version__

    # Set the QSettings path to something like
    # ~/.config/qutebrowser/qsettings/qutebrowser/qutebrowser.conf so it
    # doesn't overwrite our config.
    #
    # This fixes one of the corruption issues here:
    # https://github.com/qutebrowser/qutebrowser/issues/515

    path = os.path.join(standarddir.config(auto=True), 'qsettings')
    for fmt in [QSettings.NativeFormat, QSettings.IniFormat]:
        QSettings.setPath(fmt, QSettings.UserScope, path)
