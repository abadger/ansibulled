# coding: utf-8
# Copyright: (c) 2019, Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

# Make coding more python3-ish
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type


import datetime
import os.path
import warnings
from collections.abc import Mapping

import yaml

from ansible.module_utils.common.collections import is_sequence
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.module_utils.six import string_types
from ansible.plugins.loader import fragment_loader
from ansible.utils import plugin_docs

from .jinja2.filters import rst_ify


class RemovedPlugin(Exception):
    """Raised when a plugin has been removed and thus is no longer documented"""


class MalformedDocumentation(Exception):
    """Raised when module documentation is malformed"""


def _normalize_options(value):
    """Normalize boolean option value."""

    if value.get('type') == 'bool' and 'default' in value:
        try:
            value['default'] = boolean(value['default'], strict=True)
        except TypeError:
            pass
    return value


def _add_full_key_to_returndocs(returndocs, full_key=None):
    """
    Add a list of the heirarchical elements to pass through to every return key in returndocs.

    :arg returndocs: The returndocs dict to work through to add full_key entries.
    :kwarg full_key: If we're called recursively, then this is the full_key determined so far.
    :returns: New version of returndocs that has a key for each return entry named `full_key`.
        The entry is a list of all the elements passed through to get to this entry.  For
        instance, if the return value looks like this::

            {"databases":
                "database_name":
                    "access_priv": ...
            }

        then the full_key entry for access_priv will be::

            ['databases', 'database_name', 'access_priv']
    """
    if full_key is None:
        full_key = []

    new_returndocs = {}
    for (key, entry) in returndocs.items():
        new_entry = entry.copy()

        # Make sure that "full key" is contained
        full_key_k = full_key + [key]
        new_entry['full_key'] = full_key_k

        new_returndocs[key] = new_entry

        # Process suboptions
        suboptions = new_entry.get('contains')
        if suboptions:
            if isinstance(suboptions, Mapping):
                new_entry['contains'] = _add_full_key_to_returndocs(suboptions, full_key=full_key_k)
            elif is_sequence(suboptions):
                new_entry['contains'] = _add_full_key_to_returndocs(suboptions[0],
                                                                    full_key=full_key_k)

    return new_returndocs


def get_embedded_plugin_docs(module_path, verbose=False):
    """Return the documentation that's embedded inside of a module"""

    # use ansible core library to parse out doc metadata YAML and plaintext examples
    doc, examples, returndocs, metadata = plugin_docs.get_docstring(module_path, fragment_loader)

    if metadata and 'removed' in metadata.get('status', []):
        raise RemovedPlugin('%s has been removed' % os.path.basename(module_path))

    if not doc:
        raise MalformedDocumentation('DOCUMENTATION section missing for %s' % module_path)

    if 'options' in doc and doc['options'] is None:
        error_msg = 'DOCUMENTATION.options must be a dictionary when used.'
        pos = getattr(doc, "ansible_pos", None)
        if pos is not None:
            error_msg += " Module position: %s, %d, %d" % pos
        raise MalformedDocumentation(error_msg)

    for key, opt in doc.get('options', {}).items():
        doc['options'][key] = _normalize_options(opt)

    if returndocs is not None:
        try:
            returndocs = yaml.safe_load(returndocs)
        except Exception as e:
            module = os.path.basename(module_path)
            module = os.path.splitext(module)[0]
            print("%s:%s: yaml error: %s: returndocs=%s" % (module_path, module, e, returndocs))
            returndocs = None

        if returndocs:
            returndocs = _add_full_key_to_returndocs(returndocs)

    # save all the information
    module_data = {'metadata': metadata,
                   'doc': doc,
                   'examples': examples,
                   'returndocs': returndocs,
                   }

    return module_data


