# Author: Toshio Kuratomi <tkuratom@redhat.com>
# License: GPLv3+
# Copyright: Ansible Project, 2020
"""Entrypoint to the ansibulled-docs script."""

import asyncio
import json
import os
import os.path
import tempfile
from typing import TYPE_CHECKING, Any, Dict, Mapping

import aiohttp
import sh

from ...ansible_base import get_ansible_base
from ...collections import install_together
from ...compat import asyncio_run
from ...dependency_files import DepsFile
from ...docs_parsing.ansible_doc import get_ansible_plugin_info
from ...galaxy import CollectionDownloader
from ...venv import VenvRunner

if TYPE_CHECKING:
    import argparse
    import semantic_version as semver


async def retrieve(ansible_base_version: str,
                   collections: Mapping[str, str],
                   tmp_dir: str) -> Dict[str, 'semver.Version']:
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


async def transform_plugin_info(plugin_info: Dict[str, Dict[str, Any]]) -> None:
    """
    Normalize the data in plugin_info so that it is ready to be passed to the templates.

    .. warn:: This function operates by side effect.  plugin_info will be modified.
    """
    plugin_documentation_data = {}


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

        with open('/var/tmp/dump_data.json', 'w') as f:
            f.write(json.dumps(plugin_info))

        input()

        transform_plugin_info(plugin_info)
        calculate_additional_info(plugin_info)
        output_rst(plugin_info, args.dest_dir)
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
