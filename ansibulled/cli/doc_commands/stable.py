# Author: Toshio Kuratomi <tkuratom@redhat.com>
# License: GPLv3+
# Copyright: Ansible Project, 2020
"""Entrypoint to the ansibulled-docs script."""

import asyncio
import json
import os
import os.path
import tempfile
import typing as t
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

import aiohttp
from pydantic import ValidationError

from ...ansible_base import get_ansible_base
from ...collections import install_together
from ...compat import asyncio_run, best_get_loop
from ...dependency_files import DepsFile
from ...docs_parsing.ansible_doc import get_ansible_plugin_info
from ...galaxy import CollectionDownloader
from ...schemas.docs import DOCS_SCHEMAS
from ...write_docs import output_all_plugin_rst
from ...venv import VenvRunner

if t.TYPE_CHECKING:
    import argparse
    import semantic_version as semver


#: Mapping of plugins to nonfatal errors.  This is the type to use when returning the mapping.
PluginErrorsRT = t.DefaultDict[str, t.DefaultDict[str, t.List[str]]]


async def retrieve(ansible_base_version: str,
                   collections: t.Mapping[str, str],
                   tmp_dir: str) -> t.Dict[str, 'semver.Version']:
    """
    Download ansible-base and the collections.

    :arg ansible_base_version: Version of ansible-base to download.
    :arg collections: Map of collection names to collection versions to download.
    :arg tmp_dir: The directory to download into
    :returns: Map of collection name to directory it is in.  ansible-base will
        use the special key, `_ansible_base`.
    """
    collection_dir = os.path.join(tmp_dir, 'collections')
    os.mkdir(collection_dir, mode=0o700)

    requestors = {}
    async with aiohttp.ClientSession() as aio_session:
        requestors['_ansible_base'] = asyncio.create_task(get_ansible_base(aio_session,
                                                                           ansible_base_version,
                                                                           tmp_dir))
        downloader = CollectionDownloader(aio_session, collection_dir)
        for collection, version in collections.items():
            requestors[collection] = asyncio.create_task(downloader.download(collection, version))

        responses = await asyncio.gather(*requestors.values())

    # Note: Python dicts have always had a stable order as long as you don't modify the dict.
    # So requestors (implicitly, the keys) and responses have a matching order here.
    return dict(zip(requestors, responses))


def normalize_plugin_info(plugin_type: str,
                          plugin_info: t.Dict[str, t.Any]
                          ) -> t.Tuple[t.Dict[str, t.Any], t.List[str]]:
    # That way we'll be able to make docs as long as there is a parsable doc value
    new_info = {}
    errors = []
    # Note: loop through "doc" before any other keys.
    for field in ('doc', 'examples', 'return'):
        try:
            new_info.update(DOCS_SCHEMAS[plugin_type][field].parse_obj(plugin_info).dict())
        except ValidationError as e:
            if field == 'doc':
                # We can't recover if there's not a doc field
                raise
            # But we can use the default value (some variant of "empty") for everything else
            # Note: We looped through doc first and raised an exception if doc did not normalize
            # so we're able to use it in the error message here.
            errors.append(f'Unable to normalize {new_info["doc"]["name"]}: {field} due to: {e}')
            new_info.update(DOCS_SCHEMAS[plugin_type][field].parse_obj({}))

    return (new_info, errors)


async def normalize_all_plugin_info(plugin_info: t.Dict[str, t.Dict[str, t.Any]]
                                    ) -> t.Tuple[t.Dict[str, t.Dict[str, t.Any]], PluginErrorsRT]:
    """
    Normalize the data in plugin_info so that it is ready to be passed to the templates.

    """
    loop = best_get_loop()

    normalizers = {}
    for plugin_type, plugin_list_for_type in plugin_info.items():
        for plugin_name, plugin_record in plugin_list_for_type.items():
            normalizers[(plugin_type, plugin_name)] = loop.run_in_executor(
                ProcessPoolExecutor(), normalize_plugin_info, plugin_type, plugin_record)

    results = await asyncio.gather(*normalizers.values(), return_exceptions=True)

    new_plugin_info = defaultdict(dict)
    nonfatal_errors = defaultdict(lambda: defaultdict(list))
    for (plugin_type, plugin_name), plugin_record in zip(normalizers, results):
        if isinstance(plugin_record, Exception):
            nonfatal_errors[plugin_type][plugin_name].append(str(plugin_record))
            # An exception means there is no usable documentation for this plugin
            new_plugin_info[plugin_type][plugin_name] = {}
            continue

        if plugin_record[1]:
            nonfatal_errors[plugin_type][plugin_name].extend(results[1])

        new_plugin_info[plugin_type][plugin_name] = plugin_record[0]

    return new_plugin_info, nonfatal_errors


def generate_docs(args: 'argparse.Namespace') -> int:
    """
    Create documentation for the stable subcommand.

    Stable documentation creates documentation for a built version of Ansible.  It uses the exact
    versions of collections included in the last Ansible release to generate rst files documenting
    those collections.

    :arg args: The parsed comand line args.
    :returns: A return code for the program.  See :func:`ansibulled.cli.ansibulled_docs.main` for
        details on what each code means.
    """
    # Parse the deps file
    deps_file = DepsFile(args.deps_file)
    ansible_version, ansible_base_version, collections = deps_file.parse()

    with tempfile.TemporaryDirectory() as tmp_dir:
        if False:
            # Retrieve ansible-base and the collections
            collection_tarballs = asyncio_run(retrieve(ansible_base_version, collections, tmp_dir))

            # Get the ansible-base location
            try:
                ansible_base_tarball = collection_tarballs.pop('_ansible_base')
            except KeyError:
                print('ansible-base did not download successfully')
                return 3

            # Install the collections to a directory

            # Directory that ansible needs to see
            collection_dir = os.path.join(tmp_dir, 'installed')
            # Directory that the collections will be untarred inside of
            collection_install_dir = os.path.join(collection_dir, 'ansible_collections')
            # Safe to recursively mkdir because we created the tmp_dir
            os.makedirs(collection_install_dir, mode=0o700)
            asyncio_run(install_together(collection_tarballs.values(), collection_install_dir))

            # Create venv for ansible-base
            venv = VenvRunner('ansible-base-venv', tmp_dir)
            venv.install_package(ansible_base_tarball)

            # Get the list of plugins
            plugin_info = asyncio_run(get_ansible_plugin_info(venv, collection_dir))

            with open('/srv/ansible/ansibulled/dump_data.json', 'w') as f:
                f.write(json.dumps(plugin_info))

        with open('/srv/ansible/ansibulled/dump_data.json', 'r') as f:
            data = f.read()
            plugin_info = json.loads(data)

        plugin_info, nonfatal_errors = asyncio_run(normalize_all_plugin_info(plugin_info))
        # calculate_additional_info(plugin_info)
        asyncio_run(output_all_plugin_rst(plugin_info, nonfatal_errors, args.dest_dir))
        # format data
        # format indexes
        # output data
        # output indexes

    # Use the ansible-base version with only those collections to extract the raw data that we care
    # about
    # Format the raw data into something that we can template
    # Template the raw data to rst files
    # Output to the destdir
    return 0