def get_filesystem_plugin_info(plugin_name, plugin_path):
    """
    Get information about a plugin which is encoded in the filesystem

    :arg plugin_name: The name of a plugin as used in the ansible playbook
    :arg plugin_path: The filesystem location of a plugin
    :returns: plugin_fs_info is a dict that contains a single field, `src_plugin`.  If the plugin is
        an alias, then src_plugin is the plugin that this is an alias to.  Otherwise, it is None
    """
    plugin_fs_info = {'src_plugin': None}
    if os.path.islink(plugin_path):
        # Handle aliases
        source = os.path.splitext(os.path.basename(os.path.realpath(plugin_path)))[0]
        if source.startswith("_"):
            source = source.replace("_", "", 1)
        plugin_fs_info['src_plugin'] = source

    return plugin_fs_info


def get_plugin_info(plugin_name, plugin_path, module_dir):
    """
    Return info about a specific plugin

    :arg  plugin_name: Unnormalized plugin name.  This corresponds to the filename minus directory
        and suffix
    :arg plugin_path: Full path to the plugin
    :arg module_dir: The directory wherein all modules live.
    :returns: a dict of information about this plugin gleaned from the filesystem.  Ansible stores
        deprecation and whether the plugin is an alias on the filesystem.  So we return the
        following data:
            :is_alias: If True, this is an alias, else it is a regular plugin
            :deprecated: If True, this plugin is deprecated.
            :plugin_name: The canonical, non-FQCN name for the plugin
            :src_plugin: If is_alias is True, the name of the source plugin this is an alias
                for.  Otherwise, None
            :source: If is_alias is False, this is the path to the plugin relative to module_dir
        The output of get_embedded_plugin_docs is also added to this dictionary, verbatim.
    """
    # Canonicalize the plugin name (to the form used in playbooks) and decide if we need to
    # lookup information about deprecated or aliased status via the filesystem
    deprecated = False
    if plugin_name.startswith('_'):
        deprecated = True
        plugin_name = plugin_name.replace('_', '', 1)

    # Default values
    plugin_record = {'is_alias': False,
                     'deprecated': deprecated,
                     'plugin_name': plugin_name,
                     'src_plugin': None}

    # Values that we already know
    plugin_record['source'] = os.path.relpath(plugin_path, module_dir)

    if deprecated:
        # Get info about the plugin that's recorded by the filesystem
        # which tells us whether this module is an alias to a replacement
        plugin_fs_info = get_filesystem_plugin_info(plugin_name, plugin_path)

        src_plugin = plugin_fs_info['src_plugin']
        if src_plugin is not None:
            # This is an alias.  So we're only going to document a little information about the
            # alias (the main documentation is what the alias points at)
            plugin_record.update({'source': None,
                                  'is_alias': True,
                                  'deprecated': True,
                                  'src_plugin': src_plugin,
                                  'plugin_name': plugin_name})
            return plugin_record

        # Note: Deprecated but not aliased: We'll still extract the documentation and add a note
        # that it is deprecated

    # Extract the docs from the plugin
    plugin_embedded_info = get_embedded_plugin_docs(plugin_path)

    plugin_record.update(plugin_embedded_info)
    return plugin_record


def get_option_names(plugin_name, options, full_key=None):
    if full_key is None:
        full_key = []

    option_names = []

    if not options:
        return option_names

    for (k, v) in options.items():
        # Make sure that "full key" is contained
        full_key_k = full_key + [k]
        v['full_key'] = full_key_k

        # Error out if there's no description
        if 'description' not in v:
            raise MalformedDocumentation("Missing required description for parameter"
                                         " '%s' in '%s' " % (k, plugin_name))

        # Make sure description is a list of lines for later formatting
        if isinstance(v['description'], string_types):
            v['description'] = [v['description']]
        elif not isinstance(v['description'], (list, tuple)):
            raise MalformedDocumentation("Invalid type for options['%s']['description']."
                                         " Must be string or list of strings.  Got %s" %
                                         (k, type(v['description'])))

        # Error out if required isn't a boolean (people have been putting
        # information on when something is required in here.  Those need
        # to go in the description instead).
        required_value = v.get('required', False)
        if not isinstance(required_value, bool):
            raise MalformedDocumentation("Invalid required value '%s' for parameter"
                                         " '%s' in '%s' (must be truthy)" %
                                         (required_value, k, plugin_name))

        # TODO: This probably belongs somewhere else
        # It appears to be validating the suboptions.  But the suboption names aren't saved here so
        # it belongs in a different function.
        if 'suboptions' in v and v['suboptions']:
            if isinstance(v['suboptions'], dict):
                get_option_names(plugin_name, v['suboptions'], full_key=full_key_k)
            elif isinstance(v['suboptions'][0], dict):
                get_option_names(plugin_name, v['suboptions'][0], full_key=full_key_k)

        option_names.append(k)

    option_names.sort()

    return option_names


