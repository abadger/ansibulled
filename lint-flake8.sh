#!/bin/bash
set -e
poetry run flake8 src/ansibulled --count --max-complexity=10 --max-line-length=100 --statistics "$@"
