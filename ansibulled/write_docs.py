# coding: utf-8
# Author: Toshio Kuratomi <tkuratom@redhat.com>
# License: GPLv3+
# Copyright: Ansible Project, 2020
"""Output documentation."""

import asyncio
import os.path
import pkgutil
import typing as t

import aiofiles
from jinja2 import Template

from .jinja2.environment import doc_environment


#: Mapping of plugins to nonfatal errors.  This is the type to use when accepting the plugin.
#: It is of type thingy.
PluginErrorsT = t.Mapping[str, t.Mapping[str, t.Sequence[str]]]


async def write_rst(plugin_name: str, plugin_type: str, plugin_record: t.Dict[str, t.Any],
                    nonfatal_errors: PluginErrorsT, plugin_tmpl: Template, error_tmpl: Template,
                    dest_dir: str) -> None:
    """
    Write the rst page for one plugin.

    :arg plugin_name: FQCN for the plugin.
    :arg plugin_type: The type of the plugin.  (module, inventory, etc)
    :arg plugin_record: The record for the plugin.  doc, examples, and return are the
        toplevel fields.
    :arg nonfatal_errors: Mapping of plugin to any nonfatal errors that will be displayed in place
        of some or all of the docs
    :arg plugin_tmpl: Template for the plugin.
    :arg error_tmpl: Template to use when there wasn't enough documentation for the plugin.
    :arg dest_dir: Destination directory for the plugin data.  For instance,
        :file:`ansible-checkout/docs/docsite/rst/`.  The directory structure underneath this
        directory will be created if needed.
    """
    if not plugin_record:
        plugin_contents = error_tmpl.render(
            plugin_type=plugin_type, plugin_name=plugin_name,
            nonfatal_erros=nonfatal_errors[plugin_type][plugin_name])
    else:
        plugin_contents = plugin_tmpl.render(
            docs=plugin_record['docs'],
            examples=plugin_record['examples'],
            return_=plugin_record['return'],
            nonfatal_errors=nonfatal_errors[plugin_type][plugin_name])

    # plugin_record['doc']['name'] has the short name for the plugin.
    # So collection_name for a plugin from a collection is the part in front of that.
    namespace, collection, _dummy = plugin_name.split('.', 2)
    collection_name = '.'.join((namespace, collection))
    if not collection_name:
        # Ansible doesn't give itself any names
        collection_name = 'ansible.builtins'

    collection_dir = os.path.join(dest_dir, 'collections', collection_name)
    # This is dangerous but the code that takes dest_dir from the user checks
    # permissions on it to make it as safe as possible.
    os.makedirs(collection_dir, exist_ok=True)

    plugin_file = os.path.join(collection_dir, '{plugin_name}_{plugin_type}.rst')

    async with aiofiles.open(plugin_file, 'w') as f:
        await f.write(plugin_contents)


async def output_all_plugin_rst(plugin_info: t.Dict[str, t.Any],
                                nonfatal_errors: PluginErrorsT,
                                dest_dir: str) -> None:
    """
    Output rst files for each plugin.

    :arg plugin_info: Documentation information for all of the plugins.
    :arg nonfatal_errors: Mapping of plugins to nonfatal errors.  Using this to note on the docs
        pages when documentation wasn't formatted such that we could use it.
    :arg dest_dir: The directory to place the documentation in.
    """
    # Setup the jinja environment
    env = doc_environment(('ansibulled.data', 'docsite'))
    # Get the templates
    plugin_tmpl = env.get_template('plugin.rst.j2')
    error_tmpl = env.get_template('plugin-error.rst.j2')

    writers = []
    for plugin_type, plugins_by_type in plugin_info.items():
        for plugin_name, plugin_record in plugins_by_type.items():
            # Write docs for each plugin
            writers.append(asyncio.create_task(write_rst(plugin_name, plugin_type, plugin_record,
                                                         nonfatal_errors, plugin_tmpl, error_tmpl,
                                                         dest_dir)))
    asyncio.gather(*writers)