def normalize_plugin_info(doc, plugin_name, collection, collection_source, source, plugin_type,
                          deprecated, aliases, metadata, examples, returndocs):
    """
    Normalize and validate the data about the plugin that we've received

    :arg doc: The value from parsing the DOCUMENTATION attribute from the plugin.  This is
        a dictionary which will be modified to fit the data
    :arg plugin_name: The name of the plugin
    :arg collection: The FQCN.  If this plugin is not in a collection, use None
    :arg collection_source: The server a collection is hosted on.  If this plugin is not in
        a collection, use None
    :arg source: Relative path to the plugin file
    :arg plugin_type: The type of plugin (module, callback, etc)
    :arg deprecated: Whether the plugin is deprecated
    :arg aliases: set of aliases to this plugin
    :arg metadata: the ANSIBLE_METADATA field from the plugin
    :arg examples: examples from the plugin docs
    :arg returndocs: returndocs parsed from the plugindocs

    .. warning:: This function operated by side effect, modifying the value of `doc`.
    """

    # Output stub data if a plugin is missing documentation
    if doc is None:
        warnings.warn("%s MISSING DOCUMENTATION" % (source,))
        doc = {'short_description': 'MISSING DOCUMENTATION',
               'description': []}
    else:
        # Do some checks that we know would print warnings because DOCUMENTATION was missing (we've
        # already printed that error so we don't need to do it again)
        short_desc = doc['short_description']
        if short_desc is None:
            short_desc = ''
        doc['short_description'] = rst_ify(short_desc.rstrip('.'))  ##

        if short_desc == '':
            warnings.warn('short_description for %s was empty' % source)

        # don't show version added information if it's too old to be called out
        version_added = doc.get('version_added', None)
        if not version_added:
            warnings.warn('version_added for %s was empty' % source)

    if deprecated and 'deprecated' not in doc:
        warnings.warn("%s PLUGIN MISSING DEPRECATION DOCUMENTATION: %s" % (source, 'deprecated'))
    # check the 'deprecated' field in doc. We expect a dict potentially with 'why', 'version', and
    # 'alternative' fields

    if isinstance(doc['description'], string_types):
        doc['description'] = [doc['description']]
    elif not isinstance(doc['description'], (list, tuple)):
        raise MalformedDocumentation("Description must be a string or list of strings.  Got %s"
                                     % type(doc['description']))

    #
    # The present template gets everything from doc so we spend most of this
    # function moving data into doc for the template to reference
    #

    doc['module'] = plugin_name
    doc['collection'] = collection
    doc['collection_source'] = (
        collection_source[:-1]
        if collection_source and collection_source.endswith('/') else
        collection_source
    )
    doc['docuri'] = plugin_name.replace('_', '-')
    doc['source'] = source
    doc['plugin_type'] = plugin_type
    doc['aliases'] = aliases
    doc['now_date'] = datetime.date.today().strftime('%Y-%m-%d')
    doc['metadata'] = metadata

    doc['option_keys'] = get_option_names(plugin_name, doc.get('options'))

    # use 'examples' for 'plainexamples' if 'examples' is a string
    if isinstance(examples, string_types):
        doc['plainexamples'] = examples  # plain text
    else:
        doc['plainexamples'] = ''

    if returndocs:
        doc['returndocs'] = returndocs
    else:
        doc['returndocs'] = []

    doc['author'] = doc.get('author', ['UNKNOWN'])
    if isinstance(doc['author'], string_types):
        doc['author'] = [doc['author']]
